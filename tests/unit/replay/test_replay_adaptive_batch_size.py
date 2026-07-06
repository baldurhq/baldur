"""
Adaptive Batch Size Tests (동적 배치 크기 조정).

Tests for:
1. AdaptiveReplayConfig dataclass
2. AdaptiveReplayManager singleton
3. Batch size adjustment logic
4. ReplayService adaptive mode integration
5. RuntimeConfig integration
"""

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# AdaptiveReplayConfig Tests
# =============================================================================


class TestAdaptiveReplayConfig:
    """AdaptiveReplayConfig dataclass 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        from baldur.services.adaptive_replay import AdaptiveReplayConfig

        config = AdaptiveReplayConfig()

        assert config.min_items == 10
        assert config.max_items == 100
        assert config.initial_items == 50
        assert config.decrease_ratio == 0.8
        assert config.increase_step == 5
        assert config.failure_threshold == 0.2
        assert config.success_streak_required == 3

    def test_custom_values(self):
        """커스텀 값 설정."""
        from baldur.services.adaptive_replay import AdaptiveReplayConfig

        config = AdaptiveReplayConfig(
            min_items=5,
            max_items=200,
            initial_items=100,
            decrease_ratio=0.7,
            increase_step=10,
            failure_threshold=0.3,
            success_streak_required=5,
        )

        assert config.min_items == 5
        assert config.max_items == 200
        assert config.initial_items == 100
        assert config.decrease_ratio == 0.7
        assert config.increase_step == 10
        assert config.failure_threshold == 0.3
        assert config.success_streak_required == 5


# =============================================================================
# AdaptiveReplayManager Tests
# =============================================================================


class TestAdaptiveReplayManager:
    """AdaptiveReplayManager 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.services.adaptive_replay import reset_adaptive_replay_manager

        reset_adaptive_replay_manager()
        yield
        reset_adaptive_replay_manager()

    def test_singleton_pattern(self):
        """싱글톤 패턴 확인."""
        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager1 = get_adaptive_replay_manager()
        manager2 = get_adaptive_replay_manager()

        assert manager1 is manager2

    def test_initial_max_items(self):
        """초기 max_items 값 확인."""
        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager = get_adaptive_replay_manager()
        # Default initial_items = 50
        assert manager.get_current_max_items() == 50

    def test_configure_updates_config(self):
        """configure()가 설정을 업데이트하는지 확인."""
        from baldur.services.adaptive_replay import (
            AdaptiveReplayConfig,
            get_adaptive_replay_manager,
        )

        manager = get_adaptive_replay_manager()
        new_config = AdaptiveReplayConfig(
            min_items=20,
            max_items=80,
            initial_items=40,
        )

        manager.configure(new_config)
        stats = manager.get_stats()

        assert stats["config"]["min_items"] == 20
        assert stats["config"]["max_items"] == 80

    def test_configure_clamps_current_items_to_bounds(self):
        """configure()가 current_items를 새 범위에 맞게 조정하는지 확인."""
        from baldur.services.adaptive_replay import (
            AdaptiveReplayConfig,
            get_adaptive_replay_manager,
        )

        manager = get_adaptive_replay_manager()
        # current = 50

        # Clamp to max (30)
        manager.configure(AdaptiveReplayConfig(min_items=10, max_items=30))
        assert manager.get_current_max_items() == 30

        # Clamp to min (40)
        manager.configure(AdaptiveReplayConfig(min_items=40, max_items=100))
        assert manager.get_current_max_items() == 40

    def test_high_failure_rate_reduces_batch_size(self):
        """실패율 20% 이상 시 배치 크기 감소."""
        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager = get_adaptive_replay_manager()
        initial = manager.get_current_max_items()  # 50

        # 25% failure rate (>= 20% threshold)
        manager.record_batch_result(total=100, success=75, failures=25)

        new_items = manager.get_current_max_items()
        # 50 * 0.8 = 40
        assert new_items == 40
        assert new_items < initial

    def test_perfect_streak_increases_batch_size(self):
        """3연속 성공 시 배치 크기 증가."""
        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager = get_adaptive_replay_manager()
        initial = manager.get_current_max_items()  # 50

        # 3 consecutive perfect batches
        manager.record_batch_result(total=50, success=50, failures=0)
        manager.record_batch_result(total=50, success=50, failures=0)
        manager.record_batch_result(total=50, success=50, failures=0)

        new_items = manager.get_current_max_items()
        # 50 + 5 = 55
        assert new_items == 55
        assert new_items > initial

    def test_partial_success_resets_streak(self):
        """부분 성공 시 streak 리셋."""
        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager = get_adaptive_replay_manager()

        # 2 perfect batches
        manager.record_batch_result(total=50, success=50, failures=0)
        manager.record_batch_result(total=50, success=50, failures=0)

        assert manager.get_stats()["success_streak"] == 2

        # 1 partial failure (below threshold)
        manager.record_batch_result(total=50, success=45, failures=5)

        assert manager.get_stats()["success_streak"] == 0
        # Batch size should not change (failure rate 10% < 20%)
        assert manager.get_current_max_items() == 50

    def test_min_items_floor(self):
        """min_items 하한선 확인."""
        from baldur.services.adaptive_replay import (
            AdaptiveReplayConfig,
            get_adaptive_replay_manager,
        )

        manager = get_adaptive_replay_manager()
        manager.configure(
            AdaptiveReplayConfig(min_items=10, max_items=100, initial_items=15)
        )

        # Force 여러 번 감소시도
        for _ in range(10):
            manager.record_batch_result(total=10, success=5, failures=5)  # 50% failure

        # Should not go below min_items
        assert manager.get_current_max_items() >= 10

    def test_max_items_ceiling(self):
        """max_items 상한선 확인."""
        from baldur.services.adaptive_replay import (
            AdaptiveReplayConfig,
            get_adaptive_replay_manager,
        )

        manager = get_adaptive_replay_manager()
        manager.configure(
            AdaptiveReplayConfig(min_items=10, max_items=60, initial_items=50)
        )

        # 3연속 성공으로 증가 시도
        for _ in range(10):
            manager.record_batch_result(total=50, success=50, failures=0)
            manager.record_batch_result(total=50, success=50, failures=0)
            manager.record_batch_result(total=50, success=50, failures=0)

        # Should not exceed max_items
        assert manager.get_current_max_items() <= 60

    def test_empty_batch_skipped(self):
        """빈 배치는 기록하지 않음."""
        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager = get_adaptive_replay_manager()
        initial = manager.get_current_max_items()

        manager.record_batch_result(total=0, success=0, failures=0)

        assert manager.get_current_max_items() == initial
        assert manager.get_stats()["history"]["total_batches"] == 0

    def test_reset_returns_to_initial(self):
        """reset()이 초기 상태로 되돌리는지 확인."""
        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager = get_adaptive_replay_manager()

        # Change state
        manager.record_batch_result(total=100, success=50, failures=50)
        assert manager.get_current_max_items() != 50

        # Reset
        manager.reset()

        assert manager.get_current_max_items() == 50
        assert manager.get_stats()["success_streak"] == 0
        assert manager.get_stats()["history"]["total_batches"] == 0

    def test_get_stats_returns_complete_info(self):
        """get_stats()가 완전한 정보를 반환하는지 확인."""
        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager = get_adaptive_replay_manager()
        manager.record_batch_result(total=50, success=50, failures=0)

        stats = manager.get_stats()

        assert "current_max_items" in stats
        assert "success_streak" in stats
        assert "config" in stats
        assert "history" in stats

        assert stats["config"]["min_items"] == 10
        assert stats["config"]["max_items"] == 100
        assert stats["history"]["total_batches"] == 1

    def test_history_keeps_last_100_entries(self):
        """히스토리가 최근 100개만 유지하는지 확인."""
        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager = get_adaptive_replay_manager()

        # Record 110 batches
        for _i in range(110):
            manager.record_batch_result(total=10, success=9, failures=1)

        stats = manager.get_stats()
        assert stats["history"]["total_batches"] == 100


