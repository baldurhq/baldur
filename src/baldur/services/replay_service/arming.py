"""On-recovery auto-replay arming probe.

Answers "is the event-driven DLQ auto-replay loop armed right now, and if not,
which prerequisite is missing?" as a single on-demand evaluation. This probe is
the single source of truth behind three operator surfaces: the Prometheus
``baldur_dlq_auto_replay_armed`` gauge, the ``GET /dlq/cleanup/stats``
``auto_replay`` block, and the console armed/disarmed badge.

The verdict is derived from what was **verified**, never from what was not
refuted. A link answers ``"ok"`` / ``"missing"`` / ``"unknown"``, and a path
folds to True only when every link on it came back ``"ok"``; any ``"missing"``
folds to False, and an unrefuted ``"unknown"`` folds to None — indeterminate,
not armed.

Two sweeps can drain the DLQ on recovery and they no longer share a
prerequisite set, so each is folded as its own lane and the headline verdict is
the any-lane fold::

    shared:        disabled -> celery_missing -> worker_missing -> handler_missing
    mapped:        shared + map_unconfigured
    open_circuit:  shared + open_circuit_capture_disabled

``disabled`` / ``celery_missing`` are hard prerequisites: once one is missing
the downstream links are not evaluated. The remaining links are independent
leaf checks evaluated together, so ``missing_links`` may carry more than one.

The name is deliberately NOT ``health_check`` / ``is_healthy`` / ``check_health``
— this is configuration completeness, not component health.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import cast

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "ArmingStatus",
    "DispatchRecord",
    "LaneStatus",
    "get_dispatch_ledger",
    "get_on_recovery_arming_status",
    "get_worker_cache",
    "record_dispatch_outcome",
    "refresh_armed_gauge",
    "reset_dispatch_ledger",
    "reset_worker_cache",
]

# Queue the on-recovery replay task is pinned to (see celery_tasks.dlq_tasks).
_DLQ_QUEUE = "dlq_processing"

# Link vocabulary. Every link answers with exactly one of these.
_OK = "ok"
_MISSING = "missing"
_UNKNOWN = "unknown"

# Outcomes that actually called the replay task — the only ones the ledger keeps.
_DISPATCH_ATTEMPT_OUTCOMES = ("dispatched", "error")

# Links every lane depends on, in evaluation order.
_SHARED_LINK_ORDER = (
    "disabled",
    "celery_missing",
    "worker_missing",
    "handler_missing",
)

# Lane name -> the one link only that lane depends on.
_LANE_LINKS = {
    "open_circuit": "open_circuit_capture_disabled",
    "mapped": "map_unconfigured",
}

# Lane report order (the open-circuit lane is the shipped-default sweep).
_LANE_ORDER = ("open_circuit", "mapped")

# Global link order — shared links first, since they block both lanes, then the
# lane links. Drives the headline ``missing_link`` and the ``missing_links`` array.
_LINK_ORDER = (*_SHARED_LINK_ORDER, "map_unconfigured", "open_circuit_capture_disabled")

# Name of the daemon thread that owns the broker round-trip.
_PROBE_THREAD_NAME = "replay_arming_probe"

# Single key of the worker-presence cache (one probe, one process).
_WORKER_CACHE_KEY = "state"

# Worker-presence probe state, all per process. The broker round-trip runs on a
# dedicated daemon thread and its verdict is cached for
# ``worker_status_cache_ttl_seconds`` so the console's periodic stats polling
# does not pay a round-trip per request. One lock guards all of it, so
# concurrent polls share a single broker call. The sequence advances only when
# a waiter abandons an attempt at its deadline; a cache reset leaves a live
# attempt joinable.
_worker_cache_lock = threading.Lock()
_worker_cache: dict[str, tuple[float, str]] = {}
_probe_inflight: Future[str] | None = None
_probe_thread: threading.Thread | None = None
_probe_seq = 0
_last_logged_worker_state: str | None = None
_probe_state_pid: int | None = None

# In-process dispatch ledger — the observed-past evidence behind ``last_dispatch``.
_dispatch_lock = threading.Lock()
_dispatch_record: DispatchRecord | None = None


@dataclass(frozen=True)
class LaneStatus:
    """Verdict for one replay sweep.

    Attributes:
        armed: True when every link on this lane's path is satisfied; False
            when one is missing; None when one could not be verified.
        link: The lane's first missing link (when False) or first unverified
            link (when None); None when armed.
    """

    armed: bool | None
    link: str | None

    def to_dict(self) -> dict:
        """Serialise for the REST arming block."""
        return {"armed": self.armed, "link": self.link}


@dataclass(frozen=True)
class DispatchRecord:
    """Last on-recovery dispatch attempt observed in this process.

    Attributes:
        outcome: ``"dispatched"`` or ``"error"`` — an evaluation that never
            called the task is not an attempt and is not recorded.
        at: When the attempt was observed (UTC).
        service_name: The service whose circuit closed.
        error: Error text for a failed attempt, else None.
        consecutive_failures: Consecutive failed attempts ending at this one.
        pid: Process that observed it — a record inherited across ``fork()``
            is not this process's observation.
    """

    outcome: str
    at: datetime
    service_name: str
    error: str | None
    consecutive_failures: int
    pid: int

    def to_dict(self) -> dict:
        """Serialise for the REST arming block."""
        return {
            "outcome": self.outcome,
            "at": self.at.isoformat(),
            "service_name": self.service_name,
            "error": self.error,
            "consecutive_failures": self.consecutive_failures,
            "pid": self.pid,
        }


@dataclass(frozen=True)
class ArmingStatus:
    """Immutable result of an on-recovery arming evaluation.

    Attributes:
        armed: True when at least one lane verified every prerequisite; False
            when every lane has a missing prerequisite; None when no lane is
            armed and at least one prerequisite could not be verified.
        missing_link: The first missing link in the global order (headline).
            Set only when ``armed`` is False.
        missing_links: Every missing link in global order; empty whenever
            ``armed`` is not False.
        unverified_link: The first link that could not be verified. Set only
            when ``armed`` is None.
        links: Full per-link state map ("ok" / "missing" / "unknown").
        lanes: Per-sweep breakdown, keyed by lane name.
        last_dispatch: The last dispatch attempt this process observed, if any.
    """

    armed: bool | None
    missing_link: str | None
    missing_links: list[str] = field(default_factory=list)
    unverified_link: str | None = None
    links: dict[str, str] = field(default_factory=dict)
    lanes: dict[str, LaneStatus] = field(default_factory=dict)
    last_dispatch: DispatchRecord | None = None

    @classmethod
    def probe_failed(cls, last_dispatch: DispatchRecord | None = None) -> ArmingStatus:
        """Fail-open sentinel — the probe raised, so arming is indeterminate.

        A probe fault is an unverified cause, not a missing prerequisite, so
        the headline is ``unverified_link`` and ``missing_link`` stays None.
        """
        return cls(
            armed=None,
            missing_link=None,
            missing_links=[],
            unverified_link="probe_failed",
            links={},
            lanes={},
            last_dispatch=last_dispatch,
        )

    def to_dict(self) -> dict:
        """Serialise the whole verdict — the one REST shape for this status."""
        return {
            "armed": self.armed,
            "missing_link": self.missing_link,
            "missing_links": list(self.missing_links),
            "unverified_link": self.unverified_link,
            "links": dict(self.links),
            "lanes": {name: lane.to_dict() for name, lane in self.lanes.items()},
            "last_dispatch": (
                self.last_dispatch.to_dict() if self.last_dispatch is not None else None
            ),
        }


def _resolve_replay_config() -> dict:
    """Resolve replay-automation config: RuntimeConfig (present) → settings.

    Behaviour-consistent by construction: the RuntimeConfigManager's own
    defaults derive from a fresh ``ReplayAutomationSettings()``, so both paths
    share one default source.
    """
    from baldur.settings.replay_automation import get_replay_automation_settings

    settings = get_replay_automation_settings()
    resolved = {
        "on_recovery_enabled": settings.on_recovery_enabled,
        "service_failure_type_map": settings.service_failure_type_map,
    }
    try:
        from baldur.factory.registry import ProviderRegistry

        manager = ProviderRegistry.runtime_config_manager.safe_get()
        if manager is not None:
            rc = manager.get_config("replay_automation") or {}
            for key in resolved:
                if key in rc:
                    resolved[key] = rc[key]
    except Exception as e:
        logger.debug("replay_arming.runtime_config_read_failed", error=str(e))
    return resolved


def _celery_task_importable() -> bool:
    """Whether the on-recovery dispatch task can be imported (Celery extra present)."""
    try:
        from baldur.adapters.celery.tasks import (  # noqa: F401
            conditional_replay_on_circuit_close,
        )

        return True
    except ImportError:
        return False


def _has_registered_handler() -> bool:
    """Whether at least one domain replay handler is registered.

    Without any registered handler every replay resolves to
    ``DefaultReplayHandler`` and fails per-entry, so the loop drains nothing.
    This measures the answering process only, while the sweep runs in the
    Celery worker — register replay handlers in every process, the worker
    included.
    """
    from baldur.services.replay_service.handlers import _replay_handlers

    return len(_replay_handlers) > 0


def _open_circuit_capture_enabled() -> bool:
    """Whether open-circuit rejections are captured into the DLQ.

    The open-circuit sweep needs no failure-type map entry — the circuit that
    closed is the one that rejected those entries, which is the whole
    eligibility test — but with capture off nothing produces the entries it
    would drain.
    """
    from baldur.settings.dlq import get_dlq_settings

    return bool(get_dlq_settings().open_circuit_capture_enabled)


def _broker_connect_timeout_seconds() -> float:
    """Connect timeout the broker client applies before it gives up."""
    from celery import current_app

    configured = current_app.conf.broker_connection_timeout
    if configured is None:
        from kombu import Connection

        configured = Connection.connect_timeout
    return float(configured)


def _probe_budget_seconds() -> float:
    """Upper bound one caller waits for a worker probe.

    ``inspect_timeout`` is how long the broadcast collects replies; the broker
    connect timeout is how long establishing the connection may take before
    that. Their sum is the whole round-trip a caller can pay.
    """
    from baldur.settings.celery_task import get_celery_task_settings

    inspect_timeout = get_celery_task_settings().inspect_timeout
    return float(inspect_timeout) + _broker_connect_timeout_seconds()


def _probe_transport_options(budget: float) -> dict:
    """Socket timeouts for the probe's own connection.

    Both transports' key names are passed: each reads only its own and ignores
    the rest. Without them a half-open socket has no deadline at all and the
    probe thread would outlive the outage that produced it.
    """
    return {
        "socket_timeout": budget,
        "socket_connect_timeout": budget,
        "read_timeout": budget,
        "write_timeout": budget,
    }


def _inspect_active_queues(connection, timeout: float) -> dict:
    """Reply-collecting ``active_queues`` broadcast over ``connection`` only.

    ``control.inspect(connection=...)`` binds only the reply side to the
    supplied connection: the request itself is published through the app's
    producer pool, on a pooled connection that carries no socket deadline and
    republishes forever on a socket that went stale. A mailbox built without a
    producer pool publishes on the very channel it was handed, so both legs of
    the round-trip carry the probe connection's deadlines and a wedged probe
    never holds a pooled connection the dispatch path needs.
    """
    from celery import current_app
    from celery.app.control import flatten_reply

    base = current_app.control.mailbox
    mailbox = type(base)(
        base.namespace,
        type=base.type,
        connection=connection,
        clock=base.clock,
        accept=base.accept,
        serializer=base.serializer,
        producer_pool=None,
        queue_ttl=base.queue_ttl,
        queue_expires=base.queue_expires,
        queue_durable=base.queue_durable,
        queue_exclusive=base.queue_exclusive,
        reply_queue_ttl=base.reply_queue_ttl,
        reply_queue_expires=base.reply_queue_expires,
    )
    replies = mailbox.multi_call("active_queues", timeout=timeout)
    # celery ships no stubs, so flatten_reply is untyped: name its shape here.
    return cast(dict, flatten_reply(replies or []))


def _probe_dlq_worker() -> str:
    """Broker I/O: does any worker consume the ``dlq_processing`` queue?

    Returns "ok" / "missing" / "unknown". Isolated as a module-level function
    so tests patch it wholesale and never touch a live broker. This is the
    expensive I/O; callers read it through :func:`_cached_worker_state`, which
    runs it on a dedicated thread under a deadline.

    Uses a connection of its own rather than the producer pool, for both the
    request and the replies: a probe wedged on an unreachable broker must never
    hold a pooled connection the dispatch path needs to send the replay task.
    """
    try:
        from celery import current_app
    except ImportError:
        # Celery extra absent — the celery_missing link owns that signal; the
        # worker link is simply indeterminate here.
        return _UNKNOWN

    try:
        from baldur.core.process_utils import is_celery_worker_serving
        from baldur.settings.celery_task import get_celery_task_settings

        timeout = get_celery_task_settings().inspect_timeout
        budget = _probe_budget_seconds()
        connection = current_app.connection_for_write(
            connect_timeout=_broker_connect_timeout_seconds(),
            transport_options=_probe_transport_options(budget),
        )
        try:
            active = _inspect_active_queues(connection, timeout)
        finally:
            connection.close()

        if not active:
            # Nobody replied. Outside a worker process that is evidence — no
            # worker is consuming the queue. Inside one it is not: a broadcast
            # sent from a worker always has that worker among its addressees,
            # so silence there means the sender could not hear itself.
            return _UNKNOWN if is_celery_worker_serving() else _MISSING
        for queues in active.values():
            for queue in queues or []:
                if queue.get("name") == _DLQ_QUEUE:
                    return _OK
        # Workers replied and none listed the queue — that is a verdict.
        return _MISSING
    except Exception as e:
        # Broker/inspect error — the state is unverified, never armed.
        logger.debug("replay_arming.worker_probe_broker_unreachable", error=str(e))
        return _UNKNOWN


def _adopt_process_locked() -> None:
    """Drop probe state inherited across ``fork()`` (caller holds the lock).

    A forked child owns none of the parent's threads, so a parent's cached
    verdict is not the child's observation and a parent's in-flight future is
    one nothing in the child will ever resolve.
    """
    global _probe_state_pid, _probe_inflight, _probe_thread, _last_logged_worker_state

    pid = os.getpid()
    if _probe_state_pid == pid:
        return
    _worker_cache.clear()
    _probe_inflight = None
    _probe_thread = None
    _last_logged_worker_state = None
    _probe_state_pid = pid


def _log_worker_state(state: str, previous: str | None, *, event: str) -> None:
    """Log a probe verdict on transition only.

    A deployment with the Celery extra installed and no broker anywhere fails
    every probe forever; warning per probe would be a log flood on a stable
    condition, so repeats drop to DEBUG and recovery is announced once.
    """
    if state == _UNKNOWN:
        if previous == _UNKNOWN:
            logger.debug(event)
        else:
            logger.warning(event)
    elif previous == _UNKNOWN:
        logger.info("replay_arming.worker_probe_recovered", worker_state=state)


def _store_probe_result(state: str, seq: int, ttl: float) -> None:
    """Publish a probe verdict into the TTL cache, unless it was abandoned."""
    global _probe_inflight, _probe_thread, _last_logged_worker_state

    with _worker_cache_lock:
        _adopt_process_locked()
        if seq != _probe_seq:
            # A timed-out waiter already abandoned this attempt; its late
            # result must never overwrite the newer answer.
            return
        _worker_cache[_WORKER_CACHE_KEY] = (time.monotonic() + ttl, state)
        _probe_inflight = None
        _probe_thread = None
        previous = _last_logged_worker_state
        _last_logged_worker_state = state
    _log_worker_state(state, previous, event="replay_arming.worker_probe_failed")


def _abandon_probe(seq: int, ttl: float) -> str:
    """Give up on an in-flight probe: drop it, bump the sequence, cache unknown.

    Bumping is what keeps a wedged attempt from pinning the slot for the life
    of the process: its late result is discarded, and the next miss is free to
    spawn afresh once the abandoned thread has died.
    """
    global _probe_inflight, _probe_seq, _last_logged_worker_state

    with _worker_cache_lock:
        _adopt_process_locked()
        if seq == _probe_seq:
            _probe_inflight = None
            _probe_seq += 1
        _worker_cache[_WORKER_CACHE_KEY] = (time.monotonic() + ttl, _UNKNOWN)
        previous = _last_logged_worker_state
        _last_logged_worker_state = _UNKNOWN
    _log_worker_state(_UNKNOWN, previous, event="replay_arming.worker_probe_timeout")
    return _UNKNOWN


def _run_worker_probe(future: Future, seq: int, ttl: float) -> None:
    """Thread target: probe the broker and publish the verdict exactly once.

    The whole body is one guarded block on purpose — ``threading`` gives a
    target no error channel, and an exception escaping here would leave every
    waiter blocked to its own budget on a future nobody resolves.
    """
    try:
        state = _probe_dlq_worker()
        _store_probe_result(state, seq, ttl)
        if not future.done():
            future.set_result(state)
    except Exception as e:
        logger.warning("replay_arming.worker_probe_failed", error=str(e))
        if not future.done():
            future.set_result(_UNKNOWN)


def _start_probe_locked(ttl: float) -> Future[str]:
    """Spawn this process's one probe thread (caller holds the lock)."""
    global _probe_inflight, _probe_thread

    future: Future[str] = Future()
    thread = threading.Thread(
        target=_run_worker_probe,
        args=(future, _probe_seq, ttl),
        name=_PROBE_THREAD_NAME,
        daemon=True,
    )
    _probe_inflight = future
    _probe_thread = thread
    thread.start()
    return future


