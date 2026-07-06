"""
Stress Test Service.

DB Connection Pool 스트레스 테스트를 위한 비즈니스 로직.

이 모듈은 테스트 전용이며, 프로덕션에서는 절대 사용하지 마세요!
비즈니스 로직을 View 레이어에서 분리하여 클린 아키텍처를 유지합니다.

Note:
- Views(stress_views.py)는 Request/Response 처리만 담당
- 실제 DB 연산, 락 테스트, 풀 관리 로직은 이 서비스에서 담당
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

# SQLAlchemy Pool 상태 조회를 위한 import
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
    스트레스 테스트 서비스.

    DB Connection Pool 관련 테스트 로직을 캡슐화합니다.
    PgAdminProvider를 통해 Raw SQL을 캡슐화합니다.
    """

    # 점유 중인 커넥션들을 저장하는 클래스 변수
    _held_connections: list = []
    _held_connections_lock: threading.Lock | None = None

    def __init__(self, repository: PgAdminProvider | None = None):
        """
        서비스 초기화.

        Args:
            repository: PgAdminProvider 인스턴스 (없으면 registry default 사용)
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
        """SQLAlchemy Pool 정보 조회.

        ProviderRegistry.pool_info 의 활성 provider 에 위임합니다.
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
        """현재 Connection Pool 상태 조회."""
        try:
            # SQLAlchemy Pool 정보 먼저 시도
            pool_info = self.get_pool_info()

            from baldur.factory import ProviderRegistry

            db_provider = ProviderRegistry.database_health.get()
            conn_info = db_provider.check_connection("default")

            # PostgreSQL 연결 통계 조회 (Repository 사용)
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
        """지정된 시간 동안 DB 연결을 점유하는 느린 쿼리 실행."""
        start = time.time()
        try:
            # Repository를 통해 pg_sleep 실행
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
        """의도적으로 연결을 '누수'시키는 시뮬레이션."""
        hold_seconds = min(hold_seconds, 60)  # 최대 60초

        start = time.time()
        try:
            # Repository를 통해 커서 생성 (연결 점유)
            cursor = self._repo.create_cursor()

            # ping으로 연결 점유
            self._repo.execute_with_cursor(cursor, "SELECT 1")

            # 의도적 지연
            time.sleep(hold_seconds)

            # 명시적으로 닫지 않음 (누수 시뮬레이션)
            # cursor.close()  # 의도적으로 주석 처리

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
        """무거운 쿼리 실행."""
        start = time.time()
        try:
            # STRESS TEST ONLY: This query uses a configurable table for testing.
            from baldur.settings.stress_test import get_stress_test_settings

            stress_table = get_stress_test_settings().table

            # Repository를 통해 집계 쿼리 실행
            total, avg_price, max_price, min_price = self._repo.execute_aggregate_query(
                stress_table
            )

            # 추가 지연 (1초)
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
        """PostgreSQL Advisory Lock 획득."""
        hold_seconds = min(hold_seconds, 60)
        start = time.time()

        try:
            # Repository의 컨텍스트 매니저 사용
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

            # 컨텍스트 매니저가 자동으로 락 해제
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

            # 락 타임아웃 또는 데드락 감지
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
        """Advisory Lock 경합 시뮬레이션."""
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

                # Repository를 통해 비대기 모드로 락 시도
                lock_acquired = self._repo.try_advisory_lock(lock_id)

                if lock_acquired:
                    success_count += 1
                    # 락 유지
                    time.sleep(lock_hold_ms / 1000.0)
                    # 락 해제
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
        """Controlled Burst Failure - 폭풍 전야 → 시스템 붕괴 → 자율 복구 연출."""
        lock_timeout_ms = max(lock_timeout_ms, 1)  # 최소 1ms
        burst_duration_seconds = min(burst_duration_seconds, 30)
        concurrent_locks = min(concurrent_locks, 100)

        start = time.time()
        timeout_count = 0
        success_count = 0
        deadlock_count = 0

        try:
            # Repository의 타임아웃 컨텍스트 매니저 사용
            with self._repo.timeout_context(
                lock_timeout_ms=lock_timeout_ms,
                statement_timeout_ms=lock_timeout_ms * 10,
            ):
                logger.warning(
                    "stress_test_service.burst_started_ms",
                    lock_timeout_ms=lock_timeout_ms,
                    burst_duration_seconds=burst_duration_seconds,
                )

                # 먼저 하나의 락을 잡아서 유지 (다른 요청들이 실패하도록)
                try:
                    self._repo.acquire_advisory_lock(lock_id, wait=True)

                    # burst 동안 반복적으로 새 연결에서 락 시도 (타임아웃 유발)
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

                    # 메인 락 해제
                    self._repo.release_advisory_lock(lock_id)

                except Exception as lock_e:
                    logger.exception(
                        "stress_test_service.main_lock_failed",
                        lock_e=lock_e,
                    )
                    timeout_count += 1

            # 타임아웃 컨텍스트 매니저가 자동으로 타임아웃 복원

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
        """DB 커넥션 풀을 의도적으로 고갈시킴."""
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

            # 기존 점유 커넥션 정리
            if StressTestService._held_connections_lock:
                with StressTestService._held_connections_lock:
                    for conn_info in StressTestService._held_connections:
                        try:
                            conn_info["cursor"].close()
                        except Exception:
                            pass
                    StressTestService._held_connections.clear()

            # 여러 커넥션 점유 (Repository 사용)
            for i in range(connections_to_hold):
                try:
                    cursor = self._repo.create_cursor()

                    # 커넥션을 busy 상태로 유지
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

            # 커넥션 유지하면서 대기
            logger.warning(
                "stress_test_service.holding_connections",
                held_count=held_count,
                hold_seconds=hold_seconds,
            )
            time.sleep(hold_seconds)

            # 커넥션 반환
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
        """Circuit Breaker를 직접 트리거하기 위한 의도적 실패."""
        start = time.time()

        try:
            if error_type == "db_error":
                # 의도적인 DB 에러 발생 (Repository 사용)
                self._repo.execute_nonexistent_table_query()

            elif error_type == "timeout":
                # 타임아웃 에러 발생 (Repository 사용)
                self._repo.execute_timeout_query(timeout_ms=1, sleep_seconds=1)

            elif error_type == "exception":
                # Python 예외 발생
                raise RuntimeError("Intentional test exception for CB trigger")

            # 정상적으로 여기까지 오면 안됨
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
