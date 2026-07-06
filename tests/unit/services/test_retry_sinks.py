"""
DLQSink(Dead Letter Queue Sink) 단위 테스트.

테스트 대상: services/retry_handler/sinks.py
- DLQSink: should_dlq 플래그 기반 DLQ 저장, Fail-Open
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)
from baldur.services.retry_handler.sinks import DLQSink

# 518 batch (a): the sticky-flag baldur_pro resolver (``#485 D1b/G4``) and its
# ``_reset_baldur_pro_dlq_resolver`` cache reset were removed once
# ``baldur.dlq.helpers.store_to_dlq`` took over the fail-open contract. Patches
# now target the helper-binding location on the sink module directly, so no
# per-test resolver reset is needed.


# =============================================================================
# DLQSink — 계약 검증
# =============================================================================


class TestDLQSinkContract:
    """DLQSink 구조 및 기본값 검증."""

    def test_has_handle_failure_method(self):
        """DLQSink는 handle_failure 메서드를 가진다."""
        assert hasattr(DLQSink(), "handle_failure")


# =============================================================================
# DLQSink — 동작 검증
# =============================================================================


class TestDLQSinkBehavior:
    """DLQSink 동작 검증. should_dlq 플래그 및 Fail-Open 원칙."""

    def _make_result(self, should_dlq: bool = True) -> PolicyResult:
        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            total_attempts=3,
            metadata={
                "should_dlq": should_dlq,
                "domain": "test",
                "retry_history": [],
            },
        )

    def _make_context(self) -> PolicyContext:
        return PolicyContext(
            domain="test",
            tier_id="tier-1",
            region="kr",
        )

    def test_skips_when_should_dlq_false(self):
        """should_dlq=False이면 _store_to_dlq를 호출하지 않는다."""
        sink = DLQSink()
        result = self._make_result(should_dlq=False)
        ret = sink.handle_failure(Exception("err"), self._make_context(), result)
        assert ret is None

    def test_skips_when_should_dlq_key_missing(self):
        """should_dlq 키가 없으면 _store_to_dlq를 호출하지 않는다."""
        sink = DLQSink()
        result = PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            total_attempts=3,
            metadata={"domain": "test"},
        )
        ret = sink.handle_failure(Exception("err"), self._make_context(), result)
        assert ret is None

    @patch("baldur.services.retry_handler.sinks.store_to_dlq")
    def test_stores_when_should_dlq_true(self, mock_store):
        """should_dlq=True이면 store_to_dlq를 호출한다."""
        mock_store.return_value = MagicMock(success=True, dlq_id="dlq-123")
        sink = DLQSink()
        result = self._make_result(should_dlq=True)
        ctx = self._make_context()
        err = ValueError("fail")

        ret = sink.handle_failure(err, ctx, result)
        mock_store.assert_called_once()
        assert ret == "dlq-123"

    def test_handles_store_failure_gracefully(self):
        """store_to_dlq 호출 실패 시 예외가 전파되지 않는다 (Fail-Open)."""
        sink = DLQSink()
        result = self._make_result(should_dlq=True)
        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            side_effect=RuntimeError("DLQ down"),
        ):
            ret = sink.handle_failure(Exception("err"), self._make_context(), result)
            assert ret is None

    def test_handles_import_error_gracefully(self):
        """store_to_dlq import 실패 시 예외가 전파되지 않는다 (Fail-Open)."""
        sink = DLQSink()
        result = self._make_result(should_dlq=True)
        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            side_effect=ImportError("no module"),
        ):
            ret = sink.handle_failure(Exception("err"), self._make_context(), result)
            assert ret is None

    def test_context_none_is_safe(self):
        """context=None이어도 에러 없이 동작한다."""
        sink = DLQSink()
        result = self._make_result(should_dlq=True)
        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            return_value=MagicMock(success=True, dlq_id="dlq-456"),
        ):
            ret = sink.handle_failure(Exception("err"), None, result)
            assert ret == "dlq-456"


# =============================================================================
# DLQSink — Skip vs Error 구분 가능성 (Cat 1.9, 시나리오 plan §328)
# =============================================================================
#
# 검증 기준 (plan §328 row 1.9): "DLQ sink distinguishes 'not stored (skip)'
# from 'store failed (error)'." 반환값(str | None)만으로는 세 종착지(skip /
# stored / failed / exception)가 구분되지 않으므로 — Protocol 반환 타입을
# 바꾸는 광범위한 변경 없이는 caller-side 구분이 불가하다 — 현재 구현이
# 이미 제공하는 *로그 레벨* 가시성을 회귀 게이트로 고정한다:
#
#   - skip:      `dlq_sink.create_dlq_entry_failed` 가 emit 되지 않는다
#                (silent — store_to_dlq 자체가 호출되지 않음)
#   - stored:    `dlq_sink.created_dlq_entry` (info) emit
#   - failed:    `dlq_sink.create_dlq_entry_failed` (error) emit, kwarg=result
#   - exception: `dlq_sink.create_dlq_entry_failed` (error) emit, kwarg=dlq_error
#
# Protocol-level distinguishability(반환 타입 변경)는 이 테스트의 범위를
# 벗어남 — composer.py 호출부 + ThrottleDLQSink 가 아닌 FailureSink 구현체
# 추가 시 확장 검토 (out-of-scope follow-up).


class TestDLQSinkLogDistinguishability:
    """DLQSink가 skip / failure 경로를 로그 가시성으로 구분함을 검증."""

    def _make_result(self, should_dlq: bool = True) -> PolicyResult:
        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            total_attempts=3,
            metadata={
                "should_dlq": should_dlq,
                "domain": "test",
                "retry_history": [],
            },
        )

    def _make_context(self) -> PolicyContext:
        return PolicyContext(domain="test", tier_id="tier-1", region="kr")

    def test_skip_path_emits_no_failed_log(self):
        """should_dlq=False — silent path, no `*_failed` log."""
        sink = DLQSink()
        result = self._make_result(should_dlq=False)

        with capture_logs() as logs:
            sink.handle_failure(Exception("err"), self._make_context(), result)

        failed_events = [
            e for e in logs if e.get("event") == "dlq_sink.create_dlq_entry_failed"
        ]
        created_events = [
            e for e in logs if e.get("event") == "dlq_sink.created_dlq_entry"
        ]
        assert failed_events == []
        assert created_events == []

    def test_store_failure_emits_failed_log_at_error_level(self):
        """store_to_dlq returns success=False — failure path observable via ERROR log."""
        sink = DLQSink()
        result = self._make_result(should_dlq=True)

        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            return_value=MagicMock(success=False, dlq_id=None, error="redis_down"),
        ):
            with capture_logs() as logs:
                sink.handle_failure(Exception("err"), self._make_context(), result)

        failed_events = [
            e for e in logs if e.get("event") == "dlq_sink.create_dlq_entry_failed"
        ]
        assert len(failed_events) == 1
        evt = failed_events[0]
        assert evt["log_level"] == "error"
        # The failure-result branch carries the upstream error string in
        # ``result`` (not ``dlq_error``) — that is the discriminator from the
        # exception branch below.
        assert "result" in evt
        assert "dlq_error" not in evt

    def test_exception_path_emits_failed_log_at_error_level(self):
        """store_to_dlq raises — exception path observable via ERROR log too,
        but discriminated by the ``dlq_error`` kwarg vs ``result`` kwarg."""
        sink = DLQSink()
        result = self._make_result(should_dlq=True)

        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            side_effect=RuntimeError("crashed"),
        ):
            with capture_logs() as logs:
                sink.handle_failure(Exception("err"), self._make_context(), result)

        failed_events = [
            e for e in logs if e.get("event") == "dlq_sink.create_dlq_entry_failed"
        ]
        assert len(failed_events) == 1
        evt = failed_events[0]
        assert evt["log_level"] == "error"
        # Exception branch uses ``dlq_error`` kwarg — the ``result`` kwarg is
        # the failure-result branch's signature.
        assert "dlq_error" in evt
        assert "result" not in evt


# =============================================================================
# DLQSink — D10 user_id precedence (#504)
# =============================================================================
#
# Per interfaces/resilience_policy.py docstring, ``PolicyContext.user_id`` is
# the documented contract for DLQ user_id column. The sink reads it first;
# ``extra["user_id"]`` remains as a legacy fallback for direct callers who
# populate ``extra`` without setting the named field.


class TestExtractContextFieldsUserIdPrecedenceContract:
    """``_extract_context_fields`` reads ``context.user_id`` first; falls
    back to ``extra["user_id"]`` only when the named field is None (#504 D10)."""

    @pytest.mark.parametrize(
        ("named_user_id", "extra_user_id", "expected"),
        [
            # Named field wins when both are set
            ("7", "99", 7),
            # Named field used when set, no extras
            ("42", None, 42),
            # Fallback to extras when named is None
            (None, "5", 5),
            # Both None → None
            (None, None, None),
        ],
    )
    def test_user_id_precedence_named_wins(
        self, named_user_id, extra_user_id, expected
    ):
        extra: dict[str, object] = {}
        if extra_user_id is not None:
            extra["user_id"] = extra_user_id
        ctx = PolicyContext(user_id=named_user_id, extra=extra)

        fields = DLQSink._extract_context_fields(ctx)

        assert fields["user_id"] == expected

    def test_none_context_returns_user_id_none(self):
        """``context=None`` is the empty-context path — no user_id either way."""
        fields = DLQSink._extract_context_fields(None)
        assert fields["user_id"] is None

    def test_entity_id_reads_order_id_from_named_field(self):
        """Companion: ``entity_id`` comes from ``context.order_id`` (sinks.py)."""
        ctx = PolicyContext(order_id="o-42")
        fields = DLQSink._extract_context_fields(ctx)
        assert fields["entity_id"] == "o-42"

    def test_request_data_reads_extras_dict(self):
        """``request_data`` is read from ``extra["request_data"]`` so the
        decorator-path auto-extract (#504 D5) and direct callers share the
        same surface."""
        ctx = PolicyContext(extra={"request_data": {"order_id": "o-1", "amount": 100}})
        fields = DLQSink._extract_context_fields(ctx)
        assert fields["request_data"] == {"order_id": "o-1", "amount": 100}
