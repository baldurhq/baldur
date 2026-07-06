"""On-recovery auto-replay arming probe.

Answers "is the event-driven DLQ auto-replay loop armed right now, and if not,
which prerequisite is missing?" as a single on-demand evaluation. This probe is
the single source of truth behind three operator surfaces: the Prometheus
``baldur_dlq_auto_replay_armed`` gauge, the ``GET /dlq/cleanup/stats``
``auto_replay`` block, and the console armed/disarmed badge.

Link evaluation order (first missing wins for the headline)::

    pro_absent -> disabled -> celery_missing -> worker_missing
                -> map_unconfigured -> handler_missing

``pro_absent`` / ``disabled`` / ``celery_missing`` are hard prerequisites: once
one is missing the downstream links are not evaluated. ``worker_missing`` (a
broker round-trip, cached), ``map_unconfigured`` and ``handler_missing`` are
independent leaf checks evaluated together, so ``missing_links`` may carry more
than one of them at once.

The name is deliberately NOT ``health_check`` / ``is_healthy`` / ``check_health``
— this is configuration completeness, not component health.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()

__all__ = [
    "ArmingStatus",
    "get_on_recovery_arming_status",
    "refresh_armed_gauge",
    "get_worker_cache",
    "reset_worker_cache",
]

# Queue the on-recovery replay task is pinned to (see celery_tasks.dlq_tasks).
_DLQ_QUEUE = "dlq_processing"

# Link evaluation order — first missing wins for the headline ``missing_link``.
_LINK_ORDER = (
    "pro_absent",
    "disabled",
    "celery_missing",
    "worker_missing",
    "map_unconfigured",
    "handler_missing",
)

# Worker-presence probe cache: the broker round-trip is bounded by
# ``inspect_timeout`` and cached for ``worker_status_cache_ttl_seconds`` so the
# console's periodic stats polling does not pay a broker round-trip on each
# request. Guarded by a lock so concurrent polls share one broker call.
_worker_cache_lock = threading.Lock()
_worker_cache: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True)
class ArmingStatus:
    """Immutable result of an on-recovery arming evaluation.

    Attributes:
        armed: True when every evaluable link is satisfied; False when a link
            is missing; None when the probe itself failed (indeterminate).
        missing_link: The first missing link in ``_LINK_ORDER`` (headline), or
            None when armed.
        missing_links: Every evaluable-and-missing link, in order.
        links: Full per-link state map ("ok" / "missing" / "unknown" /
            "unevaluated").
    """

    armed: bool | None
    missing_link: str | None
    missing_links: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)

    @classmethod
    def probe_failed(cls) -> ArmingStatus:
        """Fail-open sentinel — the probe raised, so arming is indeterminate."""
        return cls(
            armed=None,
            missing_link="probe_failed",
            missing_links=["probe_failed"],
            links={},
        )


def _resolve_dlq_service() -> object | None:
    """Resolve the PRO DLQ service slot (None = PRO absent / entitlement off)."""
    try:
        from baldur.factory.registry import ProviderRegistry

        return ProviderRegistry.dlq_service.safe_get()
    except Exception:
        return None


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
    """
    from baldur.services.replay_service.handlers import _replay_handlers

    return len(_replay_handlers) > 0


def _probe_dlq_worker() -> str:
    """Broker I/O: does any worker consume the ``dlq_processing`` queue?

    Returns "ok" / "missing" / "unknown". Isolated as a module-level function
    so tests patch it wholesale and never touch a live broker. This is the
    expensive I/O; callers read it through :func:`_cached_worker_state`.
    """
    try:
        from celery import current_app
    except ImportError:
        # Celery extra absent — the celery_missing link owns that signal; the
        # worker link is simply indeterminate here.
        return "unknown"

    try:
        from baldur.settings.celery_task import get_celery_task_settings

        timeout = get_celery_task_settings().inspect_timeout
        inspect = current_app.control.inspect(timeout=timeout)
        active = inspect.active_queues()
        if not active:
            # No worker replied — none is consuming the queue.
            return "missing"
        for queues in active.values():
            for queue in queues or []:
                if queue.get("name") == _DLQ_QUEUE:
                    return "ok"
        return "missing"
    except Exception as e:
        # Broker/inspect error — fail open to indeterminate rather than
        # declaring the loop disarmed on a transient hiccup.
        logger.debug("replay_arming.worker_probe_failed", error=str(e))
        return "unknown"


