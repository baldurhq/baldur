"""679 D4/D5 — OSS ``ReplayService._execute_replay`` origin-trace linkage.

Target: ``baldur.services.replay_service.service.ReplayService._execute_replay``.

The origin trace captured at DLQ store time (``entry.metadata['origin_trace_id']``
plus the OTEL-only full ids) is surfaced ADDITIVELY on the OSS chokepoint's four
channels — the per-entry replay log field, the ``log_dlq_replay_audit`` details,
the ``DLQ_REPLAY_COMPLETED`` / ``DLQ_REPLAY_FAILED`` event ``data``, and a
``dlq.replay`` OTEL span linked to the origin SpanContext — WITHOUT clobbering
the replay's own ambient trigger trace. Missing-origin entries (pre-679, no-trace
capture, non-dict / marker-without-keys metadata, or an entry with no metadata
attribute) skip linkage silently.

This file is OSS-clean by construction: it drives ``_execute_replay`` directly
(the governance-bearing ``replay_single`` / ``replay_batch`` entry points are not
touched), so it imports no ``baldur_pro`` symbol and stays in ``tests/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.audit.trace import clear_trace_id, get_trace_id, set_trace_id
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.replay_service import (
    ReplayResult,
    ReplayService,
    _replay_handlers,
)
from baldur.services.replay_service.handlers import ReplayHandler

_AUDIT = "baldur.services.replay_service.service.log_dlq_replay_audit"
_SPAN = "baldur.observability.span_with_link"

_FULL_TRACE_ID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
_SPAN_ID = "1234567890abcdef"
_DISPLAY_ID = f"req-{_FULL_TRACE_ID[:8]}"

_ORIGIN_META = {
    "origin_trace_id": _DISPLAY_ID,
    "origin_trace_id_full": _FULL_TRACE_ID,
    "origin_span_id": _SPAN_ID,
}


@dataclass
class FakeFailedOperationData:
    """Test substitute for FailedOperationData carrying a metadata slot."""

    id: int = 1
    domain: str = "payment"
    status: str = "pending"
    failure_type: str = "PG_TIMEOUT"
    retry_count: int = 1
    error_code: str = ""
    error_message: str = ""
    snapshot_data: dict = None
    request_data: dict = None
    response_data: dict = None
    metadata: dict = None

    def __post_init__(self):
        self.snapshot_data = self.snapshot_data or {}
        self.request_data = self.request_data or {}
        self.response_data = self.response_data or {}


class SuccessHandler(ReplayHandler):
    @property
    def domain(self) -> str:
        return "payment"

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        return ReplayResult.succeeded(failed_op.id, "OK")


class CrashHandler(ReplayHandler):
    @property
    def domain(self) -> str:
        return "payment"

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        raise RuntimeError("Connection lost")


@pytest.fixture(autouse=True)
def _clear_handler_registry():
    _replay_handlers.clear()
    clear_trace_id()
    yield
    _replay_handlers.clear()
    clear_trace_id()


@pytest.fixture
def mock_event_bus():
    return MagicMock()


def _service(entry, event_bus):
    repo = MagicMock()
    repo.try_acquire_for_replay.return_value = entry
    repo.get_by_id.return_value = entry
    repo.complete_replay.return_value = None
    svc = ReplayService(repository=repo)
    svc._event_bus = event_bus
    return svc


def _events(bus, event_type):
    return [c[1]["data"] for c in bus.emit.call_args_list if c[0][0] == event_type]


def _log(captured, event_name):
    return next((e for e in captured if e["event"] == event_name), None)


class TestReplayOriginTraceLinkageBehavior:
    """Additive origin linkage across the four OSS channels + no-clobber."""

    def test_success_surfaces_origin_on_log_audit_event_and_span(self, mock_event_bus):
        """A replayed traced entry references the origin on ALL success channels."""
        _replay_handlers["payment"] = SuccessHandler()
        svc = _service(
            FakeFailedOperationData(metadata=dict(_ORIGIN_META)), mock_event_bus
        )

        with patch(_AUDIT) as audit, patch(_SPAN) as span, capture_logs() as logs:
            result = svc._execute_replay(1)

        assert result.success is True

        # 1) Log field on the success line.
        entry = _log(logs, "replay_service.dlq_entry_replayed_successfully")
        assert entry is not None
        assert entry["origin_trace_id"] == _DISPLAY_ID

        # 2) Audit details param.
        assert audit.call_args.kwargs["origin_trace_id"] == _DISPLAY_ID

        # 3) Event data.
        completed = _events(mock_event_bus, EventType.DLQ_REPLAY_COMPLETED)
        assert len(completed) == 1
        assert completed[0]["origin_trace_id"] == _DISPLAY_ID

        # 4) OTEL span link (full W3C ids + searchable attribute).
        assert span.call_args.args == ("dlq.replay", _FULL_TRACE_ID, _SPAN_ID)
        attrs = span.call_args.kwargs["attributes"]
        assert attrs["baldur.dlq.id"] == "1"
        assert attrs["baldur.dlq.origin_trace_id"] == _DISPLAY_ID

    def test_handler_crash_surfaces_origin_on_log_event_and_span(self, mock_event_bus):
        """A crashing replay still references the origin on its failure channels."""
        _replay_handlers["payment"] = CrashHandler()
        svc = _service(
            FakeFailedOperationData(metadata=dict(_ORIGIN_META)), mock_event_bus
        )

        with patch(_AUDIT), patch(_SPAN) as span, capture_logs() as logs:
            result = svc._execute_replay(1)

        assert result.success is False

        entry = _log(logs, "replay_service.handler_exception_dlq")
        assert entry is not None
        assert entry["origin_trace_id"] == _DISPLAY_ID

        failed = _events(mock_event_bus, EventType.DLQ_REPLAY_FAILED)
        assert len(failed) == 1
        assert failed[0]["origin_trace_id"] == _DISPLAY_ID

        # The span link is built around the (crashing) handler execution too.
        assert span.call_args.args == ("dlq.replay", _FULL_TRACE_ID, _SPAN_ID)

    def test_ambient_trigger_trace_is_not_clobbered_by_origin(self, mock_event_bus):
        """Linkage is additive: the replay keeps its own ambient trigger trace."""
        _replay_handlers["payment"] = SuccessHandler()
        svc = _service(
            FakeFailedOperationData(metadata=dict(_ORIGIN_META)), mock_event_bus
        )

        set_trace_id("req-trigger")
        with (
            patch("baldur.audit.trace._get_trace_id_from_otel", return_value=None),
            patch(_AUDIT),
            patch(_SPAN),
        ):
            svc._execute_replay(1)
            # Ambient trace still the trigger, not the origin.
            assert get_trace_id() == "req-trigger"

    @pytest.mark.parametrize(
        "metadata",
        [
            None,
            {},
            {"_truncated": True, "original_size": 5000, "preview": "..."},
            "not-a-dict",
        ],
        ids=["none", "empty", "marker-without-keys", "non-dict"],
    )
    def test_missing_origin_replays_normally_with_linkage_skipped(
        self, metadata, mock_event_bus
    ):
        """Entries without origin keys replay normally: no origin on any channel,
        audit gets ``origin_trace_id=None``, span gets None ids."""
        _replay_handlers["payment"] = SuccessHandler()
        svc = _service(FakeFailedOperationData(metadata=metadata), mock_event_bus)

        with patch(_AUDIT) as audit, patch(_SPAN) as span, capture_logs() as logs:
            result = svc._execute_replay(1)

        assert result.success is True

        entry = _log(logs, "replay_service.dlq_entry_replayed_successfully")
        assert entry is not None
        assert "origin_trace_id" not in entry

        assert audit.call_args.kwargs["origin_trace_id"] is None

        completed = _events(mock_event_bus, EventType.DLQ_REPLAY_COMPLETED)
        assert "origin_trace_id" not in completed[0]

        # No full ids -> the span helper receives None (no-op link downstream).
        assert span.call_args.args == ("dlq.replay", None, None)
        assert span.call_args.kwargs["attributes"]["baldur.dlq.origin_trace_id"] == ""

    def test_metadata_less_entry_is_read_defensively_and_skips_linkage(
        self, mock_event_bus
    ):
        """A pre-679 test double with no ``metadata`` attribute must not raise —
        the chokepoint reads it via ``getattr(..., 'metadata', None)`` (D-notes)."""

        class _NoMetadataOp:
            id = 1
            domain = "payment"
            status = "pending"
            failure_type = "PG_TIMEOUT"
            retry_count = 1
            error_code = ""
            error_message = ""
            request_data: dict = {}

        _replay_handlers["payment"] = SuccessHandler()
        svc = _service(_NoMetadataOp(), mock_event_bus)

        with patch(_AUDIT) as audit, patch(_SPAN):
            result = svc._execute_replay(1)

        assert result.success is True
        assert audit.call_args.kwargs["origin_trace_id"] is None
