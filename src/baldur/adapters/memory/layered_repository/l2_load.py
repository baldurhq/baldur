"""
L2 Load Operations Mixin.

Provides methods for loading data from L2 storage.
"""

from __future__ import annotations

import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

import structlog

from baldur.adapters.memory.base import _now

if TYPE_CHECKING:
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime

    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )
    from baldur.interfaces.repositories import CircuitBreakerStateRepository

logger = structlog.get_logger()


class L2LoadMixin:
    """Mixin providing L2 load operations."""

    if TYPE_CHECKING:
        # Host contract — attributes redeclared with the canonical
        # types to keep MRO inference consistent with
        # LayeredRepositoryBase, MonitoringMixin, and ErrorHandlingMixin
        # (otherwise mypy infers narrower types from the assignments
        # below — `_now()` -> datetime, `True` -> bool literal, etc.).
        _last_sync_time: datetime | None
        _l2_healthy: bool
        _l2_consecutive_failures: int
        _l1: InMemoryCircuitBreakerStateRepository
        _l2: CircuitBreakerStateRepository | None
        _metrics: dict[str, float]
        _lock: threading.Lock

        def _get_timeout_seconds(self) -> float: ...
        def _get_executor(self) -> ThreadPoolExecutor: ...
        def _incr_metrics(self, **deltas: float) -> None: ...
        def _handle_l2_timeout(
            self, operation: str, service_name: str | None
        ) -> None: ...
        def _handle_l2_error(
            self,
            operation: str,
            service_name: str | None,
            error: Exception,
            intended_state: str = "",
        ) -> None: ...

    def _load_from_l2_with_timeout(self) -> None:
        """Initial data load from L2 into L1 (timeout applied)."""
        if not self._l2:
            return

        timeout = self._get_timeout_seconds() * 2  # double timeout for the initial load
        start_time = time.perf_counter()

        try:
            executor = self._get_executor()
            future = executor.submit(self._l2.get_all_states)
            all_states = future.result(timeout=timeout)

            for state in all_states:
                self._l1.get_or_create(state.service_name)
                self._l1.update_state(
                    service_name=state.service_name,
                    state=state.state,
                    failure_count=state.failure_count,
                    success_count=state.success_count,
                    opened_at=state.opened_at,
                )

            self._last_sync_time = _now()
            # Reset the quarantine quad under self._lock (D6) so the
            # healthy/count pair flips atomically — `force_sync_from_l2()`
            # reaches this from the admin thread post-construction, racing the
            # executor-thread failure handlers. `_last_sync_time` is a single
            # reference field (not part of the quad) and stays unlocked.
            with self._lock:
                self._l2_healthy = True
                self._l2_consecutive_failures = 0

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self._incr_metrics(
                l2_latency_total_ms=elapsed_ms,
                l2_latency_count=1,
            )

            logger.info(
                "layered_repo.initial_load_completed_states",
                all_states_count=len(all_states),
                elapsed_ms=elapsed_ms,
            )

        except FuturesTimeoutError:
            self._handle_l2_timeout("initial_load", None)
            logger.warning(
                "layered_repo.initial_load_timeout_ms",
                timeout_ms=timeout * 1000,
            )
        except Exception as e:
            self._handle_l2_error("initial_load", None, e)
            logger.warning(
                "layered_repo.initial_load_failed_starting",
                error=e,
            )

    def _load_from_l2(self) -> None:
        """Initial data load from L2 into L1 (legacy, no timeout)."""
        self._load_from_l2_with_timeout()
