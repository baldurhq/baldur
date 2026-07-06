"""
CleanupService, PendingConfigService, ConfigHistoryService audit 호출 검증.

각 서비스 메서드가 올바른 audit 함수를 호출하는지 mock으로 검증.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestCleanupServiceAudit:
    """CleanupService audit 함수 호출 검증."""

    def test_archive_old_dlq_entries_calls_log_system_control_audit(self):
        """archive_old_dlq_entries가 log_system_control_audit를 호출."""
        pytest.importorskip("baldur_pro")
        with patch("baldur_pro.services.dlq.get_dlq_service") as mock_dlq:
            dlq_svc = MagicMock()
            dlq_svc.archive_old_entries.return_value = 5
            mock_dlq.return_value = dlq_svc

            with patch(
                "baldur.services.cleanup_service.log_system_control_audit"
            ) as mock_audit:
                from baldur.services.cleanup_service import CleanupService

                service = CleanupService()
                result = service.archive_old_dlq_entries(older_than_days=30)

                assert result.success is True
                mock_audit.assert_called_once()
                call_kwargs = mock_audit.call_args.kwargs
                assert call_kwargs["action"] == "archive_dlq"
                assert call_kwargs["actor"] == "system"

    def test_cleanup_expired_config_calls_log_system_control_audit(self):
        """cleanup_expired_config가 log_system_control_audit를 호출."""
        with patch(
            "baldur.services.pending_config.get_pending_config_service"
        ) as mock_pending:
            pending_svc = MagicMock()
            pending_svc.cleanup_expired.return_value = 7
            mock_pending.return_value = pending_svc

            with patch(
                "baldur.services.cleanup_service.log_system_control_audit"
            ) as mock_audit:
                from baldur.services.cleanup_service import CleanupService

                service = CleanupService()
                result = service.cleanup_expired_config(older_than_hours=24)

                assert result.success is True
                mock_audit.assert_called_once()
                call_kwargs = mock_audit.call_args.kwargs
                assert call_kwargs["action"] == "cleanup_expired_config"

    def test_purge_archived_dlq_dry_run_calls_log_system_control_audit(self):
        """purge_archived_dlq_entries dry_run이 log_system_control_audit를 호출."""
        pytest.importorskip("baldur_pro")
        with patch("baldur_pro.services.dlq.get_dlq_service") as mock_dlq:
            dlq_svc = MagicMock()
            dlq_svc.count_archived_older_than.return_value = 10
            mock_dlq.return_value = dlq_svc

            with patch(
                "baldur.services.cleanup_service.log_system_control_audit"
            ) as mock_audit:
                from baldur.services.cleanup_service import CleanupService

                service = CleanupService()
                result = service.purge_archived_dlq_entries(
                    older_than_days=90, dry_run=True
                )

                assert result.success is True
                mock_audit.assert_called_once()
                call_kwargs = mock_audit.call_args.kwargs
                assert call_kwargs["action"] == "purge_dlq_dry_run"

    def test_purge_archived_dlq_permanent_calls_log_system_control_audit(self):
        """purge_archived_dlq_entries 영구삭제가 log_system_control_audit를 호출."""
        pytest.importorskip("baldur_pro")
        with patch("baldur_pro.services.dlq.get_dlq_service") as mock_dlq:
            dlq_svc = MagicMock()
            dlq_svc.purge_archived.return_value = 3
            mock_dlq.return_value = dlq_svc

            with patch(
                "baldur.services.cleanup_service.log_system_control_audit"
            ) as mock_audit:
                from baldur.services.cleanup_service import CleanupService

                service = CleanupService()
                result = service.purge_archived_dlq_entries(
                    older_than_days=90, dry_run=False
                )

                assert result.success is True
                mock_audit.assert_called_once()
                call_kwargs = mock_audit.call_args.kwargs
                assert call_kwargs["action"] == "purge_dlq_permanent"
                assert call_kwargs["new_state"]["permanent"] is True


class TestPendingConfigServiceAudit:
    """PendingConfigService audit 함수 호출 검증."""

    def test_cancel_pending_change_calls_log_config_apply_audit(self):
        """cancel_pending_change가 log_config_apply_audit를 호출."""
        with patch("baldur.services.pending_config.get_state_backend") as mock_backend:
            backend = MagicMock()
            backend.get.return_value = None
            mock_backend.return_value = backend

            from baldur.core.apply_strategy import ApplyOptions, ApplyStrategy
            from baldur.services.pending_config import PendingConfigService

            service = PendingConfigService()

            change = service.create_pending_change(
                config_type="circuit_breaker",
                changes={"threshold": 10},
                apply_options=ApplyOptions(strategy=ApplyStrategy.IMMEDIATE),
                previous_values={"threshold": 5},
            )

            with patch(
                "baldur.services.pending_config.log_config_apply_audit"
            ) as mock_audit:
                result = service.cancel_pending_change(change.id, cancelled_by="admin")

                assert result is not None
                assert result.status == "cancelled"
                mock_audit.assert_called_once()
                call_kwargs = mock_audit.call_args.kwargs
                assert call_kwargs["status"] == "cancelled"
                assert call_kwargs["config_key"] == "circuit_breaker"


class TestConfigHistoryServiceAudit:
    """ConfigHistoryService audit 함수 호출 검증."""

    @pytest.fixture
    def mock_store(self):
        """ConfigHistoryStore mock."""
        store = MagicMock()
        store.next_version.return_value = 1
        store.save_version.return_value = None
        store.get_history.return_value = []
        store.get_current.return_value = None
        return store

    def test_save_version_calls_log_config_apply_audit(self, mock_store):
        """save_version이 log_config_apply_audit를 호출."""
        with patch(
            "baldur.services.config_history.service.log_config_apply_audit"
        ) as mock_audit:
            from baldur.services.config_history import ConfigHistoryService

            service = ConfigHistoryService(store=mock_store)

            result = service.save_version(
                config_type="circuit_breaker",
                values={"failure_threshold": 10},
                changed_by="admin",
                reason="Increase threshold",
            )

            assert result is not None
            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args.kwargs
            assert call_kwargs["status"] == "applied"
            assert call_kwargs["config_key"] == "circuit_breaker"

    def test_rollback_calls_log_rollback_audit(self, mock_store):
        """rollback이 log_rollback_audit를 호출."""
        version_data = {
            "version": 1,
            "timestamp": 1700000000.0,
            "config_type": "circuit_breaker",
            "values": {"failure_threshold": 5},
            "changed_by": "admin",
            "reason": "Initial",
            "hash": "abc123",
        }
        mock_store.get_history.return_value = [version_data]
        mock_store.next_version.return_value = 2

        with patch("baldur.services.config_history.service.log_config_apply_audit"):
            with patch(
                "baldur.services.config_history.service.log_rollback_audit"
            ) as mock_rollback_audit:
                from baldur.services.config_history import ConfigHistoryService

                service = ConfigHistoryService(store=mock_store)

                result = service.rollback(
                    config_type="circuit_breaker",
                    target_version=1,
                    rolled_back_by="admin",
                )

                assert result is not None
                mock_rollback_audit.assert_called_once()
                call_kwargs = mock_rollback_audit.call_args.kwargs
                assert call_kwargs["state"] == "completed"
                assert call_kwargs["triggered_by"] == "admin"
