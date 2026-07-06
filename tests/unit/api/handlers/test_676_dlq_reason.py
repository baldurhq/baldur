"""676 — optional ``reason`` on the DLQ replay/retry handlers (D7).

Target: ``baldur.api.handlers.dlq`` — ``_parse_reason`` + ``dlq_replay`` +
``dlq_retry``.

``POST /dlq/replay`` and ``POST /dlq/{pk}/retry`` accept an optional
``reason`` (<=500 chars, same shape as ``dlq_force_redrive`` but optional —
replay is the intended non-destructive path). A malformed/oversized reason
is a 400 before any state change; an absent reason keeps the pre-676
behavior. The reason is forwarded to the service call.

The DLQ service is stubbed via ``_get_service`` — no ``baldur_pro`` import
(G19/G20/G21 safe, PRO-absent safe).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from baldur.api.handlers.dlq import _parse_reason, dlq_replay, dlq_retry
from baldur.interfaces.web_framework import HttpMethod, RequestContext

_GET_SERVICE = "baldur.api.handlers.dlq._get_service"


def _ctx(json_body=None, path_params=None):
    return RequestContext(
        method=HttpMethod("POST"),
        path="/dlq/replay/",
        query_params={},
        path_params=path_params or {},
        json_body=json_body,
        user=None,
    )


def _replay_service() -> MagicMock:
    service = MagicMock()
    service.replay.return_value = SimpleNamespace(
        processed=1, success=1, failed=0, skipped=0
    )
    return service


def _retry_service() -> MagicMock:
    service = MagicMock()
    service.retry_entry.return_value = {
        "success": True,
        "id": "7",
        "retry_count": 1,
        "previous_retry_count": 0,
        "status": "resolved",
        "message": "ok",
    }
    return service


# =============================================================================
# _parse_reason — boundary + type contract
# =============================================================================


class TestParseReasonContract:
    """The optional-reason validator: <=500 chars, string, or absent."""

    def test_absent_reason_is_accepted_as_none(self):
        assert _parse_reason({}) == (None, None)

    def test_reason_at_500_chars_is_accepted(self):
        reason = "x" * 500
        parsed, error = _parse_reason({"reason": reason})
        assert error is None
        assert parsed == reason

    def test_reason_over_500_chars_is_rejected_400(self):
        parsed, error = _parse_reason({"reason": "x" * 501})
        assert parsed is None
        assert error is not None
        assert error.status_code == 400

    def test_non_string_reason_is_rejected_400(self):
        parsed, error = _parse_reason({"reason": 123})
        assert parsed is None
        assert error is not None
        assert error.status_code == 400


# =============================================================================
# dlq_replay — reason validation + forwarding
# =============================================================================


class TestDlqReplayReasonContract:
    def test_reason_at_boundary_passes_and_is_forwarded(self):
        service = _replay_service()
        reason = "x" * 500
        with patch(_GET_SERVICE, return_value=service):
            resp = dlq_replay(_ctx(json_body={"reason": reason}))

        assert resp.status_code == 200
        assert service.replay.call_args.kwargs["reason"] == reason

    def test_reason_over_boundary_returns_400_before_service(self):
        service = _replay_service()
        with patch(_GET_SERVICE, return_value=service):
            resp = dlq_replay(_ctx(json_body={"reason": "x" * 501}))

        assert resp.status_code == 400
        service.replay.assert_not_called()

    def test_absent_reason_keeps_current_behavior_and_forwards_none(self):
        service = _replay_service()
        with patch(_GET_SERVICE, return_value=service):
            resp = dlq_replay(_ctx(json_body={"batch_size": 10}))

        assert resp.status_code == 200
        assert service.replay.call_args.kwargs["reason"] is None


# =============================================================================
# dlq_retry — reason validation + forwarding
# =============================================================================


class TestDlqRetryReasonContract:
    def test_reason_at_boundary_passes_and_is_forwarded(self):
        service = _retry_service()
        reason = "x" * 500
        with patch(_GET_SERVICE, return_value=service):
            resp = dlq_retry(
                _ctx(json_body={"reason": reason}, path_params={"pk": "7"})
            )

        assert resp.status_code == 200
        service.retry_entry.assert_called_once_with("7", reason=reason)

    def test_reason_over_boundary_returns_400_before_service(self):
        service = _retry_service()
        with patch(_GET_SERVICE, return_value=service):
            resp = dlq_retry(
                _ctx(json_body={"reason": "x" * 501}, path_params={"pk": "7"})
            )

        assert resp.status_code == 400
        service.retry_entry.assert_not_called()

    def test_absent_reason_keeps_current_behavior_and_forwards_none(self):
        service = _retry_service()
        with patch(_GET_SERVICE, return_value=service):
            resp = dlq_retry(_ctx(json_body={}, path_params={"pk": "7"}))

        assert resp.status_code == 200
        service.retry_entry.assert_called_once_with("7", reason=None)
