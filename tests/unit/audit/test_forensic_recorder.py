"""Unit tests for baldur.audit.forensic_recorder.

Verifies that record_forensic_capture() routes captured forensic context
to the canonical AuditLogAdapter via AuditEntry, masks sensitive fields,
respects the audit_enabled flag, and remains fail-open on adapter
failures.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from baldur.audit.forensic_recorder import record_forensic_capture
from baldur.interfaces.audit_adapter import AuditAction, AuditEntry, AuditLogAdapter


class _FakeAdapter(AuditLogAdapter):
    """In-memory audit adapter capturing logged entries for assertions."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def log(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    def query(self, **_: Any) -> list[AuditEntry]:
        return list(self.entries)


class _RaisingAdapter(AuditLogAdapter):
    """Adapter whose log() always raises — used for fail-open verification."""

    def log(self, entry: AuditEntry) -> None:
        raise RuntimeError("audit backend unavailable")

    def query(self, **_: Any) -> list[AuditEntry]:
        return []


@pytest.fixture(autouse=True)
def _reset_forensic_settings():
    from baldur.settings.forensic import reset_forensic_settings

    reset_forensic_settings()
    yield
    reset_forensic_settings()


class TestRecordForensicCapture:
    def test_emits_audit_entry_with_correct_action_and_target(self) -> None:
        adapter = _FakeAdapter()
        try:
            raise ValueError("boom")
        except ValueError as exc:
            ok = record_forensic_capture(
                exception=exc,
                stack_trace="line1\nline2\nline3",
                context={"task_id": "abc", "order": {"id": 42}},
                target_type="celery_task",
                target_id="abc",
                audit_adapter=adapter,
            )

        assert ok is True
        assert len(adapter.entries) == 1
        entry = adapter.entries[0]
        assert entry.action == AuditAction.FORENSIC_CAPTURE_COMPLETED
        assert entry.target_type == "celery_task"
        assert entry.target_id == "abc"
        assert entry.success is False
        assert entry.error_message == "boom"
        assert entry.details["exception_type"] == "ValueError"
        assert entry.details["stack_depth"] == 3
        assert entry.details["context"]["task_id"] == "abc"
        assert entry.details["context"]["order"] == {"id": 42}

    def test_masks_sensitive_fields(self) -> None:
        adapter = _FakeAdapter()
        context = {
            "user": {"id": 7, "password": "hunter2"},
            "card_number": "4111111111111111",
            "safe_field": "ok",
        }
        try:
            raise RuntimeError("x")
        except RuntimeError as exc:
            record_forensic_capture(
                exception=exc,
                stack_trace="",
                context=context,
                target_type="celery_task",
                target_id="t1",
                audit_adapter=adapter,
            )

        masked = adapter.entries[0].details["context"]
        assert masked["user"]["password"] == "***REDACTED***"
        assert masked["card_number"] == "***REDACTED***"
        assert masked["safe_field"] == "ok"
        assert masked["user"]["id"] == 7

    def test_audit_disabled_short_circuits(self) -> None:
        # 686 D1/D5: the recorder now reads forensic settings through the layered
        # provider so a console edit takes effect. Assert the disabled short-circuit
        # via the layered seam (a disabled ForensicSettings), which is robust
        # whether or not a RuntimeConfigManager overlay is present — unlike a bare
        # env var, which a stale manager default blob can pin over (R1).
        adapter = _FakeAdapter()
        from baldur.settings.forensic import ForensicSettings

        disabled = ForensicSettings(audit_enabled=False)
        with mock.patch(
            "baldur.settings.layered_provider.get_layered_settings",
            autospec=True,
            return_value=disabled,
        ):
            try:
                raise ValueError("x")
            except ValueError as exc:
                ok = record_forensic_capture(
                    exception=exc,
                    stack_trace="",
                    context={"k": "v"},
                    target_type="celery_task",
                    target_id="t1",
                    audit_adapter=adapter,
                )

        assert ok is False
        assert adapter.entries == []

    def test_fail_open_when_adapter_raises(self) -> None:
        # D8: a raising adapter fails open (no propagation) AND increments
        # audit_emit_dropped_total{site="forensic_recorder"} so the drop is
        # observable in production, not only in tests.
        from baldur.metrics.audit_emit_metrics import (
            METRICS_AVAILABLE,
            audit_emit_dropped_total,
        )

        before = (
            audit_emit_dropped_total.labels(site="forensic_recorder")._value.get()
            if METRICS_AVAILABLE
            else None
        )

        adapter = _RaisingAdapter()
        try:
            raise ValueError("x")
        except ValueError as exc:
            ok = record_forensic_capture(
                exception=exc,
                stack_trace="line1",
                context={"k": "v"},
                target_type="celery_task",
                target_id="t1",
                audit_adapter=adapter,
            )

        assert ok is False  # write failed but did not propagate

        if METRICS_AVAILABLE:
            after = audit_emit_dropped_total.labels(
                site="forensic_recorder"
            )._value.get()
            assert after - before == 1.0

    def test_none_context_yields_empty_masked_context(self) -> None:
        adapter = _FakeAdapter()
        try:
            raise RuntimeError("x")
        except RuntimeError as exc:
            ok = record_forensic_capture(
                exception=exc,
                stack_trace="",
                context=None,
                target_type="celery_task",
                target_id="t1",
                audit_adapter=adapter,
            )

        assert ok is True
        assert adapter.entries[0].details["context"] == {}

    def test_truncates_long_error_message(self) -> None:
        adapter = _FakeAdapter()
        long_msg = "x" * 10_000
        try:
            raise ValueError(long_msg)
        except ValueError as exc:
            record_forensic_capture(
                exception=exc,
                stack_trace="",
                context={},
                target_type="celery_task",
                target_id="t1",
                audit_adapter=adapter,
            )

        from baldur.settings.forensic import get_forensic_settings

        max_len = get_forensic_settings().error_message_max_length
        assert len(adapter.entries[0].error_message) == max_len
