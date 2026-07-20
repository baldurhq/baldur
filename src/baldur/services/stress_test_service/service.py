"""
Stress Test Service.

Business logic for DB connection pool stress testing.

This module is test-only; never use it in production!
Business logic is separated from the View layer to keep the architecture clean.

Note:
- Views (stress_views.py) handle request/response only
- Actual DB operations, lock tests, and pool management live in this service
"""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING

import structlog

from .models import (
    BurstFailureResult,
    LockContentionResult,
    PoolStatusResult,
    StressTestResult,
)

if TYPE_CHECKING:
    from baldur.interfaces.pg_admin import PgAdminProvider

logger = structlog.get_logger()

# Import used to query SQLAlchemy pool state
try:
    from sqlalchemy.exc import TimeoutError as SATimeoutError
    from sqlalchemy.pool import QueuePool  # noqa: F401

    SQLALCHEMY_AVAILABLE = True
except ImportError:
    SQLALCHEMY_AVAILABLE = False

    class SATimeoutError(Exception):  # type: ignore[no-redef]
        """Fallback when SQLAlchemy is not installed; never raised."""


# =============================================================================
# Stress Test Service
# =============================================================================


class StressTestService:
    """
    Stress test service.

    Encapsulates DB connection pool test logic.
    Raw SQL is encapsulated behind PgAdminProvider.
    """

    # Class variable holding the currently occupied connections
    _held_connections: list = []
    _held_connections_lock: threading.Lock | None = None

    def __init__(self, repository: PgAdminProvider | None = None):
        """
        Initialize the service.

        Args:
            repository: PgAdminProvider instance (registry default if omitted)
        """
        if StressTestService._held_connections_lock is None:
            StressTestService._held_connections_lock = threading.Lock()

        if repository is None:
            from baldur.factory import ProviderRegistry

            self._repo = ProviderRegistry.pg_admin.get()
        else:
            self._repo = repository

    # =========================================================================
    # Pool Information
    # =========================================================================

    def get_pool_info(self) -> dict:
        """Query SQLAlchemy pool information.

        Delegates to the active provider of ProviderRegistry.pool_info.
        """
        try:
            from baldur.factory import ProviderRegistry

            result = ProviderRegistry.pool_info.get().get_pool_info()
            if not result:
                return {
                    "pool_type": "django_default",
                    "note": "No SQLAlchemy pool detected",
                }
            return result
        except Exception as e:
            return {"pool_type": "unknown", "error": str(e)}

    def get_pool_status(self) -> PoolStatusResult:
        """Query the current connection pool state."""
        try:
            # Try SQLAlchemy pool info first
            pool_info = self.get_pool_info()

            from baldur.factory import ProviderRegistry

            db_provider = ProviderRegistry.database_health.get()
            conn_info = db_provider.check_connection("default")

            # Query PostgreSQL connection stats (via repository)
            stats = self._repo.get_connection_stats()

            is_exhausted = pool_info.get("pool_exhausted", False)

            return PoolStatusResult(
                status="exhausted" if is_exhausted else "healthy",
                sqlalchemy_pool=pool_info,
                pg_stats={
                    "total_connections": stats.total_connections,
                    "active": stats.active,
                    "idle": stats.idle,
                    "idle_in_transaction": stats.idle_in_transaction,
                },
                connection_usable=conn_info.is_usable,
                use_connection_pool=os.getenv("USE_CONNECTION_POOL", "FALSE") == "TRUE",
            )
        except SATimeoutError as e:
            logger.exception(
                "stress_test_service.pool_exhausted_timeouterror",
                error=e,
            )
            return PoolStatusResult(
                status="exhausted",
                error="Connection pool exhausted",
                error_type="SQLAlchemy TimeoutError",
            )
        except Exception as e:
            logger.exception(
                "stress_test_service.failed",
                error=e,
            )
            return PoolStatusResult(
                status="error",
                error=str(e),
            )

    # =========================================================================
    # Slow Query Tests
    # =========================================================================

    def execute_slow_query(self, seconds: int) -> StressTestResult:
        """Run a slow query that holds a DB connection for the given duration."""
        start = time.time()
        try:
            # Run pg_sleep through the repository
            self._repo.execute_slow_query(seconds)

            elapsed = time.time() - start
            return StressTestResult(
                status="success",
                elapsed_seconds=elapsed,
                message=f"Connection held for {seconds} seconds",
            )
        except SATimeoutError as e:
            elapsed = time.time() - start
            logger.exception(
                "stress_test_service.pool_exhausted_timeout_after",
                elapsed=elapsed,
                error=e,
            )
            return StressTestResult(
                status="pool_exhausted",
                elapsed_seconds=elapsed,
                error="Connection pool exhausted - no available connections",
                error_type="SQLAlchemy TimeoutError",
            )
        except Exception as e:
            elapsed = time.time() - start
            logger.exception(
                "stress_test_service.after_failed",
                elapsed=elapsed,
                error=e,
            )
            return StressTestResult(
                status="error",
                elapsed_seconds=elapsed,
                error=str(e),
                error_type=type(e).__name__,
            )

    def simulate_connection_leak(self, hold_seconds: int) -> StressTestResult:
        """Simulate an intentional connection 'leak'."""
        hold_seconds = min(hold_seconds, 60)  # 60s max

        start = time.time()
        try:
            # Create a cursor through the repository (occupies a connection)
            cursor = self._repo.create_cursor()

            # Hold the connection with a ping
            self._repo.execute_with_cursor(cursor, "SELECT 1")

            # Intentional delay
            time.sleep(hold_seconds)

            # Never closed explicitly (leak simulation)
            # cursor.close()  # commented out intentionally

            elapsed = time.time() - start
            return StressTestResult(
                status="leak_simulated",
                elapsed_seconds=elapsed,
                extra={
                    "held_seconds": hold_seconds,
                    "warning": "Connection intentionally not closed",
                },
            )
        except Exception as e:
            elapsed = time.time() - start
            logger.exception(
                "stress_test_service.leak_simulation_failed_after",
                elapsed=elapsed,
                error=e,
            )
            return StressTestResult(
                status="error",
                elapsed_seconds=elapsed,
                error=str(e),
            )

    def execute_heavy_query(self) -> StressTestResult:
        """Run a heavy query."""
        start = time.time()
        try:
            # STRESS TEST ONLY: This query uses a configurable table for testing.
            from baldur.settings.stress_test import get_stress_test_settings

            stress_table = get_stress_test_settings().table

            # Run the aggregate query through the repository
            total, avg_price, max_price, min_price = self._repo.execute_aggregate_query(
                stress_table
            )

            # Extra delay (1 second)
            self._repo.pg_sleep(1)

            elapsed = time.time() - start
            return StressTestResult(
                status="success",
                elapsed_seconds=elapsed,
                extra={
                    "stats": {
                        "total_products": total,
                        "avg_price": avg_price,
                        "max_price": max_price,
                        "min_price": min_price,
                    },
                },
            )
        except Exception as e:
            elapsed = time.time() - start
            logger.exception(
                "stress_test_service.after_failed",
                elapsed=elapsed,
                error=e,
            )
            return StressTestResult(
                status="error",
                elapsed_seconds=elapsed,
                error=str(e),
            )

    # =========================================================================
    # Advisory Lock Operations
    # =========================================================================

    def acquire_advisory_lock(
        self,
        lock_id: int = 12345,
        hold_seconds: int = 5,
        exclusive: bool = True,
        wait: bool = True,
    ) -> StressTestResult:
        """Acquire a PostgreSQL advisory lock."""
        hold_seconds = min(hold_seconds, 60)
        start = time.time()

        try:
            # Use the repository's context manager
            with self._repo.advisory_lock_context(
                lock_id, exclusive, wait
            ) as lock_acquired:
                if not lock_acquired:
                    elapsed = time.time() - start
                    logger.info(
                        "stress_test_service.lock_acquired_conflict",
                        lock_id=lock_id,
                    )
                    return StressTestResult(
                        status="conflict",
                        elapsed_seconds=elapsed,
                        message="Lock held by another session",
                        extra={"lock_id": lock_id},
                    )

                logger.info(
                    "stress_test_service.lock_acquired_holding",
                    lock_id=lock_id,
                    hold_seconds=hold_seconds,
                )
                time.sleep(hold_seconds)

            # The context manager releases the lock automatically
            elapsed = time.time() - start
            logger.info(
                "stress_test_service.lock_released_after",
                lock_id=lock_id,
                elapsed=elapsed,
            )

            return StressTestResult(
                status="success",
                elapsed_seconds=elapsed,
                message=f"Advisory lock {lock_id} acquired and released successfully",
                extra={
                    "lock_id": lock_id,
                    "held_seconds": hold_seconds,
                    "exclusive": exclusive,
                },
            )

        except Exception as e:
            elapsed = time.time() - start
            error_str = str(e).lower()

            # Detect a lock timeout or a deadlock
            if "lock" in error_str or "timeout" in error_str or "deadlock" in error_str:
                logger.warning(
                    "stress_test_service.lock_contention_detected",
                    error=e,
                )
                return StressTestResult(
                    status="lock_timeout",
                    elapsed_seconds=elapsed,
                    error=str(e),
                    error_type="LockTimeout",
                    extra={"lock_id": lock_id},
                )

            logger.exception(
                "stress_test_service.after_failed",
                elapsed=elapsed,
                error=e,
            )
            return StressTestResult(
                status="error",
                elapsed_seconds=elapsed,
                error=str(e),
                error_type=type(e).__name__,
                extra={"lock_id": lock_id},
            )

    def run_lock_contention(
        self,
        lock_id: int = 99999,
        duration_seconds: int = 5,
        lock_hold_ms: int = 100,
    ) -> LockContentionResult:
        """Simulate advisory lock contention."""
        duration_seconds = min(duration_seconds, 30)
        lock_hold_ms = min(lock_hold_ms, 5000)

        start = time.time()
        success_count = 0
        fail_count = 0
        total_wait_ms = 0.0

        try:
            end_time = start + duration_seconds

            while time.time() < end_time:
                attempt_start = time.time()

                # Try the lock in non-blocking mode through the repository
                lock_acquired = self._repo.try_advisory_lock(lock_id)

                if lock_acquired:
                    success_count += 1
                    # Hold the lock
                    time.sleep(lock_hold_ms / 1000.0)
                    # Release the lock
                    self._repo.release_advisory_lock(lock_id)
                else:
                    fail_count += 1

                wait_ms = (time.time() - attempt_start) * 1000
                total_wait_ms += wait_ms

            elapsed = time.time() - start
            total_attempts = success_count + fail_count

            return LockContentionResult(
                status="completed",
                lock_id=lock_id,
                duration_seconds=elapsed,
                total_attempts=total_attempts,
                success_count=success_count,
                fail_count=fail_count,
                success_rate_percent=(
                    round(success_count / total_attempts * 100, 2)
                    if total_attempts > 0
                    else 0
                ),
                avg_wait_ms=(
                    round(total_wait_ms / total_attempts, 2)
                    if total_attempts > 0
                    else 0
                ),
                lock_hold_ms=lock_hold_ms,
            )

        except Exception as e:
            elapsed = time.time() - start
            logger.exception(
                "stress_test_service.contention_test_failed",
                error=e,
            )
            return LockContentionResult(
                status="error",
                lock_id=lock_id,
                duration_seconds=elapsed,
                error=str(e),
            )

    def run_controlled_burst_failure(
        self,
        lock_id: int = 777,
        lock_timeout_ms: int = 1,
        burst_duration_seconds: int = 10,
        concurrent_locks: int = 50,
    ) -> BurstFailureResult:
        """Controlled burst failure.

        Stages calm-before-the-storm -> system collapse -> autonomous recovery.
        """
        lock_timeout_ms = max(lock_timeout_ms, 1)  # 1ms min
        burst_duration_seconds = min(burst_duration_seconds, 30)
        concurrent_locks = min(concurrent_locks, 100)

        start = time.time()
        timeout_count = 0
        success_count = 0
        deadlock_count = 0

        try:
            # Use the repository's timeout context manager
            with self._repo.timeout_context(
                lock_timeout_ms=lock_timeout_ms,
                statement_timeout_ms=lock_timeout_ms * 10,
            ):
                logger.warning(
                    "stress_test_service.burst_started_ms",
                    lock_timeout_ms=lock_timeout_ms,
                    burst_duration_seconds=burst_duration_seconds,
                )

                # Hold one lock first so that other requests fail
                try:
                    self._repo.acquire_advisory_lock(lock_id, wait=True)

                    # Repeatedly try the lock on new connections during the
                    # burst (induces timeouts)
                    end_time = start + burst_duration_seconds

                    while time.time() < end_time:
                        try:
                            lock_acquired = self._repo.try_advisory_lock(lock_id + 1)
                            if lock_acquired:
                                success_count += 1
                                self._repo.release_advisory_lock(lock_id + 1)
                            else:
                                timeout_count += 1
                        except Exception as inner_e:
                            error_str = str(inner_e).lower()
                            if "timeout" in error_str or "lock" in error_str:
                                timeout_count += 1
                            elif "deadlock" in error_str:
                                deadlock_count += 1
                            else:
                                timeout_count += 1

                        time.sleep(0.01)  # 10ms

                    # Release the main lock
                    self._repo.release_advisory_lock(lock_id)

                except Exception as lock_e:
                    logger.exception(
                        "stress_test_service.main_lock_failed",
                        lock_e=lock_e,
                    )
                    timeout_count += 1

            # The timeout context manager restores the timeouts automatically

            elapsed = time.time() - start
            total_attempts = timeout_count + success_count + deadlock_count

            logger.warning(
                "stress_test_service.burst_completed",
                timeout_count=timeout_count,
                deadlock_count=deadlock_count,
            )

            return BurstFailureResult(
                status="burst_completed",
                lock_id=lock_id,
                lock_timeout_ms=lock_timeout_ms,
                burst_duration_seconds=elapsed,
                total_attempts=total_attempts,
                timeout_count=timeout_count,
                success_count=success_count,
                deadlock_count=deadlock_count,
                failure_rate_percent=round(
                    (timeout_count + deadlock_count) / max(1, total_attempts) * 100, 2
                ),
                message="Controlled burst failure completed - check DLQ for captured failures",
            )

        except Exception as e:
            elapsed = time.time() - start
            logger.exception(
                "stress_test_service.test_failed",
                error=e,
            )
            return BurstFailureResult(
                status="error",
                lock_id=lock_id,
                lock_timeout_ms=lock_timeout_ms,
                burst_duration_seconds=elapsed,
                timeout_count=timeout_count,
                error=str(e),
            )

    # =========================================================================
    # Pool Exhaustion Operations
    # =========================================================================

    def exhaust_pool(  # noqa: C901
        self,
        connections_to_hold: int = 10,
        hold_seconds: int = 30,
    ) -> StressTestResult:
        """Intentionally exhaust the DB connection pool."""
        connections_to_hold = min(connections_to_hold, 20)
        hold_seconds = min(hold_seconds, 60)

        start = time.time()
        held_count = 0

        try:
            logger.warning(
                "stress_test_service.starting_pool_exhaustion_connections",
                connections_to_hold=connections_to_hold,
                hold_seconds=hold_seconds,
            )

            # Clean up previously held connections
            if StressTestService._held_connections_lock:
                with StressTestService._held_connections_lock:
                    for conn_info in StressTestService._held_connections:
                        try:
                            conn_info["cursor"].close()
                        except Exception:
                            pass
                    StressTestService._held_connections.clear()

            # Occupy several connections (via repository)
            for i in range(connections_to_hold):
                try:
                    cursor = self._repo.create_cursor()

                    # Keep the connection in a busy state
                    self._repo.execute_with_cursor(
                        cursor, "SELECT pg_backend_pid(), pg_sleep(0.01)"
                    )

                    if StressTestService._held_connections_lock:
                        with StressTestService._held_connections_lock:
                            StressTestService._held_connections.append(
                                {"cursor": cursor, "created_at": time.time()}
                            )

                    held_count += 1
                    logger.info(
                        "stress_test_service.held_connection",
                        value=i + 1,
                        connections_to_hold=connections_to_hold,
                    )

                except Exception as e:
                    logger.warning(
                        "stress_test_service.acquire_connection_failed",
                        value=i + 1,
                        error=e,
                    )
                    break

            # Wait while holding the connections
            logger.warning(
                "stress_test_service.holding_connections",
                held_count=held_count,
                hold_seconds=hold_seconds,
            )
            time.sleep(hold_seconds)

            # Return the connections
            if StressTestService._held_connections_lock:
                with StressTestService._held_connections_lock:
                    for conn_info in StressTestService._held_connections:
                        try:
                            conn_info["cursor"].close()
                        except Exception:
                            pass
                    StressTestService._held_connections.clear()

            elapsed = time.time() - start
            logger.warning(
                "stress_test_service.pool_exhaustion_completed_after",
                elapsed=elapsed,
            )

            return StressTestResult(
                status="exhaustion_completed",
                elapsed_seconds=elapsed,
                message="Pool exhaustion completed - connections released",
                extra={
                    "connections_held": held_count,
                    "hold_seconds": hold_seconds,
                },
            )

        except Exception as e:
            elapsed = time.time() - start
            logger.exception(
                "stress_test_service.failed",
                error=e,
            )
            return StressTestResult(
                status="error",
                elapsed_seconds=elapsed,
                error=str(e),
                extra={"connections_held": held_count},
            )

    def trigger_cb_failure(self, error_type: str = "db_error") -> StressTestResult:
        """Intentional failure used to trigger the circuit breaker directly."""
        start = time.time()

        try:
            if error_type == "db_error":
                # Raise an intentional DB error (via repository)
                self._repo.execute_nonexistent_table_query()

            elif error_type == "timeout":
                # Raise a timeout error (via repository)
                self._repo.execute_timeout_query(timeout_ms=1, sleep_seconds=1)

            elif error_type == "exception":
                # Raise a Python exception
                raise RuntimeError("Intentional test exception for CB trigger")

            # Reaching this point normally should not happen
            return StressTestResult(status="unexpected_success")

        except Exception as e:
            elapsed = time.time() - start
            return StressTestResult(
                status="intentional_failure",
                elapsed_seconds=elapsed,
                error=str(e),
                message="This failure is intentional for CB testing",
                extra={"error_type": error_type},
            )


# =============================================================================
# Singleton Pattern
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_stress_test_service, configure_stress_test_service, reset_stress_test_service = (
    make_singleton_factory("stress_test_service", StressTestService)
)