def _cached_worker_state() -> str:
    """Return the worker-presence state, cached behind a short TTL.

    The round-trip runs on a dedicated daemon thread because it is not
    otherwise bounded: on a pooled connection that went stale after the process
    connected, the broker client republishes forever rather than raising. Every
    caller waits at most ``inspect_timeout`` plus the broker connect timeout,
    and concurrent callers share the one in-flight probe instead of each
    starting another.
    """
    from baldur.settings.celery_task import get_celery_task_settings

    ttl = float(get_celery_task_settings().worker_status_cache_ttl_seconds)
    budget = _probe_budget_seconds()

    now = time.monotonic()
    with _worker_cache_lock:
        _adopt_process_locked()
        cached = _worker_cache.get(_WORKER_CACHE_KEY)
        if cached is not None and cached[0] > now:
            return cached[1]
        future = _probe_inflight
        if future is None:
            if _probe_thread is not None and _probe_thread.is_alive():
                # An abandoned attempt is still wedged in the broker call.
                # Spawn nothing, and cache nothing: this is a non-observation,
                # and the first miss after that thread dies must probe afresh.
                return _UNKNOWN
            future = _start_probe_locked(ttl)
        seq = _probe_seq

    try:
        return future.result(timeout=budget)
    except FutureTimeoutError:
        return _abandon_probe(seq, ttl)


