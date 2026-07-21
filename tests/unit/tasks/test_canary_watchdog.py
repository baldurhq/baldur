"""
Canary Watchdog Tests.

RolloutWatchdog 서비스 및 Celery 태스크 테스트.

"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from datetime import timedelta
from unittest.mock import Mock, patch

import pytest

from baldur.tasks.canary_watchdog import (
    CanaryWatchdogConfig,
    RolloutWatchdog,
    WatchdogResult,
    ZombieRollout,
    auto_promote_eligible,
    collect_canary_metrics,
    get_rollout_watchdog,
    reset_watchdog,
    scan_zombie_rollouts,
)
from baldur.utils.time import utc_now
from baldur_pro.services.canary import (
    CanaryRollout,
    CanaryStage,
    CanaryState,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def reset_singleton():
    """각 테스트 전 싱글톤 리셋."""
    reset_watchdog()
    yield
    reset_watchdog()


@pytest.fixture
def watchdog_config():
    """테스트용 Watchdog 설정."""
    return CanaryWatchdogConfig(
        zombie_threshold_minutes=30,
        auto_rollback_after_minutes=60,
        max_stage_duration_minutes=15,
        enable_auto_promote=True,
        enable_auto_rollback=True,
        notification_enabled=False,  # 테스트에서는 알림 비활성화
    )


@pytest.fixture
def sample_rollout():
    """샘플 롤아웃 (정상 상태)."""
    return CanaryRollout(
        id="test1234",
        config_type="circuit_breaker",
        previous_values={"failure_threshold": 5},
        new_values={"failure_threshold": 3},
        state=CanaryState.CANARY,
        current_stage_index=0,
        stages=[
            CanaryStage(
                name="canary",
                clusters=["seoul-canary"],
                percentage=10.0,
                duration_minutes=5,
                auto_promote=True,
            ),
        ],
        created_by="admin@example.com",
        created_at=utc_now(),  # 방금 생성됨
        reason="Test rollout",
    )


@pytest.fixture
def zombie_rollout():
    """Zombie 롤아웃 (오래 정체됨)."""
    return CanaryRollout(
        id="zombie123",
        config_type="dlq",
        previous_values={"max_retries": 3},
        new_values={"max_retries": 5},
        state=CanaryState.CANARY,
        current_stage_index=0,
        stages=[
            CanaryStage(
                name="canary",
                clusters=["tokyo"],
                percentage=10.0,
                duration_minutes=5,
            ),
        ],
        created_by="operator@example.com",
        created_at=utc_now() - timedelta(minutes=45),  # 45분 전 생성
        reason="Old rollout",
    )


@pytest.fixture
def paused_rollout():
    """일시 중지된 롤아웃."""
    return CanaryRollout(
        id="paused456",
        config_type="retry",
        previous_values={},
        new_values={"max_attempts": 5},
        state=CanaryState.PAUSED,
        current_stage_index=0,
        stages=[
            CanaryStage(
                name="canary",
                clusters=["singapore"],
                percentage=10.0,
                duration_minutes=5,
            ),
        ],
        created_by="admin@example.com",
        created_at=utc_now() - timedelta(minutes=35),  # 35분 전
        reason="Paused rollout",
    )


# =============================================================================
# CanaryWatchdogConfig Tests
# =============================================================================


class TestCanaryWatchdogConfig:
    """CanaryWatchdogConfig 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        config = CanaryWatchdogConfig()

        assert config.zombie_threshold_minutes == 30
        assert config.auto_rollback_after_minutes == 60
        assert config.enable_auto_promote is True
        assert config.enable_auto_rollback is True
        assert config.notification_enabled is True

    def test_custom_values(self):
        """사용자 정의 값."""
        config = CanaryWatchdogConfig(
            zombie_threshold_minutes=15,
            enable_auto_rollback=False,
        )

        assert config.zombie_threshold_minutes == 15
        assert config.enable_auto_rollback is False


# =============================================================================
# ZombieRollout Tests
# =============================================================================


