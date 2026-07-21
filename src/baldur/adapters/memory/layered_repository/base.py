"""
Layered Repository Base Class.

Provides the base class with initialization and configuration.
L2 store I/O is isolated from request threads by a process-wide bounded
``ThreadPoolExecutor`` (``BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS``, default 16)
with ``future.result(timeout=...)`` — a thread-pool bulkhead that bounds
concurrency, caps each call, and keeps slow or failing L2 operations off the
caller thread.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from baldur.adapters.memory.drift_reconciliation import (
    DriftReconciler,
    get_drift_reconciler,
)
from baldur.adapters.memory.shadow_logger import get_shadow_logger
from baldur.interfaces.repositories import (
    CircuitBreakerStateRepository,
)

logger = structlog.get_logger()

# 479 D2: dunder-prefixed sentinel service name reserves the baldur internal
# namespace; operator service names empirically use domain-style names
# (no dunder collision). Used by _ensure_l2_warmup_once to drive a full-path
# L2 warmup (executor thread + connection pool + ResilientStorageBackend
# wrapper + Lua eval RTT) at LayeredRepository construction time.
_WARMUP_SENTINEL_SERVICE_NAME = "__baldur_init_warmup__"


def _default_metrics() -> dict[str, float]:
    """Return a fresh zeroed dict of the local metric counters.

    Single source of truth for the counter key set so ``__init__`` and
    ``reset_metrics`` cannot drift — the previously duplicated 6-key literal
    lived in both base.py and monitoring.py and had to be edited in two
    places whenever the key set changed.
    """
    return {
        "l2_timeout_count": 0,
        "l2_sync_failure_count": 0,
        "l2_sync_success_count": 0,
        "l2_latency_total_ms": 0.0,
        "l2_latency_count": 0,
        "drift_reconciliation_count": 0,
    }


class LayeredRepositoryBase:
    """
    Base class for Layered Repository.

    Provides initialization, configuration, and executor management.
    L2 store I/O runs on a process-wide bounded ``ThreadPoolExecutor`` with
    ``future.result(timeout=...)`` so slow or failing L2 operations cannot
    block the request threads that drive ``record_*`` / read operations.
    """

    if TYPE_CHECKING:
        # Host contract — `_load_from_l2_with_timeout` is provided by
        # L2LoadMixin which is mixed into the assembled
        # `LayeredCircuitBreakerStateRepository`. The base calls it from
        # __init__ (intentional template-method pattern).
        def _load_from_l2_with_timeout(self) -> None: ...

    # ThreadPoolExecutor for async L2 operations with timeout
    _executor: ThreadPoolExecutor | None = None
    _executor_lock = threading.Lock()

    # 479 D2: process-wide L2 warmup state. Once-per-process gate via
    # double-checked locking on _warmup_done. Mirrors the existing
    # _executor / _executor_lock ClassVar pattern.
    _warmup_done: bool = False
    _warmup_lock = threading.Lock()

    @classmethod
    def _get_executor(cls) -> ThreadPoolExecutor:
        """Get or create shared ThreadPoolExecutor.

        Read/write always targets ``LayeredRepositoryBase`` directly so the
        pool is a true process-wide singleton — using ``cls`` would create
        per-subclass attributes that shadow the Base when written, breaking
        ``reset_layered_repository_executor()`` (which only clears Base).
        Mirrors the ``_warmup_done`` ClassVar pattern below.
        """
        if LayeredRepositoryBase._executor is None:
            with LayeredRepositoryBase._executor_lock:
                if LayeredRepositoryBase._executor is None:
                    # 478 D3: pool size driven by L2StorageSettings env var
                    # BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS (default 16).
                    # Lazy-imported here to avoid a settings import at base.py
                    # module load time.
                    try:
                        from baldur.settings.l2_storage import (
                            get_l2_storage_settings,
                        )

                        max_workers = get_l2_storage_settings().executor_max_workers
                    except Exception:
                        max_workers = 16
                    LayeredRepositoryBase._executor = ThreadPoolExecutor(
                        max_workers=max_workers, thread_name_prefix="l2_sync"
                    )
        return LayeredRepositoryBase._executor

    def __init__(
        self,
        l2_repo: CircuitBreakerStateRepository | None = None,
        sync_interval_seconds: float = 5.0,
        adapter_type: str = "unknown",
        drift_reconciler: DriftReconciler | None = None,
    ):
        """
        Args:
            l2_repo: L2 store (Redis, Django DB, etc.). L1 only when None.
            sync_interval_seconds: L2 sync interval (seconds)
            adapter_type: L2 adapter type (redis, django, etc.) — used to
                pick the timeout
            drift_reconciler: drift reconciliation instance. Falls back to the
                default instance when None.
        """
        # Lazy import to avoid circular dependency
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )

        self._l1 = InMemoryCircuitBreakerStateRepository()
        self._l2 = l2_repo
        self._sync_interval = sync_interval_seconds
        self._adapter_type = adapter_type
        self._last_sync_time: datetime | None = None
        self._shadow_logger = get_shadow_logger()
        self._drift_reconciler = drift_reconciler or get_drift_reconciler()

        # L2 connection state tracking. Guarded by self._lock so the
        # failure-count read-modify-write and the healthy<->quarantined
        # transition decision from <=16 concurrent l2_sync executor threads
        # stay atomic, and multi-field admin reads observe a consistent
        # snapshot. A plain Lock (not RLock) is used deliberately: no
        # self._lock-holding method re-acquires it — side-effects fire after
        # release and admin readers snapshot-then-release — so an accidental
        # nested acquire should surface as an immediate deadlock in test
        # rather than silently nesting (mirrors the _metrics_lock choice).
        self._lock = threading.Lock()
        self._l2_healthy = True
        self._l2_last_error_time: datetime | None = None
        self._l2_consecutive_failures = 0
        self._l2_was_unhealthy = False  # Used to detect L2 recovery

        # Local metric counters (used until Prometheus integration). Guarded by
        # a dedicated lock so the read-modify-write increments from ≤16
        # concurrent l2_sync executor threads do not lose updates, and snapshot
        # reads/reset observe a consistent set. Kept separate from the
        # quarantine-state self._lock (above) so the two critical sections stay
        # disjoint and never-nested, avoiding any lock-ordering surface.
        self._metrics = _default_metrics()
        self._metrics_lock = threading.Lock()

        # Initial load when an L2 store is configured
        if self._l2:
            self._load_from_l2_with_timeout()
            # 479 D2: process-wide L2 warmup (executor threads + connection
            # pool + ResilientStorageBackend wrapper + Lua eval RTT).
            # Idempotent: only the first redis-l2 LayeredRepository in a
            # process pays the cost; subsequent constructions short-circuit
            # via the _warmup_done ClassVar.
            self._ensure_l2_warmup_once()

    def _ensure_l2_warmup_once(self) -> None:
        """Once-per-process L2 warmup gate (479 D2).

        Drives a full-path L2 warmup so the first real burst does not pay
        cumulative thread-spawn + TCP-handshake + ResilientStorageBackend-init
        cost. Skips for non-redis adapters (Cat 6.4 specifically gates the
        redis path) and for repositories without an L2 store.

        Idempotent: subsequent constructions short-circuit on the unlocked
        _warmup_done check; the lock guards the once-per-process race only.
        Test isolation: tests can call ``_reset_warmup_state()`` to re-arm
        the gate.

        Fail-open: any exception during warmup is logged and swallowed.
        ``_warmup_done`` is set to True even on failure — do not retry from
        the constructor; subsequent real calls go through the normal
        ``_l2_healthy`` quarantine pathway.
        """
        if not self._l2 or self._adapter_type.lower() != "redis":
            return

        # Double-checked locking: unlocked fast-path for steady state,
        # locked slow-path for first-construction race.
        if LayeredRepositoryBase._warmup_done:
            return
        with LayeredRepositoryBase._warmup_lock:
            if LayeredRepositoryBase._warmup_done:
                return
            try:
                self._do_l2_warmup()
            except Exception as e:
                logger.warning(
                    "layered_repo.l2_warmup_failed_continuing",
                    adapter_type=self._adapter_type,
                    error=str(e),
                )
            finally:
                # Set even on failure to avoid retry storms from the
                # constructor; the normal call path takes over.
                LayeredRepositoryBase._warmup_done = True

    def _do_l2_warmup(self) -> None:
        """Perform the actual L2 warmup work (479 D2).

        Submits ``executor_max_workers`` concurrent ``try_acquire_half_open_slot``
        calls coordinated by a ``threading.Barrier`` so no submission completes
        before all threads spawn. This forces the executor to spawn all
        worker threads up to ``max_workers`` (CPython
        ``ThreadPoolExecutor._adjust_thread_count`` reuses idle threads
        first; the barrier holds them busy). Connection pool slots and the
        Lua-eval round-trip are warmed in the same single window.

        Final ``delete_state`` cleanup is defense-in-depth — under the
        current Lua script the closed→closed branch does NOT ``HSET``, so
        no sentinel key is created. Recorded as OOS#479-1.
        """
        import time as _time  # local import: scoped to perf measurement

        # Caller invariant: _ensure_l2_warmup_once gates this with `if self._l2`.
        assert self._l2 is not None

        redis_timeout = self._get_timeout_seconds()
        # Mirror _load_from_l2_with_timeout: init-time budget = 2 × steady-state.
        barrier_timeout = 2 * redis_timeout

        executor = self._get_executor()
        max_workers = executor._max_workers

        barrier = threading.Barrier(parties=max_workers, timeout=barrier_timeout)
        start = _time.perf_counter()

        def warmup_worker():
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                # A peer crashed or the main thread aborted; exit cleanly.
                return
            # All threads have spawned; pay the L2 round-trip in parallel.
            self._l2.try_acquire_half_open_slot(
                _WARMUP_SENTINEL_SERVICE_NAME,
                1,
                1,
            )

        futures = []
        try:
            for _ in range(max_workers):
                try:
                    futures.append(executor.submit(warmup_worker))
                except Exception:
                    # Submit-time failure: release any already-waiting threads
                    # with BrokenBarrierError instead of stranding them.
                    barrier.abort()
                    raise

            # Drain futures with the same budget the barrier already enforces.
            for f in futures:
                try:
                    f.result(timeout=barrier_timeout)
                except Exception:
                    # Per-worker failures are absorbed; outer try/except in
                    # _ensure_l2_warmup_once handles the catastrophic path.
                    pass
        finally:
            # Defense-in-depth cleanup: under the current Lua script the
            # sentinel key is never written (closed→closed no-op), but if
            # a future Lua change starts writing on that branch, this
            # prevents key accumulation. DEL on a missing key is ~0.1 ms.
            try:
                self._l2.delete_state(_WARMUP_SENTINEL_SERVICE_NAME)
            except Exception:
                pass

        elapsed_ms = (_time.perf_counter() - start) * 1000
        logger.debug(
            "layered_repo.l2_warmup_completed",
            elapsed_ms=elapsed_ms,
            max_workers=max_workers,
            adapter_type=self._adapter_type,
        )

    @classmethod
    def _reset_warmup_state(cls) -> None:
        """Reset the once-per-process warmup gate. Test-only."""
        LayeredRepositoryBase._warmup_done = False

    def _get_timeout_seconds(self) -> float:
        """Return the timeout (seconds) for the current adapter type."""
        try:
            from baldur.settings.l2_storage import get_l2_storage_runtime_config

            config = get_l2_storage_runtime_config()
            return config.get_timeout_for_adapter(self._adapter_type)
        except ImportError:
            # 478 D1: post-fix the runtime-config path is the normal branch.
            # The fallback dict below is reached only on a bootstrap-order
            # ImportError — log a warning so the silent regression to a
            # hardcoded ceiling is observable.
            logger.warning(
                "layered_repo.runtime_config_import_failed",
                adapter_type=self._adapter_type,
            )
            timeouts = {
                "redis": 1.0,  # 1000ms (479 D1)
                "database": 0.2,  # 200ms
                "django": 0.2,  # 200ms
            }
            return timeouts.get(self._adapter_type.lower(), 0.1)

    def _incr_metrics(self, **deltas: float) -> None:
        """Atomically apply one or more counter deltas under the metrics lock.

        A single multi-field update keeps related counters mutually
        consistent for snapshot readers — e.g. the success path increments
        ``l2_sync_success_count``, ``l2_latency_total_ms``, and
        ``l2_latency_count`` together so a reader deriving the average latency
        never observes a torn ``(total_ms, count)`` pair.

        Only ever called with keys present in ``_default_metrics()``, so a
        ``KeyError`` is impossible. ``: float`` accepts the int deltas
        (``int`` is compatible with ``float`` in typing).
        """
        with self._metrics_lock:
            for key, delta in deltas.items():
                self._metrics[key] += delta
