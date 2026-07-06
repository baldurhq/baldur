"""
Tests for the post-416 thin AuditLogger shim.

After 416 Part 6 (H1/H2 unification), AuditLogger no longer owns a
backend. ``log_change()`` masks fields, builds an H1 ``AuditEntry``,
and delegates to ``ProviderRegistry.get_audit_adapter().log()``.

These tests cover:
- Event dataclass behavior (unchanged)
- Masking pipeline (IP / sensitive fields)
- Singleton lifecycle
- log_change return contract (D17)
- Routing through ProviderRegistry (mock adapter)
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.audit.hashchain_adapter import HashChainFileAuditLogAdapter
from baldur.audit import (
    AuditLogger,
    ConfigAuditAction,
    ConfigChangeEvent,
    get_audit_logger,
    log_config_change,
)
from baldur.interfaces.audit_adapter import AuditAction, AuditEntry


class TestConfigChangeEvent:
    """Tests for ConfigChangeEvent dataclass."""

    def test_create_event(self):
        event = ConfigChangeEvent(
            config_type="RETRY_CONFIG",
            config_key="max_retries",
            action=ConfigAuditAction.UPDATE,
            old_value=3,
            new_value=5,
            user="admin",
            reason="Increasing reliability",
        )

        assert event.config_type == "RETRY_CONFIG"
        assert event.config_key == "max_retries"
        assert event.action == ConfigAuditAction.UPDATE
        assert event.old_value == 3
        assert event.new_value == 5

    def test_to_dict(self):
        event = ConfigChangeEvent(
            config_type="CIRCUIT_BREAKER",
            config_key="failure_threshold",
            action=ConfigAuditAction.UPDATE,
            old_value=5,
            new_value=10,
        )

        result = event.to_dict()

        assert isinstance(result, dict)
        assert result["config_type"] == "CIRCUIT_BREAKER"
        assert result["action"] == "update"

    def test_string_action(self):
        event = ConfigChangeEvent(
            config_type="TEST",
            config_key="key",
            action="custom_action",
        )

        assert event.action == "custom_action"


@pytest.fixture
def mock_audit_adapter():
    """Patch ProviderRegistry.get_audit_adapter to return a Mock."""
    AuditLogger.reset_instance()
    captured_entries: list[AuditEntry] = []
    mock_adapter = MagicMock()
    mock_adapter.log.side_effect = lambda e: captured_entries.append(e)
    with patch(
        "baldur.factory.ProviderRegistry.get_audit_adapter",
        return_value=mock_adapter,
    ):
        yield mock_adapter, captured_entries
    AuditLogger.reset_instance()


class TestAuditLoggerRouting:
    """log_change() routes through ProviderRegistry.get_audit_adapter()."""

    def test_log_change_basic_routes_through_provider(self, mock_audit_adapter):
        adapter, entries = mock_audit_adapter
        logger = AuditLogger(enable_console_log=False)

        event = ConfigChangeEvent(
            config_type="RETRY_CONFIG",
            config_key="max_retries",
            action=ConfigAuditAction.UPDATE,
            old_value={"value": 3},
            new_value={"value": 5},
            user="admin",
        )

        result = logger.log_change(event)

        assert result is True  # D17: log() did not raise → True
        assert adapter.log.call_count == 1
        entry = entries[0]
        assert isinstance(entry, AuditEntry)
        assert entry.action == AuditAction.CONFIG_CHANGE
        assert entry.target_type == "RETRY_CONFIG"
        assert entry.target_id == "max_retries"
        assert entry.actor_id == "admin"
        assert entry.details["old_value"] == {"value": 3}
        assert entry.details["new_value"] == {"value": 5}

    def test_log_change_with_dict_event(self, mock_audit_adapter):
        adapter, entries = mock_audit_adapter
        logger = AuditLogger(enable_console_log=False)

        event_dict = {
            "config_type": "TIMEOUT_CONFIG",
            "config_key": "connect_timeout",
            "action": "update",
            "old_value": {"timeout": 30},
            "new_value": {"timeout": 60},
            "user": "system",
        }

        result = logger.log_change(event_dict)

        assert result is True
        assert entries[0].target_type == "TIMEOUT_CONFIG"
        assert entries[0].actor_id == "system"

    def test_log_change_with_request(self, mock_audit_adapter):
        adapter, entries = mock_audit_adapter
        logger = AuditLogger(enable_console_log=False)

        request = MagicMock()
        request.META = {
            "REMOTE_ADDR": "192.168.1.100",
            "HTTP_USER_AGENT": "Mozilla/5.0",
        }

        event = ConfigChangeEvent(
            config_type="TEST",
            config_key="key",
            action=ConfigAuditAction.UPDATE,
            old_value={"v": 1},
            new_value={"v": 2},
        )

        assert logger.log_change(event, request=request) is True
        details = entries[0].details
        assert "192.168" in details["ip_address"]
        assert "***" in details["ip_address"]  # masked
        assert "Mozilla" in details["user_agent"]

    def test_ip_masking(self, mock_audit_adapter):
        adapter, entries = mock_audit_adapter
        logger = AuditLogger(mask_ip_addresses=True, enable_console_log=False)

        event = ConfigChangeEvent(
            config_type="TEST",
            config_key="key",
            action=ConfigAuditAction.UPDATE,
            ip_address="10.20.30.40",
        )
        logger.log_change(event)

        assert entries[0].details["ip_address"] == "10.20.***.***"

    def test_sensitive_field_masking(self, mock_audit_adapter):
        adapter, entries = mock_audit_adapter
        logger = AuditLogger(
            sensitive_fields=["password", "api_key"],
            enable_console_log=False,
        )

        event = ConfigChangeEvent(
            config_type="AUTH_CONFIG",
            config_key="settings",
            action=ConfigAuditAction.UPDATE,
            old_value={"username": "admin", "password": "old_secret"},
            new_value={"username": "admin", "password": "new_secret"},
        )
        logger.log_change(event)

        details = entries[0].details
        assert details["old_value"]["password"] == "***REDACTED***"
        assert details["new_value"]["password"] == "***REDACTED***"
        assert details["old_value"]["username"] == "admin"

    def test_log_config_update_convenience(self, mock_audit_adapter):
        adapter, entries = mock_audit_adapter
        logger = AuditLogger(enable_console_log=False)

        result = logger.log_config_update(
            config_type="RATE_LIMIT",
            config_key="requests_per_minute",
            old_value={"limit": 100},
            new_value={"limit": 200},
            user="admin",
            reason="Scaling up",
        )

        assert result is True
        assert entries[0].reason == "Scaling up"

    def test_log_batch_update(self, mock_audit_adapter):
        adapter, entries = mock_audit_adapter
        logger = AuditLogger(enable_console_log=False)

        changes = [
            {"key": "setting1", "old_value": {"v": 1}, "new_value": {"v": 2}},
            {"key": "setting2", "old_value": {"v": "a"}, "new_value": {"v": "b"}},
            {
                "key": "setting3",
                "old_value": {"v": True},
                "new_value": {"v": False},
            },
        ]

        result = logger.log_batch_update(
            config_type="BULK_CONFIG",
            changes=changes,
            user="admin",
        )

        assert result is True
        assert len(entries) == 3
        # All should share a batch_id
        batch_ids = {e.details["batch_id"] for e in entries}
        assert len(batch_ids) == 1

    def test_log_change_returns_false_on_exception(self, mock_audit_adapter):
        """D17: log_change returns False only when adapter.log raises."""
        adapter, _entries = mock_audit_adapter
        adapter.log.side_effect = RuntimeError("simulated failure")
        logger = AuditLogger(enable_console_log=False)

        event = ConfigChangeEvent(
            config_type="TEST",
            config_key="k",
            action=ConfigAuditAction.UPDATE,
        )
        assert logger.log_change(event) is False


class TestEndToEndWithHashChainAdapter:
    """Round-trip tests using the real HashChainFileAuditLogAdapter (D6)."""

    def test_log_change_writes_h2_dict_schema(self, tmp_path):
        AuditLogger.reset_instance()
        adapter = HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path), enable_hash_chain=False
        )
        try:
            with patch(
                "baldur.factory.ProviderRegistry.get_audit_adapter",
                return_value=adapter,
            ):
                result = log_config_change(
                    config_type="RETRY_CONFIG",
                    config_key="max_retries",
                    old_value={"v": 3},
                    new_value={"v": 5},
                    user="admin",
                )
            assert result is True

            # The on-disk schema is the H2 dict shape (D6 byte-compat).
            files = list(tmp_path.glob("audit_*.jsonl"))
            assert len(files) == 1
            with open(files[0]) as f:
                row = json.loads(f.readline())
            assert row["change"]["config_type"] == "RETRY_CONFIG"
            assert row["change"]["config_key"] == "max_retries"
            assert row["change"]["old_value"] == {"v": 3}
            assert row["actor"]["user"] == "admin"
        finally:
            adapter.close()
            AuditLogger.reset_instance()


class TestGlobalLogger:
    """Tests for global logger functions."""

    def test_get_audit_logger_singleton(self):
        AuditLogger.reset_instance()
        try:
            logger1 = get_audit_logger()
            logger2 = get_audit_logger()
            assert logger1 is logger2
        finally:
            AuditLogger.reset_instance()

    def test_log_config_change_function(self, mock_audit_adapter):
        adapter, entries = mock_audit_adapter
        result = log_config_change(
            config_type="GLOBAL_TEST",
            config_key="setting",
            old_value={"v": 1},
            new_value={"v": 2},
        )
        assert result is True
        assert entries[0].target_type == "GLOBAL_TEST"


class TestConfigAuditAction:
    """Tests for ConfigAuditAction enum."""

    def test_action_values(self):
        assert ConfigAuditAction.CREATE.value == "create"
        assert ConfigAuditAction.UPDATE.value == "update"
        assert ConfigAuditAction.DELETE.value == "delete"
        assert ConfigAuditAction.READ.value == "read"
        assert ConfigAuditAction.APPLY.value == "apply"
        assert ConfigAuditAction.ROLLBACK.value == "rollback"
