"""
Postmortem Audit Integration Tests.

Audit logging for the automatic post-mortem trigger.
(The manual-API tests need Django and live in the top-level tests tree.)

Verified:
1. The automatic trigger's WAL write
2. The recorded audit event types
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# Every test here exercises a baldur_pro audit path; skip when PRO is absent
# (the published OSS mirror installs only baldur).
pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


@contextmanager
def _stub_audit_wal(helper_module_name, sequence=1):
    """Run a PRO audit helper against a stub WAL, yielding the stub.

    ``sequence`` is what the WAL hands back for a write; ``None`` stands for
    "no WAL is available", the case where the helper's immediate delivery leg
    runs. Assert on ``wal.write.call_args[0][0]`` to read the entry the writer
    built.

    Two deliberate choices. The WAL is stubbed rather than the writer, so this
    file never names a ``baldur_pro`` internal — it ships to the public repo.
    And the writer's own globals are patched rather than a dotted path,
    because an autouse fixture drops the audit modules from ``sys.modules``
    between tests: a helper can hold a writer from an earlier module object
    that a path-based patch would miss. The import is deferred to entry so
    the module is only required once a test has skipped on PRO's absence.
    """
    import importlib

    from baldur.audit.wal import WriteAheadLog

    module = importlib.import_module(f"baldur_pro.services.audit.{helper_module_name}")
    wal = MagicMock(spec=WriteAheadLog)
    wal.write.return_value = sequence
    resolver = (lambda: None) if sequence is None else (lambda: wal)
    with patch.dict(module._write_to_wal.__globals__, {"_get_wal": resolver}):
        yield wal


class TestAutoPostmortemAudit:
    """Audit logging for the automatic post-mortem trigger."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.api_view import reset_api_view_settings

        reset_api_view_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.api_view import reset_api_view_settings

        reset_api_view_settings()

    def test_auto_postmortem_no_audit_when_disabled(self, monkeypatch):
        """With the automatic post-mortem disabled, no audit is recorded."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_circuit_breaker_closed_postmortem,
        )

        monkeypatch.setenv("BALDUR_API_VIEW_XTEST_AUTO_POSTMORTEM_ENABLED", "false")

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={"service_name": "test_service"},
            source="test",
        )

        with _stub_audit_wal("xtest_audit") as wal:
            _on_circuit_breaker_closed_postmortem(event)

        # Disabled means the event never reaches the WAL at all.
        postmortem_calls = [
            call.args[0]
            for call in wal.write.call_args_list
            if call.args[0].get("event_type") == "POSTMORTEM_AUTO_GENERATED"
        ]
        assert len(postmortem_calls) == 0


class TestAuditEventTypes:
    """Audit event type tests."""

    def test_xtest_audit_uses_xtest_operation_event_type(self):
        """The manual API records the XTEST_OPERATION event type."""
        from baldur_pro.services.audit.xtest_audit import log_xtest_operation_audit

        with _stub_audit_wal("xtest_audit") as wal:
            log_xtest_operation_audit(
                session_id="test-session",
                action="generate_postmortem",
                component="observability",
                details={"incident_id": "TEST-001"},
                result="success",
                user="test_user",
            )

        wal.write.assert_called_once()
        entry = wal.write.call_args[0][0]
        assert entry["event_type"] == "XTEST_OPERATION"
        assert entry["source"] == "XTest.observability"
