"""Unit tests for 617 D3/D4 — _emit_replay_blocked consolidation + #496 audit.

D3 consolidated the 4-channel replay-block surface (structlog log +
``DLQ_REPLAY_BLOCKED`` event + ``on_replay_blocked`` metric + optional audit)
that was copy-pasted across 7 block sites into ``_emit_replay_blocked``.
``TestReplayBlockedChannels`` verifies the helper dispatches all four channels
per the per-site contract (event name, level, payload, metric args, audit
on/off) for the full 7-site matrix.

D4 (#496) added the previously-missing audit channel to the
``max_replay_attempts_exceeded`` branch. ``TestReplayMaxAttemptsAudit`` drives
that branch end-to-end and asserts the audit record carries the documented
``reason`` / ``trigger`` / ``details.dlq_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.replay_service import ReplayService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def service() -> ReplayService:
    """ReplayService with a mock repository and a mock event bus."""
    svc = ReplayService(repository=MagicMock())
    svc._event_bus = MagicMock()
    return svc


@dataclass
class _FakeFailedOp:
    """Minimal FailedOperationData stand-in for the max-attempts branch."""

    id: int
    domain: str = "payment"
    status: str = "pending"


# =============================================================================
# D3 — _emit_replay_blocked 7-site channel matrix
# =============================================================================

# (site_id, log_event, log_level, audit_present) — one row per production
# block site, capturing each site's per-site channel fidelity.
_BLOCK_SITES = [
    ("max_attempts", "replay_service.replay_max_attempts_exceeded", "warning", True),
    ("truncate_gate", "dlq.replay_blocked_truncated", "debug", False),
    ("replay_single_governance", "replay_service.blocked", "warning", False),
    ("replay_batch_governance", "replay_service.blocked", "warning", False),
    (
        "circuit_close_inflight",
        "replay_service.circuit_close_inflight_skipped",
        "warning",
        True,
    ),
    (
        "no_failure_types_mapped",
        "replay_service.no_failure_types_mapped",
        "warning",
        True,
    ),
    ("circuit_close_governance", "replay_service.blocked", "warning", False),
]


class TestReplayBlockedChannels:
    """``_emit_replay_blocked`` dispatches all 4 channels per per-site contract."""

    @pytest.mark.parametrize(
        ("site_id", "log_event", "log_level", "audit_present"),
        _BLOCK_SITES,
        ids=[row[0] for row in _BLOCK_SITES],
    )
    def test_emit_replay_blocked_dispatches_each_channel(
        self, service, site_id, log_event, log_level, audit_present
    ):
        """One Act → log (at level) + event + metric + (conditional) audit."""
        # Given — representative per-site payloads
        event_data = {"block_reason": site_id, "marker": site_id}
        metric_subject = "payment"
        metric_reason = site_id
        audit = (
            {
                "domain": "dlq",
                "reason": site_id,
                "service_name": "ReplayService",
                "trigger": "single",
                "details": {"dlq_id": 1},
            }
            if audit_present
            else None
        )

        # When
        with (
            patch(
                "baldur.metrics.event_handlers.ReplayEventHandler.on_replay_blocked",
                autospec=True,
            ) as mock_metric,
            patch(
                "baldur.services.replay_service.service.log_dlq_replay_blocked_audit",
                autospec=True,
            ) as mock_audit,
        ):
            with capture_logs() as logs:
                service._emit_replay_blocked(
                    log_event=log_event,
                    log_fields={"marker": site_id},
                    event_data=event_data,
                    metric_subject=metric_subject,
                    metric_reason=metric_reason,
                    log_level=log_level,
                    audit=audit,
                )

        # Then — channel 1: structlog log at the site's level with its event name
        matching = [e for e in logs if e.get("event") == log_event]
        assert len(matching) == 1
        assert matching[0]["log_level"] == log_level
        assert matching[0]["marker"] == site_id

        # channel 2: DLQ_REPLAY_BLOCKED event with the verbatim payload
        service._event_bus.emit.assert_called_once()
        emit_call = service._event_bus.emit.call_args
        assert emit_call[0][0] == EventType.DLQ_REPLAY_BLOCKED
        assert emit_call.kwargs["data"] == event_data

        # channel 3: metric called once with (subject, reason)
        mock_metric.assert_called_once_with(metric_subject, metric_reason)

        # channel 4: audit fires iff the site supplies an explicit audit dict
        if audit_present:
            mock_audit.assert_called_once_with(**audit)
        else:
            mock_audit.assert_not_called()


# =============================================================================
# D4 / #496 — max_replay_attempts_exceeded audit channel
# =============================================================================


class TestReplayMaxAttemptsAudit:
    """The max-attempts block records a blocked-family audit entry (#496)."""

    @pytest.fixture
    def max_attempts_service(self) -> ReplayService:
        """ReplayService whose repository forces the max-attempts branch.

        ``try_acquire_for_replay`` returns None (acquisition refused) while a
        pending entry still exists — the config-lowered / race path that emits
        the max-attempts block.
        """
        repo = MagicMock()
        repo.try_acquire_for_replay.return_value = None
        repo.get_by_id.return_value = _FakeFailedOp(id=42, domain="payment")
        svc = ReplayService(repository=repo)
        svc._event_bus = MagicMock()
        return svc

    def test_max_attempts_block_records_audit_with_reason_and_dlq_id(
        self, max_attempts_service
    ):
        """Audit fires with reason=max_replay_attempts_exceeded + dlq_id in details."""
        with patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit",
            autospec=True,
        ) as mock_audit:
            result = max_attempts_service._execute_replay(42, replay_type="single")

        assert result.success is False

        mock_audit.assert_called_once()
        kwargs = mock_audit.call_args.kwargs
        assert kwargs["reason"] == "max_replay_attempts_exceeded"
        assert kwargs["service_name"] == "ReplayService"
        assert kwargs["details"] == {"dlq_id": 42}

    def test_max_attempts_block_audit_trigger_matches_replay_type(
        self, max_attempts_service
    ):
        """The audit ``trigger`` mirrors the replay_type ('single' | 'batch')."""
        with patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit",
            autospec=True,
        ) as mock_audit:
            max_attempts_service._execute_replay(42, replay_type="batch")

        assert mock_audit.call_args.kwargs["trigger"] == "batch"

    def test_max_attempts_block_audit_domain_mirrors_metric_subject(
        self, max_attempts_service
    ):
        """The audit ``domain`` mirrors the existing entry's domain."""
        with patch(
            "baldur.services.replay_service.service.log_dlq_replay_blocked_audit",
            autospec=True,
        ) as mock_audit:
            max_attempts_service._execute_replay(42)

        assert mock_audit.call_args.kwargs["domain"] == "payment"