class TestZombieRollout:
    """ZombieRollout 테스트."""

    def test_creation(self):
        """Zombie 롤아웃 생성."""
        zombie = ZombieRollout(
            rollout_id="test123",
            config_type="circuit_breaker",
            state="canary",
            stuck_since=utc_now() - timedelta(minutes=40),
            stuck_minutes=40.0,
            created_by="admin@example.com",
            affected_clusters=["seoul", "tokyo"],
            reason="Stuck in CANARY state",
        )

        assert zombie.rollout_id == "test123"
        assert zombie.stuck_minutes == 40.0
        assert zombie.action_taken == ""


# =============================================================================
# WatchdogResult Tests
# =============================================================================


class TestWatchdogResult:
    """WatchdogResult 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        result = WatchdogResult()

        assert result.success is True
        assert result.scanned_count == 0
        assert result.zombie_count == 0
        assert result.rollback_count == 0
        assert result.zombies == []

    def test_to_dict(self):
        """딕셔너리 변환."""
        zombie = ZombieRollout(
            rollout_id="test123",
            config_type="cb",
            state="canary",
            stuck_since=utc_now(),
            stuck_minutes=45.0,
            created_by="admin",
            affected_clusters=["seoul"],
        )

        result = WatchdogResult(
            success=True,
            scanned_count=5,
            zombie_count=1,
            zombies=[zombie],
        )

        d = result.to_dict()

        assert d["success"] is True
        assert d["scanned_count"] == 5
        assert d["zombie_count"] == 1
        assert len(d["zombies"]) == 1
        assert d["zombies"][0]["rollout_id"] == "test123"


# =============================================================================
# RolloutWatchdog Tests
# =============================================================================


class TestRolloutWatchdog:
    """RolloutWatchdog 테스트."""

    def test_initialization_default_config(self):
        """기본 설정으로 초기화."""
        watchdog = RolloutWatchdog()

        assert watchdog.config.zombie_threshold_minutes == 30
        assert watchdog._service is None

    def test_initialization_custom_config(self, watchdog_config):
        """사용자 정의 설정으로 초기화."""
        watchdog = RolloutWatchdog(config=watchdog_config)

        assert watchdog.config.notification_enabled is False

    def test_scan_no_active_rollouts(self, watchdog_config):
        """활성 롤아웃이 없을 때."""
        watchdog = RolloutWatchdog(config=watchdog_config)

        mock_service = Mock()
        mock_service.get_active_rollouts.return_value = []
        watchdog._service = mock_service

        result = watchdog.scan_and_handle()

        assert result.success is True
        assert result.scanned_count == 0
        assert result.zombie_count == 0

    def test_scan_detects_zombie_canary(self, watchdog_config, zombie_rollout):
        """CANARY 상태의 Zombie 감지."""
        watchdog = RolloutWatchdog(config=watchdog_config)

        mock_service = Mock()
        mock_service.get_active_rollouts.return_value = [zombie_rollout]
        mock_service.rollback.return_value = False  # 롤백 조건 미달
        watchdog._service = mock_service

        result = watchdog.scan_and_handle()

        assert result.scanned_count == 1
        assert result.zombie_count == 1
        assert result.zombies[0].rollout_id == "zombie123"

    def test_scan_detects_zombie_paused(self, watchdog_config, paused_rollout):
        """PAUSED 상태의 Zombie 감지."""
        watchdog = RolloutWatchdog(config=watchdog_config)

        mock_service = Mock()
        mock_service.get_active_rollouts.return_value = [paused_rollout]
        watchdog._service = mock_service

        result = watchdog.scan_and_handle()

        assert result.zombie_count == 1
        assert "paused" in result.zombies[0].state.lower()

    def test_scan_normal_rollout_not_zombie(self, watchdog_config, sample_rollout):
        """정상 롤아웃은 Zombie로 감지 안됨."""
        watchdog = RolloutWatchdog(config=watchdog_config)

        mock_service = Mock()
        mock_service.get_active_rollouts.return_value = [sample_rollout]
        watchdog._service = mock_service

        result = watchdog.scan_and_handle()

        assert result.scanned_count == 1
        assert result.zombie_count == 0

    def test_auto_rollback_on_threshold(self, watchdog_config):
        """임계값 초과 시 자동 롤백."""
        # 65분 정체된 롤아웃 (auto_rollback_after_minutes=60 초과)
        old_rollout = CanaryRollout(
            id="veryold",
            config_type="circuit_breaker",
            previous_values={},
            new_values={"threshold": 3},
            state=CanaryState.CANARY,
            stages=[
                CanaryStage(
                    name="canary", clusters=["seoul"], percentage=10, duration_minutes=5
                ),
            ],
            created_by="admin",
            created_at=utc_now() - timedelta(minutes=65),
        )

        watchdog = RolloutWatchdog(config=watchdog_config)

        mock_service = Mock()
        mock_service.get_active_rollouts.return_value = [old_rollout]
        mock_service.rollback.return_value = True
        watchdog._service = mock_service

        result = watchdog.scan_and_handle()

        assert result.zombie_count == 1
        assert result.rollback_count == 1
        mock_service.rollback.assert_called_once()

    def test_auto_rollback_passes_governance_bypass_arguments(self, watchdog_config):
        """Auto rollback passes bypass_governance=True to skip governance gate."""
        # Given — zombie rollout stuck beyond auto_rollback_after_minutes
        old_rollout = CanaryRollout(
            id="zombie-gov",
            config_type="circuit_breaker",
            previous_values={},
            new_values={"threshold": 3},
            state=CanaryState.CANARY,
            stages=[
                CanaryStage(
                    name="canary", clusters=["seoul"], percentage=10, duration_minutes=5
                ),
            ],
            created_by="admin",
            created_at=utc_now() - timedelta(minutes=65),
        )

        watchdog = RolloutWatchdog(config=watchdog_config)
        mock_service = Mock()
        mock_service.get_active_rollouts.return_value = [old_rollout]
        mock_service.rollback.return_value = True
        watchdog._service = mock_service

        # When
        watchdog.scan_and_handle()

        # Then — rollback called with governance bypass arguments
        call_kwargs = mock_service.rollback.call_args.kwargs
        assert call_kwargs["bypass_governance"] is True
        assert call_kwargs["requested_by"] == "system:watchdog"
        assert len(call_kwargs["bypass_reason"]) >= 10

    def test_auto_promote_eligible(self, watchdog_config, sample_rollout):
        """자동 프로모션 조건 충족 시 프로모션."""
        from unittest.mock import Mock as MockClass
        from unittest.mock import patch

        # duration 경과한 롤아웃
        sample_rollout.created_at = utc_now() - timedelta(minutes=10)

        watchdog = RolloutWatchdog(config=watchdog_config)

        mock_service = Mock()
        mock_service.get_active_rollouts.return_value = [sample_rollout]
        mock_service.promote.return_value = True
        watchdog._service = mock_service

        # Mock governance to allow promotion
        mock_governance = MockClass()
        mock_governance.allowed = True

        with patch(
            "baldur_pro.services.governance.checks.check_all_governance",
            return_value=mock_governance,
        ):
            result = watchdog.auto_promote_eligible()

        assert result.scanned_count == 1
        assert result.promote_count == 1
        mock_service.promote.assert_called_once_with(sample_rollout.id, force=False)

    def test_auto_promote_disabled(self, sample_rollout):
        """auto_promote 비활성화 시 프로모션 안함."""
        config = CanaryWatchdogConfig(enable_auto_promote=False)
        watchdog = RolloutWatchdog(config=config)

        mock_service = Mock()
        mock_service.get_active_rollouts.return_value = [sample_rollout]
        watchdog._service = mock_service

        result = watchdog.auto_promote_eligible()

        assert result.promote_count == 0
        mock_service.promote.assert_not_called()


# =============================================================================
# Task Function Tests
# =============================================================================


class TestScanZombieRolloutsTask:
    """scan_zombie_rollouts 태스크 테스트."""

    def test_task_success(self):
        """태스크 성공."""
        with patch("baldur.tasks.canary_watchdog.get_rollout_watchdog") as mock_get:
            mock_watchdog = Mock()
            mock_watchdog.scan_and_handle.return_value = WatchdogResult(
                success=True,
                scanned_count=5,
                zombie_count=1,
            )
            mock_get.return_value = mock_watchdog

            result = scan_zombie_rollouts()

            assert result["success"] is True
            assert result["scanned_count"] == 5
            assert result["zombie_count"] == 1

    def test_task_error_handling(self):
        """태스크 에러 처리."""
        with patch("baldur.tasks.canary_watchdog.get_rollout_watchdog") as mock_get:
            mock_get.side_effect = Exception("Test error")

            with pytest.raises(Exception, match="Test error"):
                scan_zombie_rollouts()


class TestAutoPromoteEligibleTask:
    """auto_promote_eligible 태스크 테스트."""

    def test_task_success(self):
        """태스크 성공."""
        with patch("baldur.tasks.canary_watchdog.get_rollout_watchdog") as mock_get:
            mock_watchdog = Mock()
            mock_watchdog.auto_promote_eligible.return_value = WatchdogResult(
                success=True,
                scanned_count=3,
                promote_count=2,
            )
            mock_get.return_value = mock_watchdog

            result = auto_promote_eligible()

            assert result["success"] is True
            assert result["promote_count"] == 2


class TestCollectCanaryMetricsTask:
    """collect_canary_metrics 태스크 테스트."""

    def test_task_success(self):
        """태스크 성공."""
        with patch(
            "baldur_pro.services.canary.get_canary_rollout_service"
        ) as mock_get_service:
            mock_service = Mock()

            mock_rollout = Mock()
            mock_rollout.id = "test123"
            mock_service.get_active_rollouts.return_value = [mock_rollout]
            mock_service.collect_metrics.return_value = [Mock()]
            mock_get_service.return_value = mock_service

            result = collect_canary_metrics()

            assert result["success"] is True
            assert result["rollout_count"] == 1
            assert result["metrics_collected"] == 1


# =============================================================================
# Singleton Tests
# =============================================================================


# =============================================================================
# Watchdog Governance Settings Tests (377)
# =============================================================================


class TestWatchdogGovernanceSettingsBehavior:
    """Watchdog governance settings integration (377)."""

    def test_auto_promote_loads_emergency_min_level_from_settings(
        self,
        watchdog_config,
        sample_rollout,
    ):
        """auto_promote_eligible loads emergency_min_level from CanaryGovernanceSettings."""
        sample_rollout.created_at = utc_now() - timedelta(minutes=10)

        watchdog = RolloutWatchdog(config=watchdog_config)
        mock_service = Mock()
        mock_service.get_active_rollouts.return_value = [sample_rollout]
        mock_service.promote.return_value = True
        watchdog._service = mock_service

        mock_governance = Mock()
        mock_governance.allowed = True

        mock_settings = Mock()
        mock_settings.promote_emergency_min_level = 3

        with patch(
            "baldur_pro.services.governance.checks.check_all_governance",
            return_value=mock_governance,
        ) as mock_gov:
            with patch(
                "baldur.settings.canary_governance.get_canary_governance_settings",
                return_value=mock_settings,
            ):
                watchdog.auto_promote_eligible()

        call_kwargs = mock_gov.call_args.kwargs
        assert call_kwargs["emergency_min_level"] == 3

    def test_auto_promote_import_error_logs_warning_and_blocks(self, watchdog_config):
        """ImportError triggers logger.warning (not debug) and blocks (fail-closed)."""
        watchdog = RolloutWatchdog(config=watchdog_config)
        mock_service = Mock()
        watchdog._service = mock_service

        with patch(
            "baldur_pro.services.governance.checks.check_all_governance",
            side_effect=ImportError("No module"),
        ):
            result = watchdog.auto_promote_eligible()

        assert result.governance_blocked is True
        assert "not available" in result.governance_block_reason


class TestSingleton:
    """싱글톤 패턴 테스트."""

    def test_get_rollout_watchdog_same_instance(self):
        """싱글톤이 같은 인스턴스를 반환."""
        w1 = get_rollout_watchdog()
        w2 = get_rollout_watchdog()

        assert w1 is w2

    def test_reset_watchdog_creates_new_instance(self):
        """리셋 후 새 인스턴스 생성."""
        w1 = get_rollout_watchdog()
        reset_watchdog()
        w2 = get_rollout_watchdog()

        assert w1 is not w2