def _cached_worker_state() -> str:
    """Return the worker-presence state, cached behind a short TTL."""
    try:
        from baldur.settings.celery_task import get_celery_task_settings

        ttl = get_celery_task_settings().worker_status_cache_ttl_seconds
    except Exception:
        ttl = 15

    monotonic = time.monotonic()
    with _worker_cache_lock:
        cached = _worker_cache.get("state")
        if cached is not None and cached[0] > monotonic:
            return cached[1]

    state = _probe_dlq_worker()
    with _worker_cache_lock:
        _worker_cache["state"] = (monotonic + ttl, state)
    return state


def get_worker_cache() -> dict[str, tuple[float, str]]:
    """Return a snapshot of the worker-presence TTL cache (read accessor)."""
    with _worker_cache_lock:
        return dict(_worker_cache)


def reset_worker_cache() -> None:
    """Clear the worker-presence TTL cache (test isolation)."""
    with _worker_cache_lock:
        _worker_cache.clear()


def _finalize(links: dict[str, str]) -> ArmingStatus:
    """Derive the headline / array / armed flag from a link-state map."""
    missing_links = [key for key in _LINK_ORDER if links.get(key) == "missing"]
    missing_link = missing_links[0] if missing_links else None
    return ArmingStatus(
        armed=missing_link is None,
        missing_link=missing_link,
        missing_links=missing_links,
        links=links,
    )


def _evaluate(check_worker: bool) -> ArmingStatus:
    """Evaluate all links in order. ``check_worker`` gates the broker I/O link."""
    links: dict[str, str] = {}

    # 1. pro_absent — hard prerequisite for everything below.
    if _resolve_dlq_service() is None:
        links["pro_absent"] = "missing"
        return _finalize(links)
    links["pro_absent"] = "ok"

    config = _resolve_replay_config()

    # 2. disabled — needs PRO present.
    if not config.get("on_recovery_enabled", True):
        links["disabled"] = "missing"
        return _finalize(links)
    links["disabled"] = "ok"

    # 3. celery_missing — needs PRO + enabled.
    if not _celery_task_importable():
        links["celery_missing"] = "missing"
        return _finalize(links)
    links["celery_missing"] = "ok"

    # 4. worker_missing — broker I/O (cached); only when check_worker.
    if check_worker:
        links["worker_missing"] = _cached_worker_state()
    else:
        links["worker_missing"] = "unevaluated"

    # 5. map_unconfigured — non-I/O, independent of the worker link.
    links["map_unconfigured"] = (
        "ok" if config.get("service_failure_type_map") else "missing"
    )

    # 6. handler_missing — non-I/O, independent of the worker link.
    links["handler_missing"] = "ok" if _has_registered_handler() else "missing"

    return _finalize(links)


def _set_gauge(armed: bool | None) -> None:
    """Update the Prometheus armed gauge (fail-open; None leaves it unchanged)."""
    if armed is None:
        return
    try:
        from baldur.metrics.prometheus import get_metrics

        recorder = getattr(get_metrics(), "dlq", None)
        if recorder is not None:
            recorder.set_auto_replay_armed(armed)
    except Exception:
        pass


def get_on_recovery_arming_status() -> ArmingStatus:
    """Full on-demand arming probe (includes the cached worker I/O link).

    Fail-open: any unexpected error resolves to a ``probe_failed`` status
    rather than raising, so the operator surfaces never 500. Sets the armed
    gauge as a side effect.
    """
    try:
        status = _evaluate(check_worker=True)
    except Exception as e:
        logger.warning("replay_arming.probe_failed", error=str(e))
        status = ArmingStatus.probe_failed()
    _set_gauge(status.armed)
    return status


def refresh_armed_gauge(check_worker: bool = False) -> None:
    """Recompute arming (non-I/O by default) and update the armed gauge.

    Used at ``baldur.init()`` startup, where the worker ping is skipped so init
    stays non-blocking.
    """
    try:
        status = _evaluate(check_worker=check_worker)
        _set_gauge(status.armed)
    except Exception as e:
        logger.debug("replay_arming.refresh_gauge_failed", error=str(e))