def get_worker_cache() -> dict[str, tuple[float, str]]:
    """Return a snapshot of the worker-presence TTL cache (read accessor)."""
    with _worker_cache_lock:
        _adopt_process_locked()
        return dict(_worker_cache)


def reset_worker_cache(*, log_state: bool = False) -> None:
    """Invalidate the worker-presence cache.

    Called on every dispatch attempt, so the next evaluation re-checks the
    broker instead of serving an entry that attempt just contradicted, and by
    test fixtures. A probe still in flight is left joinable on purpose: it is
    itself the re-check the reset asks for, and dropping it would turn the
    next evaluation into a non-observation for the rest of that round-trip.
    Only a waiter's own deadline abandons an attempt. ``log_state``
    additionally clears the transition memory that keeps repeated probe
    failures at DEBUG — test isolation only, since clearing it in production
    would re-warn once per circuit close.
    """
    global _last_logged_worker_state

    with _worker_cache_lock:
        _adopt_process_locked()
        _worker_cache.clear()
        if log_state:
            _last_logged_worker_state = None


def record_dispatch_outcome(
    outcome: str, *, service_name: str, error: str | None = None
) -> None:
    """Record one on-recovery dispatch evaluation (fail-open).

    Counts every outcome, but keeps a ledger entry only for outcomes that
    actually called the task — an evaluation that never dispatched is not a
    dispatch, and reporting one as the last one would name an attempt that
    never happened. Both such outcomes are already visible as links.

    Every attempt also invalidates the worker cache so the next evaluation
    re-checks the broker: after a failure the cached state is a verdict the
    attempt just contradicted, and after a success a cached "unknown" left by
    an earlier probe timeout would otherwise sit beside a successful dispatch
    in the same answer.
    """
    global _dispatch_record

    if outcome in _DISPATCH_ATTEMPT_OUTCOMES:
        pid = os.getpid()
        with _dispatch_lock:
            previous = _dispatch_record
            prior_failures = (
                previous.consecutive_failures
                if previous is not None and previous.pid == pid
                else 0
            )
            _dispatch_record = DispatchRecord(
                outcome=outcome,
                at=utc_now(),
                service_name=service_name,
                error=error,
                consecutive_failures=prior_failures + 1 if outcome == "error" else 0,
                pid=pid,
            )
        reset_worker_cache()

    try:
        from baldur.metrics.prometheus import get_metrics

        recorder = getattr(get_metrics(), "dlq", None)
        if recorder is not None:
            recorder.record_replay_dispatch(outcome)
    except Exception:
        pass


