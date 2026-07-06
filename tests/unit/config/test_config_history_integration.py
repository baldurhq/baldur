"""
Config History Integration Tests.

RuntimeConfigManager와 ConfigHistory 자동 연동 테스트.
설정 변경 시 자동으로 이력이 기록되고 롤백 가능한지 검증합니다.
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

import pytest

from baldur.services.config_history import (
    ConfigHistoryService,
    reset_config_history_service,
)
from baldur_pro.services.runtime_config import (
    RuntimeConfigManager,
    reset_runtime_config_manager,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def reset_singletons():
    """각 테스트 전후로 싱글톤 리셋."""
    reset_runtime_config_manager()
    reset_config_history_service()
    yield
    reset_runtime_config_manager()
    reset_config_history_service()


@pytest.fixture
def mock_state_backend():
    """StateBackend mock with real persistence + version-CAS semantics.

    The config write path is backend-authoritative (666 D2): each write re-reads
    the blob fresh from the backend and version-guards via ``compare_and_set``.
    A non-persisting mock (``get`` always ``None``) would make every same-value
    write look like a change, so the fake is dict-backed and implements the CAS
    contract. Stays a ``MagicMock`` so call tracking is preserved.
    """
    with patch("baldur_pro.services.runtime_config.base.get_state_backend") as mock:
        store: dict = {}

        def _get(key, default=None):
            return store.get(key, default)

        def _set(key, value, **kwargs):
            store[key] = value
            return True

        def _cas(key, expected_version, new_value, *, version_field="__occ_version__"):
            existing = store.get(key)
            current = (
                existing.get(version_field, 0) if isinstance(existing, dict) else 0
            )
            if current != expected_version:
                return False
            store[key] = new_value
            return True

        backend = MagicMock()
        backend.get.side_effect = _get
        backend.set.side_effect = _set
        backend.compare_and_set.side_effect = _cas
        mock.return_value = backend
        yield backend


# =============================================================================
# RuntimeConfigManager History Integration Tests
# =============================================================================


class TestRuntimeConfigHistoryIntegration:
    """RuntimeConfigManager와 ConfigHistory 연동 테스트."""

    def test_update_config_saves_to_history(self, mock_state_backend):
        """설정 업데이트 시 ConfigHistory에 자동 저장."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_get.return_value = mock_service

            manager = RuntimeConfigManager()

            # Update config
            manager._update_config(
                "circuit_breaker",
                changed_by="admin",
                reason="Test update",
                failure_threshold=10,
            )

            # Verify history was saved
            mock_service.save_version.assert_called_once()
            call_kwargs = mock_service.save_version.call_args[1]
            assert call_kwargs["config_type"] == "circuit_breaker"
            assert call_kwargs["changed_by"] == "admin"
            assert "Test update" in call_kwargs["reason"]

    def test_update_config_with_default_changed_by(self, mock_state_backend):
        """changed_by 미지정 시 기본값 'system' 사용."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_get.return_value = mock_service

            manager = RuntimeConfigManager()
            manager._cache = {}  # Clear cache for fresh start

            # Use a value different from default (default max_replay_attempts is 2)
            manager._update_config(
                "dlq",
                max_replay_attempts=5,
            )

            mock_service.save_version.assert_called_once()
            call_kwargs = mock_service.save_version.call_args[1]
            assert call_kwargs["changed_by"] == "system"

    def test_update_config_includes_field_names_in_reason(self, mock_state_backend):
        """reason 미지정 시 변경된 필드명 포함."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_get.return_value = mock_service

            manager = RuntimeConfigManager()

            manager._update_config(
                "retry",
                max_attempts=5,
            )

            mock_service.save_version.assert_called_once()
            call_kwargs = mock_service.save_version.call_args[1]
            assert "max_attempts" in call_kwargs["reason"]

    def test_history_save_failure_does_not_break_config_update(
        self, mock_state_backend
    ):
        """History 저장 실패해도 설정 변경은 성공."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_service.save_version.side_effect = Exception("Redis connection failed")
            mock_get.return_value = mock_service

            manager = RuntimeConfigManager()

            # Should not raise exception
            result = manager._update_config(
                "circuit_breaker",
                changed_by="admin",
                failure_threshold=15,
            )

            # Config update should still succeed
            assert result is not None
            assert result.get("failure_threshold") == 15


class TestDiffAwareSaving:
    """Diff-Aware Saving 테스트."""

    def test_no_save_when_no_changes(self, mock_state_backend):
        """동일한 값으로 업데이트 시 두 번째 호출은 History 저장 안 함."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_get.return_value = mock_service

            manager = RuntimeConfigManager()

            # First update - should save
            manager._update_config(
                "circuit_breaker",
                failure_threshold=10,
            )
            first_call_count = mock_service.save_version.call_count

            # Same value update - should NOT save again (diff-aware)
            manager._update_config(
                "circuit_breaker",
                failure_threshold=10,  # Same value
            )

            # Should be same count (no new save)
            assert mock_service.save_version.call_count == first_call_count

    def test_save_when_value_changes(self, mock_state_backend):
        """값이 실제로 변경될 때 저장."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_get.return_value = mock_service

            manager = RuntimeConfigManager()
            manager._cache = {}  # Clear cache for fresh start

            # Initial update with value different from default (default max_replay_attempts is 2)
            manager._update_config(
                "dlq",
                max_replay_attempts=5,
            )
            initial_calls = mock_service.save_version.call_count
            assert initial_calls >= 1  # First call should save

            # Different value - should save again
            manager._update_config(
                "dlq",
                max_replay_attempts=8,
            )

            assert mock_service.save_version.call_count == initial_calls + 1


class TestSafeDefaultTracking:
    """Safe Default 적용 추적 테스트."""

    def test_safe_default_applied_in_reason(self, mock_state_backend):
        """Safe Default 적용 시 reason에 표식 추가."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_get.return_value = mock_service

            # Mock safe_defaults to return a value different from current
            with patch("baldur.core.safe_defaults.is_valid_value", return_value=False):
                with patch(
                    "baldur.core.safe_defaults.get_safe_default", return_value=99
                ):
                    manager = RuntimeConfigManager()
                    manager._update_config(
                        "circuit_breaker",
                        changed_by="user",
                        reason="User request",
                        failure_threshold=0,  # Invalid value, will be replaced with 99
                    )

                    # Verify safe default marker in reason
                    mock_service.save_version.assert_called_once()
                    call_kwargs = mock_service.save_version.call_args[1]
                    assert "⚠️ Safe Default applied" in call_kwargs["reason"]

    def test_valid_value_no_safe_default_marker(self, mock_state_backend):
        """유효한 값은 Safe Default 표식 없음."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_get.return_value = mock_service

            manager = RuntimeConfigManager()

            manager._update_config(
                "circuit_breaker",
                changed_by="admin",
                reason="Normal update",
                failure_threshold=10,  # Valid value
            )

            if mock_service.save_version.called:
                call_kwargs = mock_service.save_version.call_args[1]
                # Valid value means no safe default applied
                assert "⚠️ Safe Default" not in call_kwargs["reason"]


class TestUpdateWithStrategyHistoryIntegration:
    """update_with_strategy와 History 연동 테스트."""

    def test_immediate_strategy_saves_history(self, mock_state_backend):
        """IMMEDIATE 전략 시 History에 저장."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_get.return_value = mock_service

            manager = RuntimeConfigManager()

            manager.update_with_strategy(
                config_type="circuit_breaker",
                changes={"failure_threshold": 15},
                changed_by="api_user",
                reason="API update",
                strategy="immediate",
            )

            mock_service.save_version.assert_called()
            call_kwargs = mock_service.save_version.call_args[1]
            assert call_kwargs["changed_by"] == "api_user"
            assert "API update" in call_kwargs["reason"]

    def test_update_with_strategy_default_changed_by(self, mock_state_backend):
        """update_with_strategy changed_by 기본값 테스트."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_get.return_value = mock_service

            manager = RuntimeConfigManager()

            manager.update_with_strategy(
                config_type="dlq",
                changes={"max_size": 3000},
                strategy="immediate",
            )

            if mock_service.save_version.called:
                call_kwargs = mock_service.save_version.call_args[1]
                assert call_kwargs["changed_by"] == "system"


class TestSupportedConfigTypes:
    """SUPPORTED_CONFIG_TYPES 확장 테스트."""

    def test_emergency_in_supported_types(self):
        """emergency가 SUPPORTED_CONFIG_TYPES에 포함."""
        assert "emergency" in ConfigHistoryService.SUPPORTED_CONFIG_TYPES

    def test_logging_in_supported_types(self):
        """logging이 SUPPORTED_CONFIG_TYPES에 포함."""
        assert "logging" in ConfigHistoryService.SUPPORTED_CONFIG_TYPES

    def test_chaos_in_supported_types(self):
        """chaos가 SUPPORTED_CONFIG_TYPES에 포함."""
        assert "chaos" in ConfigHistoryService.SUPPORTED_CONFIG_TYPES

    def test_all_new_types_are_valid(self):
        """새로 추가된 타입들이 유효성 검사 통과."""
        service = ConfigHistoryService()

        assert service.is_valid_config_type("emergency")
        assert service.is_valid_config_type("logging")
        assert service.is_valid_config_type("chaos")


class TestSaveToHistoryHelper:
    """_save_to_history 헬퍼 메서드 테스트."""

    def test_save_to_history_graceful_degradation(self, mock_state_backend):
        """_save_to_history는 예외를 전파하지 않음."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_get.side_effect = Exception("Service unavailable")

            manager = RuntimeConfigManager()

            # Should not raise
            manager._save_to_history(
                config_type="circuit_breaker",
                values={"failure_threshold": 10},
                changed_by="test",
                reason="test",
            )

    def test_save_to_history_logs_warning_on_failure(self, mock_state_backend):
        """_save_to_history 실패 시 경고 로그."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_service.save_version.side_effect = Exception("Save failed")
            mock_get.return_value = mock_service

            with patch("baldur_pro.services.runtime_config.base.logger") as mock_logger:
                manager = RuntimeConfigManager()
                manager._save_to_history(
                    config_type="dlq",
                    values={"max_size": 1000},
                    changed_by="test",
                    reason="test",
                )

                mock_logger.warning.assert_called()
                warning_msg = mock_logger.warning.call_args[0][0]
                assert warning_msg == "runtime_config.save_history_failed"


class TestApplyPendingChangeHistory:
    """apply_pending_change History 연동 테스트."""

    def test_apply_pending_change_saves_history(self, mock_state_backend):
        """apply_pending_change 시 History 저장."""
        with patch(
            "baldur.services.config_history.get_config_history_service"
        ) as mock_hist:
            mock_hist_service = MagicMock()
            mock_hist.return_value = mock_hist_service

            with patch(
                "baldur.services.pending_config.get_pending_config_service"
            ) as mock_pending:
                mock_pending_service = MagicMock()
                mock_change = MagicMock()
                mock_change.config_type = "circuit_breaker"
                mock_change.changes = {"failure_threshold": 20}
                mock_change.status = "pending"
                # 665 D7 — the deferred apply preserves the requesting operator's
                # identity through to the audit/history (was hardcoded
                # "pending_config_worker").
                mock_change.requested_by = "alice"
                mock_pending_service.get_pending_change.return_value = mock_change
                mock_pending.return_value = mock_pending_service

                manager = RuntimeConfigManager()
                manager.apply_pending_change("test-pending-id")

                # Verify history was saved with appropriate metadata
                mock_hist_service.save_version.assert_called_once()
                call_kwargs = mock_hist_service.save_version.call_args[1]
                assert call_kwargs["changed_by"] == "alice"
                assert "test-pending-id" in call_kwargs["reason"]
