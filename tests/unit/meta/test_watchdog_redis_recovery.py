"""
_recover_redis() 2단계 복구 전략 + 쿨다운 메커니즘 단위 테스트.

Stage 1(커넥션 풀 리셋) → Stage 2(인프라 재시작) 흐름과
_attempt_recovery() 쿨다운 동작 검증.
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.meta.config import MetaWatchdogSettings
from baldur.meta.health_probe import HealthStatus, ProbeResult
from baldur.meta.recovery_adapter import RecoveryAction, RecoveryResult
from baldur_pro.services.meta_watchdog import SelfHealerWatchdog

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def settings():
    """복구 테스트용 설정.

    recovery_enabled=True (558 D7) keeps the FULL recovery + cooldown path
    reachable through _attempt_guarded_recovery; the v1.0 default (False) would
    short-circuit to slice-A escalate-only before the cooldown guard.
    """
    return MetaWatchdogSettings(
        enabled=True,
        self_cb_enabled=False,
        dry_run_mode=False,
        recovery_enabled=True,
        recovery_cooldown_seconds=300.0,
        redis_workload_name="redis",
        dlq_worker_workload_name="celery-dlq-worker",
    )


@pytest.fixture
def mock_probe_manager():
    """Mock probe manager."""
    manager = MagicMock()
    manager.probe_all.return_value = {}
    manager.get_overall_status.return_value = HealthStatus.HEALTHY
    return manager


@pytest.fixture
def watchdog(settings, mock_probe_manager):
    """테스트 대상 Watchdog."""
    return SelfHealerWatchdog(
        settings=settings,
        probe_manager=mock_probe_manager,
    )


@pytest.fixture
def unhealthy_redis_probe():
    """비정상 Redis 프로브 결과."""
    return ProbeResult(
        component="redis",
        status=HealthStatus.UNHEALTHY,
        latency_ms=0,
        timestamp=datetime.now(UTC),
        error="Connection refused",
        details={},
    )


# =============================================================================
# _recover_redis() — Stage 1/2 분기 테스트
# =============================================================================


class TestRecoverRedisStage1Behavior:
    """_recover_redis() Stage 1 (커넥션 풀 리셋) 동작 검증."""

    def test_stage1_reconnect_success_returns_true(
        self, watchdog, unhealthy_redis_probe
    ):
        """Stage 1 reconnect 성공 시 True를 반환하고 Stage 2를 호출하지 않는다."""
        mock_adapter = MagicMock()
        mock_adapter.reconnect.return_value = True

        with patch("baldur.factory.ProviderRegistry") as mock_registry:
            mock_registry.get_cache.return_value = mock_adapter

            result = watchdog._recover_redis_impl(
                unhealthy_redis_probe, threading.Event(), 60.0
            )

            assert result is True
            mock_registry.get_cache.assert_called_once_with("redis")

    def test_stage1_reconnect_false_falls_through_to_stage2(
        self, watchdog, unhealthy_redis_probe
    ):
        """Stage 1의 reconnect()가 False를 반환하면 Stage 2로 진행한다."""
        mock_adapter = MagicMock()
        mock_adapter.reconnect.return_value = False

        mock_recovery_result = RecoveryResult(
            action=RecoveryAction.RESTART_WORKER,
            success=True,
            target="redis",
            message="Rolling restart triggered (Deployment)",
            timestamp=datetime.now(UTC),
        )
        mock_recovery_adapter = MagicMock()
        mock_recovery_adapter.restart_worker.return_value = mock_recovery_result

        with (
            patch("baldur.factory.ProviderRegistry") as mock_registry,
            patch(
                "baldur.meta.recovery_adapter.get_recovery_adapter",
                return_value=mock_recovery_adapter,
            ),
        ):
            mock_registry.get_cache.return_value = mock_adapter

            result = watchdog._recover_redis_impl(
                unhealthy_redis_probe, threading.Event(), 60.0
            )

            assert result is True
            mock_recovery_adapter.restart_worker.assert_called_once_with(
                "redis",
                timeout=60.0,
            )

    def test_stage1_provider_import_error_falls_to_stage2(
        self, watchdog, unhealthy_redis_probe
    ):
        """ProviderRegistry import 실패 시 Stage 2로 진행한다."""
        mock_recovery_result = RecoveryResult(
            action=RecoveryAction.RESTART_WORKER,
            success=True,
            target="redis",
            message="Rolling restart triggered (Deployment)",
            timestamp=datetime.now(UTC),
        )
        mock_recovery_adapter = MagicMock()
        mock_recovery_adapter.restart_worker.return_value = mock_recovery_result

        with (
            patch(
                "baldur.factory.ProviderRegistry",
                side_effect=ImportError("no module"),
            ),
            patch(
                "baldur.meta.recovery_adapter.get_recovery_adapter",
                return_value=mock_recovery_adapter,
            ),
        ):
            result = watchdog._recover_redis_impl(
                unhealthy_redis_probe, threading.Event(), 60.0
            )

            assert result is True


class TestRecoverRedisExceptionFilterBehavior:
    """_recover_redis() 예외 세분화 (화이트리스트) 동작 검증."""

    def test_connection_error_triggers_stage2(self, watchdog, unhealthy_redis_probe):
        """ConnectionError 시 Stage 2 RecoveryAdapter를 호출한다."""
        import redis as redis_lib

        mock_adapter = MagicMock()
        mock_adapter.reconnect.side_effect = redis_lib.exceptions.ConnectionError(
            "refused"
        )

        mock_recovery_result = RecoveryResult(
            action=RecoveryAction.RESTART_WORKER,
            success=True,
            target="redis",
            message="Rolling restart triggered (Deployment)",
            timestamp=datetime.now(UTC),
        )
        mock_recovery_adapter = MagicMock()
        mock_recovery_adapter.restart_worker.return_value = mock_recovery_result

        with (
            patch("baldur.factory.ProviderRegistry") as mock_registry,
            patch(
                "baldur.meta.recovery_adapter.get_recovery_adapter",
                return_value=mock_recovery_adapter,
            ),
        ):
            mock_registry.get_cache.return_value = mock_adapter

            result = watchdog._recover_redis_impl(
                unhealthy_redis_probe, threading.Event(), 60.0
            )

            assert result is True
            mock_recovery_adapter.restart_worker.assert_called_once()

    def test_timeout_error_triggers_stage2(self, watchdog, unhealthy_redis_probe):
        """TimeoutError 시 Stage 2로 진행한다."""
        import redis as redis_lib

        mock_adapter = MagicMock()
        mock_adapter.reconnect.side_effect = redis_lib.exceptions.TimeoutError(
            "timed out"
        )

        mock_recovery_result = RecoveryResult(
            action=RecoveryAction.RESTART_WORKER,
            success=True,
            target="redis",
            message="Rolling restart triggered (Deployment)",
            timestamp=datetime.now(UTC),
        )
        mock_recovery_adapter = MagicMock()
        mock_recovery_adapter.restart_worker.return_value = mock_recovery_result

        with (
            patch("baldur.factory.ProviderRegistry") as mock_registry,
            patch(
                "baldur.meta.recovery_adapter.get_recovery_adapter",
                return_value=mock_recovery_adapter,
            ),
        ):
            mock_registry.get_cache.return_value = mock_adapter

            result = watchdog._recover_redis_impl(
                unhealthy_redis_probe, threading.Event(), 60.0
            )

            assert result is True

    def test_busy_loading_error_triggers_stage2(self, watchdog, unhealthy_redis_probe):
        """BusyLoadingError 시 Stage 2로 진행한다."""
        import redis as redis_lib

        mock_adapter = MagicMock()
        mock_adapter.reconnect.side_effect = redis_lib.exceptions.BusyLoadingError(
            "loading"
        )

        mock_recovery_result = RecoveryResult(
            action=RecoveryAction.RESTART_WORKER,
            success=True,
            target="redis",
            message="Rolling restart triggered (Deployment)",
            timestamp=datetime.now(UTC),
        )
        mock_recovery_adapter = MagicMock()
        mock_recovery_adapter.restart_worker.return_value = mock_recovery_result

        with (
            patch("baldur.factory.ProviderRegistry") as mock_registry,
            patch(
                "baldur.meta.recovery_adapter.get_recovery_adapter",
                return_value=mock_recovery_adapter,
            ),
        ):
            mock_registry.get_cache.return_value = mock_adapter

            result = watchdog._recover_redis_impl(
                unhealthy_redis_probe, threading.Event(), 60.0
            )

            assert result is True

    def test_auth_error_skips_stage2_returns_false(
        self, watchdog, unhealthy_redis_probe
    ):
        """AuthenticationError 시 Stage 2를 스킵하고 즉시 False 반환한다."""
        import redis as redis_lib

        mock_adapter = MagicMock()
        mock_adapter.reconnect.side_effect = redis_lib.exceptions.AuthenticationError(
            "WRONGPASS"
        )

        with patch("baldur.factory.ProviderRegistry") as mock_registry:
            mock_registry.get_cache.return_value = mock_adapter

            result = watchdog._recover_redis_impl(
                unhealthy_redis_probe, threading.Event(), 60.0
            )

            assert result is False

    def test_response_error_skips_stage2_returns_false(
        self, watchdog, unhealthy_redis_probe
    ):
        """ResponseError 시 Stage 2를 스킵하고 즉시 False 반환한다."""
        import redis as redis_lib

        mock_adapter = MagicMock()
        mock_adapter.reconnect.side_effect = redis_lib.exceptions.ResponseError("OOM")

        with patch("baldur.factory.ProviderRegistry") as mock_registry:
            mock_registry.get_cache.return_value = mock_adapter

            result = watchdog._recover_redis_impl(
                unhealthy_redis_probe, threading.Event(), 60.0
            )

            assert result is False


class TestRecoverRedisStage2Behavior:
    """_recover_redis() Stage 2 (인프라 재시작) 동작 검증."""

    def test_stage2_uses_settings_workload_name(self, watchdog, unhealthy_redis_probe):
        """Stage 2가 settings.redis_workload_name을 사용한다."""
        import redis as redis_lib

        watchdog._settings.redis_workload_name = "redis-master"

        mock_adapter = MagicMock()
        mock_adapter.reconnect.side_effect = redis_lib.exceptions.ConnectionError()

        mock_recovery_result = RecoveryResult(
            action=RecoveryAction.RESTART_WORKER,
            success=True,
            target="redis-master",
            message="Rolling restart triggered",
            timestamp=datetime.now(UTC),
        )
        mock_recovery_adapter = MagicMock()
        mock_recovery_adapter.restart_worker.return_value = mock_recovery_result

        with (
            patch("baldur.factory.ProviderRegistry") as mock_registry,
            patch(
                "baldur.meta.recovery_adapter.get_recovery_adapter",
                return_value=mock_recovery_adapter,
            ),
        ):
            mock_registry.get_cache.return_value = mock_adapter

            watchdog._recover_redis_impl(unhealthy_redis_probe, threading.Event(), 60.0)

            mock_recovery_adapter.restart_worker.assert_called_once_with(
                "redis-master",
                timeout=60.0,
            )

    def test_both_stages_fail_returns_false(self, watchdog, unhealthy_redis_probe):
        """Stage 1, 2 모두 실패 시 False 반환한다."""
        import redis as redis_lib

        mock_adapter = MagicMock()
        mock_adapter.reconnect.side_effect = redis_lib.exceptions.ConnectionError()

        mock_recovery_result = RecoveryResult(
            action=RecoveryAction.RESTART_WORKER,
            success=False,
            target="redis",
            message="K8s client not available",
            timestamp=datetime.now(UTC),
        )
        mock_recovery_adapter = MagicMock()
        mock_recovery_adapter.restart_worker.return_value = mock_recovery_result

        with (
            patch("baldur.factory.ProviderRegistry") as mock_registry,
            patch(
                "baldur.meta.recovery_adapter.get_recovery_adapter",
                return_value=mock_recovery_adapter,
            ),
        ):
            mock_registry.get_cache.return_value = mock_adapter

            result = watchdog._recover_redis_impl(
                unhealthy_redis_probe, threading.Event(), 60.0
            )

            assert result is False

    def test_stage2_recovery_adapter_import_error_returns_false(
        self, watchdog, unhealthy_redis_probe
    ):
        """RecoveryAdapter import 실패 시 False를 반환한다."""
        mock_adapter = MagicMock()
        mock_adapter.reconnect.return_value = False

        with (
            patch("baldur.factory.ProviderRegistry") as mock_registry,
            patch(
                "baldur.meta.recovery_adapter.get_recovery_adapter",
                side_effect=ImportError("no module"),
            ),
        ):
            mock_registry.get_cache.return_value = mock_adapter

            result = watchdog._recover_redis_impl(
                unhealthy_redis_probe, threading.Event(), 60.0
            )

            assert result is False


# =============================================================================
# _attempt_recovery() 쿨다운 메커니즘 테스트
# =============================================================================


class TestRecoveryCooldownBehavior:
    """Cooldown behavior via _attempt_guarded_recovery and _is_in_cooldown."""

    def test_cooldown_blocks_repeated_recovery(self, watchdog, unhealthy_redis_probe):
        """쿨다운 기간 내 동일 컴포넌트 복구를 차단한다."""
        watchdog._last_recovery_time["redis"] = time.time()

        assert watchdog._is_in_cooldown("redis") is True

        # _attempt_guarded_recovery skips recovery during cooldown
        result = watchdog._attempt_guarded_recovery(
            "redis",
            unhealthy_redis_probe,
            60.0,
        )
        assert result is False

    def test_cooldown_expired_allows_recovery(self, watchdog, unhealthy_redis_probe):
        """쿨다운 만료 후 복구를 허용한다."""
        cooldown = watchdog._settings.recovery_cooldown_seconds
        watchdog._last_recovery_time["redis"] = time.time() - cooldown - 1

        assert watchdog._is_in_cooldown("redis") is False

        with (
            patch.object(watchdog, "_execute_recovery_with_timeout", return_value=True),
            patch.object(watchdog, "_get_recovery_audit_recorder", return_value=None),
        ):
            result = watchdog._attempt_recovery("redis", unhealthy_redis_probe, 60.0)

            assert result is True

    def test_different_component_not_affected_by_cooldown(
        self, watchdog, unhealthy_redis_probe
    ):
        """다른 컴포넌트의 쿨다운에 영향받지 않는다."""
        watchdog._last_recovery_time["dlq"] = time.time()

        assert watchdog._is_in_cooldown("redis") is False

        with (
            patch.object(watchdog, "_execute_recovery_with_timeout", return_value=True),
            patch.object(watchdog, "_get_recovery_audit_recorder", return_value=None),
        ):
            result = watchdog._attempt_recovery("redis", unhealthy_redis_probe, 60.0)

            assert result is True

    def test_cooldown_timestamp_recorded_after_recovery(
        self, watchdog, unhealthy_redis_probe
    ):
        """복구 시도 후 타임스탬프가 기록된다 (pre-set before execution)."""
        assert "redis" not in watchdog._last_recovery_time

        with (
            patch.object(watchdog, "_execute_recovery_with_timeout", return_value=True),
            patch.object(watchdog, "_get_recovery_audit_recorder", return_value=None),
        ):
            watchdog._attempt_recovery("redis", unhealthy_redis_probe, 60.0)

            assert "redis" in watchdog._last_recovery_time
            assert watchdog._last_recovery_time["redis"] > 0

    def test_cooldown_timestamp_recorded_even_on_failure(
        self, watchdog, unhealthy_redis_probe
    ):
        """복구 실패 시에도 타임스탬프가 기록된다."""
        with (
            patch.object(
                watchdog, "_execute_recovery_with_timeout", return_value=False
            ),
            patch.object(watchdog, "_get_recovery_audit_recorder", return_value=None),
        ):
            watchdog._attempt_recovery("redis", unhealthy_redis_probe, 60.0)

            assert "redis" in watchdog._last_recovery_time

    def test_first_recovery_has_no_cooldown(self, watchdog, unhealthy_redis_probe):
        """최초 복구 시도는 쿨다운 없이 즉시 실행된다."""
        assert watchdog._last_recovery_time.get("redis", 0.0) == 0.0
        assert watchdog._is_in_cooldown("redis") is False

        with (
            patch.object(watchdog, "_execute_recovery_with_timeout", return_value=True),
            patch.object(watchdog, "_get_recovery_audit_recorder", return_value=None),
        ):
            result = watchdog._attempt_recovery("redis", unhealthy_redis_probe, 60.0)

            assert result is True


# =============================================================================
# _recover_dlq() 워커 이름 환경변수화 테스트
# =============================================================================


class TestRecoverDlqWorkloadNameBehavior:
    """_recover_dlq() 워크로드 이름 설정 사용 검증."""

    def test_uses_settings_workload_name(self, watchdog):
        """settings.dlq_worker_workload_name을 사용한다."""
        watchdog._settings.dlq_worker_workload_name = "custom-dlq-worker"

        mock_recovery_result = RecoveryResult(
            action=RecoveryAction.RESTART_WORKER,
            success=True,
            target="custom-dlq-worker",
            message="Restarted",
            timestamp=datetime.now(UTC),
        )
        mock_recovery_adapter = MagicMock()
        mock_recovery_adapter.restart_worker.return_value = mock_recovery_result

        probe_result = ProbeResult(
            component="dlq",
            status=HealthStatus.UNHEALTHY,
            latency_ms=0,
            timestamp=datetime.now(UTC),
            error="DLQ stuck",
            details={},
        )

        with patch(
            "baldur.meta.recovery_adapter.get_recovery_adapter",
            return_value=mock_recovery_adapter,
        ):
            result = watchdog._recover_dlq_impl(probe_result, threading.Event(), 60.0)

            assert result is True
            mock_recovery_adapter.restart_worker.assert_called_once_with(
                "custom-dlq-worker",
                timeout=60.0,
            )