def get_dispatch_ledger() -> DispatchRecord | None:
    """Return this process's last dispatch attempt, or None if it made none."""
    with _dispatch_lock:
        record = _dispatch_record
    if record is not None and record.pid != os.getpid():
        # Inherited across fork() — not this process's observation.
        return None
    return record


def reset_dispatch_ledger() -> None:
    """Clear the dispatch ledger (test isolation)."""
    global _dispatch_record

    with _dispatch_lock:
        _dispatch_record = None


def _lane_path(links: dict[str, str], lane: str) -> list[str]:
    """Links this lane depends on, in global order, that were evaluated."""
    lane_link = _LANE_LINKS[lane]
    return [
        key
        for key in _LINK_ORDER
        if key in links and (key in _SHARED_LINK_ORDER or key == lane_link)
    ]


def _fold_lane(links: dict[str, str], path: list[str]) -> LaneStatus:
    """Fold one lane's path: verified armed, verified missing, or unverified."""
    missing = [key for key in path if links.get(key) == _MISSING]
    if missing:
        return LaneStatus(armed=False, link=missing[0])
    unknown = [key for key in path if links.get(key) == _UNKNOWN]
    if unknown:
        return LaneStatus(armed=None, link=unknown[0])
    return LaneStatus(armed=True, link=None)