# =============================================================================
# ReplayService Adaptive Integration Tests
# =============================================================================


class TestReplayServiceAdaptiveIntegration:
    """ReplayService와 Adaptive 모드 통합 테스트."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # Every test patches baldur_pro.services.governance.checks (PRO-tier).
        pytest.importorskip("baldur_pro")

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.services.adaptive_replay import reset_adaptive_replay_manager

        reset_adaptive_replay_manager()
        yield
        reset_adaptive_replay_manager()

    @pytest.fixture
    def mock_repository(self):
        """Mock repository."""
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.find_replayable.return_value = []
        return mock

    @pytest.fixture
    def replay_service(self, mock_repository):
        """ReplayService 인스턴스."""
        from baldur.services.replay_service import ReplayService

        return ReplayService(repository=mock_repository)

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    def test_adaptive_mode_disabled_uses_provided_max_items(
        self, mock_governance, replay_service, mock_repository
    ):
        """Adaptive 모드 비활성화 시 제공된 max_items 사용."""
        mock_governance.return_value = MagicMock(allowed=True)

        # Adaptive 모드 비활성화
        with patch.object(replay_service, "_is_adaptive_enabled", return_value=False):
            replay_service.replay_batch(max_items=75)

        mock_repository.find_replayable.assert_called_once()
        call_args = mock_repository.find_replayable.call_args
        assert call_args.kwargs.get("limit") == 75 or call_args[1].get("limit") == 75

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    def test_adaptive_mode_enabled_uses_manager_value(
        self, mock_governance, replay_service, mock_repository
    ):
        """Adaptive 모드 활성화 시 AdaptiveReplayManager 값 사용."""
        from baldur.services.adaptive_replay import (
            AdaptiveReplayConfig,
            get_adaptive_replay_manager,
        )

        mock_governance.return_value = MagicMock(allowed=True)

        # Manager의 current_items는 50으로 초기화됨 (initial_items default)
        # max_items=40으로 설정하면 current_items는 40으로 clamped됨
        manager = get_adaptive_replay_manager()
        manager.configure(
            AdaptiveReplayConfig(min_items=10, max_items=40, initial_items=50)
        )

        # Adaptive 모드 활성화
        with patch.object(replay_service, "_is_adaptive_enabled", return_value=True):
            with patch.object(
                replay_service,
                "_get_adaptive_config",
                return_value=AdaptiveReplayConfig(
                    min_items=10, max_items=40, initial_items=50
                ),
            ):
                replay_service.replay_batch(max_items=100)

        mock_repository.find_replayable.assert_called_once()
        call_args = mock_repository.find_replayable.call_args
        # Should use adaptive value (40, clamped from 50), not provided (100)
        limit = call_args.kwargs.get("limit") or call_args[1].get("limit")
        assert limit == 40

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    def test_use_adaptive_override_true(
        self, mock_governance, replay_service, mock_repository
    ):
        """use_adaptive=True 파라미터로 강제 활성화."""
        from baldur.services.adaptive_replay import (
            AdaptiveReplayConfig,
            get_adaptive_replay_manager,
        )

        mock_governance.return_value = MagicMock(allowed=True)

        # max_items=30으로 설정하여 current_items=30으로 clamp
        manager = get_adaptive_replay_manager()
        manager.configure(
            AdaptiveReplayConfig(min_items=10, max_items=30, initial_items=50)
        )

        # _is_adaptive_enabled가 False여도 use_adaptive=True면 활성화
        with patch.object(replay_service, "_is_adaptive_enabled", return_value=False):
            with patch.object(
                replay_service,
                "_get_adaptive_config",
                return_value=AdaptiveReplayConfig(
                    min_items=10, max_items=30, initial_items=50
                ),
            ):
                replay_service.replay_batch(max_items=100, use_adaptive=True)

        call_args = mock_repository.find_replayable.call_args
        limit = call_args.kwargs.get("limit") or call_args[1].get("limit")
        assert limit == 30

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    def test_use_adaptive_override_false(
        self, mock_governance, replay_service, mock_repository
    ):
        """use_adaptive=False 파라미터로 강제 비활성화."""
        mock_governance.return_value = MagicMock(allowed=True)

        # _is_adaptive_enabled가 True여도 use_adaptive=False면 비활성화
        with patch.object(replay_service, "_is_adaptive_enabled", return_value=True):
            replay_service.replay_batch(max_items=88, use_adaptive=False)

        call_args = mock_repository.find_replayable.call_args
        limit = call_args.kwargs.get("limit") or call_args[1].get("limit")
        assert limit == 88

    @patch("baldur_pro.services.governance.checks.check_all_governance")
    def test_batch_result_recorded_in_adaptive_mode(
        self, mock_governance, replay_service, mock_repository
    ):
        """Adaptive 모드에서 배치 결과가 기록되는지 확인."""
        from dataclasses import dataclass

        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        mock_governance.return_value = MagicMock(allowed=True)

        # Create mock entries that will succeed
        @dataclass
        class MockEntry:
            id: int

        mock_repository.find_replayable.return_value = [
            MockEntry(id=1),
            MockEntry(id=2),
            MockEntry(id=3),
        ]

        manager = get_adaptive_replay_manager()

        with patch.object(replay_service, "_is_adaptive_enabled", return_value=True):
            with patch.object(
                replay_service,
                "_execute_replay",
                side_effect=[
                    MagicMock(success=True),
                    MagicMock(success=True),
                    MagicMock(success=False),
                ],
            ):
                replay_service.replay_batch(use_adaptive=True)

        stats = manager.get_stats()
        assert stats["history"]["total_batches"] == 1


# =============================================================================
# RuntimeConfig Integration Tests
# =============================================================================


class TestRuntimeConfigIntegration:
    """RuntimeConfig 통합 테스트."""

    def test_replay_automation_config_has_adaptive_fields(self):
        """ReplayAutomationConfig에 adaptive 필드가 있는지 확인."""
        from baldur.core.config import ReplayAutomationConfig

        config = ReplayAutomationConfig()

        assert hasattr(config, "adaptive_enabled")
        assert hasattr(config, "adaptive_min_items")
        assert hasattr(config, "adaptive_max_items")
        assert hasattr(config, "adaptive_failure_threshold")

        # Check default values
        assert config.adaptive_enabled is False
        assert config.adaptive_min_items == 10
        assert config.adaptive_max_items == 100
        assert config.adaptive_failure_threshold == 0.2

    def test_config_classes_has_replay_automation(self):
        """CONFIG_CLASSES에 replay_automation이 있는지 확인."""
        pytest.importorskip("baldur_pro")
        from baldur.core.config import ReplayAutomationConfig
        from baldur_pro.services.runtime_config.constants import CONFIG_CLASSES

        assert "replay_automation" in CONFIG_CLASSES
        assert CONFIG_CLASSES["replay_automation"] is ReplayAutomationConfig


# =============================================================================
# Thread Safety Tests
# =============================================================================


class TestThreadSafety:
    """Thread safety 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.services.adaptive_replay import reset_adaptive_replay_manager

        reset_adaptive_replay_manager()
        yield
        reset_adaptive_replay_manager()

    def test_concurrent_record_batch_result(self):
        """동시에 여러 스레드가 record_batch_result 호출해도 안전한지."""
        import threading

        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager = get_adaptive_replay_manager()
        errors = []

        def record_batch():
            try:
                for _ in range(50):
                    manager.record_batch_result(total=10, success=9, failures=1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_batch) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # History should have entries from all threads
        stats = manager.get_stats()
        assert stats["history"]["total_batches"] == 100  # Capped at 100

    def test_concurrent_get_current_max_items(self):
        """동시에 여러 스레드가 get_current_max_items 호출해도 안전한지."""
        import threading

        from baldur.services.adaptive_replay import get_adaptive_replay_manager

        manager = get_adaptive_replay_manager()
        results = []
        errors = []

        def get_items():
            try:
                for _ in range(100):
                    value = manager.get_current_max_items()
                    results.append(value)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=get_items) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 1000
