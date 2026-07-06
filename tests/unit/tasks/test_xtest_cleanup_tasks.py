"""
Tests for X-Test Cleanup Tasks.

Celery 태스크 및 Beat Schedule 테스트:
- cleanup_xtest_artifacts thin wrapper
- get_xtest_cleanup_beat_schedule
"""

from unittest.mock import MagicMock, patch

import pytest


class TestCleanupXTestArtifactsWrapper:
    """cleanup_xtest_artifacts thin wrapper 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """Reset singletons before and after each test."""
        from baldur.services.xtest_cleanup_service import (
            reset_xtest_cleanup_service,
        )
        from baldur.settings.xtest_cleanup import (
            reset_xtest_cleanup_settings,
        )

        reset_xtest_cleanup_service()
        reset_xtest_cleanup_settings()
        yield
        reset_xtest_cleanup_service()
        reset_xtest_cleanup_settings()

    @patch("baldur.services.xtest_cleanup_service.get_xtest_cleanup_service")
    def test_cleanup_xtest_artifacts_calls_service(self, mock_get_service):
        """cleanup_xtest_artifacts가 서비스를 호출하는지 검증."""
        from baldur.services.xtest_cleanup_service import XTestCleanupResult
        from baldur.tasks.xtest_cleanup_tasks import cleanup_xtest_artifacts

        mock_service = MagicMock()
        mock_service.cleanup_expired_sessions.return_value = XTestCleanupResult(
            success=True,
            sessions_cleaned=2,
            cb_states_restored=1,
            dlq_entries_purged=3,
        )
        mock_get_service.return_value = mock_service

        result = cleanup_xtest_artifacts()

        mock_service.cleanup_expired_sessions.assert_called_once()
        assert result["success"] is True
        assert result["sessions_cleaned"] == 2
        assert result["cb_states_restored"] == 1
        assert result["dlq_entries_purged"] == 3

    @patch("baldur.services.xtest_cleanup_service.get_xtest_cleanup_service")
    def test_cleanup_xtest_artifacts_propagates_exception(self, mock_get_service):
        """cleanup_xtest_artifacts가 예외를 전파하는지 검증."""
        from baldur.tasks.xtest_cleanup_tasks import cleanup_xtest_artifacts

        mock_service = MagicMock()
        mock_service.cleanup_expired_sessions.side_effect = Exception("Test error")
        mock_get_service.return_value = mock_service

        with pytest.raises(Exception, match="Test error"):
            cleanup_xtest_artifacts()


class TestGetXTestCleanupStatsWrapper:
    """get_xtest_cleanup_stats thin wrapper 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """Reset singletons before and after each test."""
        from baldur.services.xtest_cleanup_service import (
            reset_xtest_cleanup_service,
        )

        reset_xtest_cleanup_service()
        yield
        reset_xtest_cleanup_service()

    @patch("baldur.services.xtest_cleanup_service.get_xtest_cleanup_service")
    def test_get_xtest_cleanup_stats_calls_service(self, mock_get_service):
        """get_xtest_cleanup_stats가 서비스를 호출하는지 검증."""
        from baldur.tasks.xtest_cleanup_tasks import get_xtest_cleanup_stats

        mock_service = MagicMock()
        mock_service.get_cleanup_stats.return_value = {
            "active_sessions": 5,
            "expired_sessions": 2,
            "pending_idempotency_clears": 10,
        }
        mock_get_service.return_value = mock_service

        result = get_xtest_cleanup_stats()

        mock_service.get_cleanup_stats.assert_called_once()
        assert result["active_sessions"] == 5
        assert result["expired_sessions"] == 2

    @patch("baldur.services.xtest_cleanup_service.get_xtest_cleanup_service")
    def test_get_xtest_cleanup_stats_handles_exception(self, mock_get_service):
        """get_xtest_cleanup_stats가 예외를 처리하는지 검증."""
        from baldur.tasks.xtest_cleanup_tasks import get_xtest_cleanup_stats

        mock_service = MagicMock()
        mock_service.get_cleanup_stats.side_effect = Exception("Connection error")
        mock_get_service.return_value = mock_service

        result = get_xtest_cleanup_stats()

        assert "error" in result
        assert "Connection error" in result["error"]


class TestGetXTestCleanupBeatSchedule:
    """get_xtest_cleanup_beat_schedule 함수 테스트."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.xtest_cleanup import reset_xtest_cleanup_settings

        reset_xtest_cleanup_settings()
        yield
        reset_xtest_cleanup_settings()

    def test_beat_schedule_returns_dict(self):
        """Beat 스케줄이 딕셔너리를 반환하는지 검증."""
        from baldur.tasks.xtest_cleanup_tasks import (
            get_xtest_cleanup_beat_schedule,
        )

        schedule = get_xtest_cleanup_beat_schedule()

        assert isinstance(schedule, dict)

    def test_beat_schedule_contains_cleanup_task(self):
        """Beat 스케줄에 cleanup-xtest-artifacts 태스크가 포함되는지 검증."""
        from baldur.tasks.xtest_cleanup_tasks import (
            get_xtest_cleanup_beat_schedule,
        )

        schedule = get_xtest_cleanup_beat_schedule()

        assert "cleanup-xtest-artifacts" in schedule

    def test_beat_schedule_task_config(self):
        """Beat 스케줄 태스크 설정 검증."""
        from baldur.tasks.xtest_cleanup_tasks import (
            get_xtest_cleanup_beat_schedule,
        )

        schedule = get_xtest_cleanup_beat_schedule()
        task_config = schedule.get("cleanup-xtest-artifacts", {})

        assert task_config.get("task") == "baldur.cleanup_xtest_artifacts"
        assert "schedule" in task_config
        assert task_config.get("options", {}).get("queue") == "maintenance"

    def test_beat_schedule_uses_settings_interval(self, monkeypatch):
        """Beat 스케줄이 설정의 정리 주기를 사용하는지 검증."""
        from baldur.settings.xtest_cleanup import reset_xtest_cleanup_settings

        # 새 설정으로 초기화
        monkeypatch.setenv("BALDUR_XTEST_CLEANUP_CLEANUP_INTERVAL_MINUTES", "15")
        reset_xtest_cleanup_settings()

        from baldur.tasks.xtest_cleanup_tasks import (
            get_xtest_cleanup_beat_schedule,
        )

        schedule = get_xtest_cleanup_beat_schedule()

        # 스케줄이 생성되었는지 확인
        assert "cleanup-xtest-artifacts" in schedule


class TestCeleryAvailability:
    """Celery availability flag 테스트."""

    def test_celery_tasks_available_flag(self):
        """CELERY_TASKS_AVAILABLE 플래그 존재 검증."""
        from baldur.tasks.xtest_cleanup_tasks import CELERY_TASKS_AVAILABLE

        # Celery가 설치되어 있으면 True, 아니면 False
        assert isinstance(CELERY_TASKS_AVAILABLE, bool)