def _finalize(links: dict[str, str]) -> ArmingStatus:
    """Derive the per-lane verdicts and the any-lane headline from link states."""
    lanes = {lane: _fold_lane(links, _lane_path(links, lane)) for lane in _LANE_ORDER}
    verdicts = [lane.armed for lane in lanes.values()]

    if any(verdict is True for verdict in verdicts):
        armed: bool | None = True
    elif all(verdict is False for verdict in verdicts):
        armed = False
    else:
        armed = None

    missing_links = (
        [key for key in _LINK_ORDER if links.get(key) == _MISSING]
        if armed is False
        else []
    )
    unverified_link = None
    if armed is None:
        unverified = {
            lane.link for lane in lanes.values() if lane.armed is None and lane.link
        }
        unverified_link = next((key for key in _LINK_ORDER if key in unverified), None)

    return ArmingStatus(
        armed=armed,
        missing_link=missing_links[0] if missing_links else None,
        missing_links=missing_links,
        unverified_link=unverified_link,
        links=links,
        lanes=lanes,
    )


def _evaluate() -> ArmingStatus:
    """Evaluate every link in order and fold the lanes."""
    links: dict[str, str] = {}

    config = _resolve_replay_config()

    # 1. disabled — hard prerequisite for everything below.
    if not config.get("on_recovery_enabled", True):
        links["disabled"] = _MISSING
        return _finalize(links)
    links["disabled"] = _OK

    # 2. celery_missing — needs enabled.
    if not _celery_task_importable():
        links["celery_missing"] = _MISSING
        return _finalize(links)
    links["celery_missing"] = _OK

    # 3. worker_missing — broker I/O (cached), shared by both lanes.
    links["worker_missing"] = _cached_worker_state()

    # 4. handler_missing — non-I/O, shared by both lanes.
    links["handler_missing"] = _OK if _has_registered_handler() else _MISSING

    # 5. map_unconfigured — the mapped sweep's own prerequisite. Retry-exhaustion
    #    captures carry failure types only the map can select.
    links["map_unconfigured"] = (
        _OK if config.get("service_failure_type_map") else _MISSING
    )

    # 6. open_circuit_capture_disabled — the open-circuit sweep's own
    #    prerequisite. That lane consults no map, but with capture off nothing
    #    produces the entries it would drain.
    links["open_circuit_capture_disabled"] = (
        _OK if _open_circuit_capture_enabled() else _MISSING
    )

    return _finalize(links)


def _set_gauge(armed: bool | None) -> None:
    """Update the Prometheus armed gauge (fail-open).

    Folds the tri-state here so the recorder keeps its boolean contract: 1 only
    when a lane verified every prerequisite, 0 for both "a prerequisite is
    missing" and "a prerequisite could not be verified". An unverifiable
    guarantee is not a delivered one.
    """
    try:
        from baldur.metrics.prometheus import get_metrics

        recorder = getattr(get_metrics(), "dlq", None)
        if recorder is not None:
            recorder.set_auto_replay_armed(armed is True)
    except Exception:
        pass


def get_on_recovery_arming_status() -> ArmingStatus:
    """Full on-demand arming probe (includes the cached worker I/O link).

    Fail-open: any unexpected error resolves to a ``probe_failed`` status
    rather than raising, so the operator surfaces never 500. Sets the armed
    gauge as a side effect.
    """
    last_dispatch = None
    try:
        last_dispatch = get_dispatch_ledger()
        status = replace(_evaluate(), last_dispatch=last_dispatch)
    except Exception as e:
        logger.warning("replay_arming.probe_failed", error=str(e))
        status = ArmingStatus.probe_failed(last_dispatch=last_dispatch)
    _set_gauge(status.armed)
    return status


def refresh_armed_gauge() -> None:
    """Recompute arming and update the armed gauge.

    Driven by the per-process metric collector so the scrape surface tracks the
    same verdict a REST poll would return. A failed evaluation writes 0 rather
    than leaving a previous 1 in place: a stale positive claim is the one
    outcome this gauge must never produce.
    """
    try:
        status = _evaluate()
        _set_gauge(status.armed)
    except Exception as e:
        logger.debug("replay_arming.refresh_gauge_failed", error=str(e))
        _set_gauge(None)
