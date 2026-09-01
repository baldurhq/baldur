"""
DLQSink(Dead Letter Queue Sink) 단위 테스트.

테스트 대상: services/retry_handler/sinks.py
- DLQSink: should_dlq 플래그 기반 DLQ 저장, Fail-Open
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)
from baldur.models.dlq import DLQEntryResult
from baldur.services.bulkhead.exceptions import BulkheadFullError
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.retry_handler.sinks import DLQSink
from tests.factories import dry_run_active

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


# =============================================================================
# DLQSink — open-circuit rejection terminal
# =============================================================================
#
# The rejected call never ran, so there is no retry history and no
# ``should_dlq`` verdict: the store is gated on the capture flag instead, and
# the entry is stored under the rejecting breaker's own name so the
# on-recovery sweep can find it again.


def _rejection_result(
    service_name: str = "payment_api",
    state: str = "open",
    *,
    with_service_name: bool = True,
) -> PolicyResult:
    """A REJECTED terminal carrying only the CB policy's own metadata keys.

    The rejection path never runs retry, so ``domain`` / ``should_dlq`` are
    genuinely absent — reproducing that is the point of building the result
    by hand rather than reusing the FAILURE fixture.
    """
    metadata: dict[str, object] = {"state": state}
    if with_service_name:
        metadata["service_name"] = service_name
    return PolicyResult(
        outcome=PolicyOutcome.REJECTED,
        total_attempts=1,
        metadata=metadata,
        executed_policies=["circuit_breaker"],
    )


def _capture_settings(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(open_circuit_capture_enabled=enabled)


class TestDLQSinkOpenCircuitContract:
    """The stored entry's shape — spec values, hardcoded."""

    def _store_call(self, mock_store) -> dict:
        assert mock_store.call_count == 1
        return mock_store.call_args.kwargs

    def test_entry_is_stored_under_the_rejecting_breaker_name(self):
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                return_value=DLQEntryResult.created("dlq-oc-1"),
            ) as mock_store,
        ):
            DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"),
                PolicyContext(domain="ignored_by_the_rejection_branch"),
                _rejection_result("payment_api"),
            )

        assert self._store_call(mock_store)["domain"] == "payment_api"

    def test_failure_type_is_the_open_circuit_spec_value(self):
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                return_value=DLQEntryResult.created("dlq-oc-1"),
            ) as mock_store,
        ):
            DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"),
                None,
                _rejection_result(),
            )

        assert self._store_call(mock_store)["failure_type"] == "CIRCUIT_BREAKER_OPEN"

    def test_metadata_source_marks_the_entry_as_a_policy_chain_capture(self):
        """The sweep joins on this value — an entry without it is never swept."""
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                return_value=DLQEntryResult.created("dlq-oc-1"),
            ) as mock_store,
        ):
            DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"),
                None,
                _rejection_result(),
            )

        metadata = self._store_call(mock_store)["metadata"]
        assert metadata["source"] == "policy_chain"
        assert metadata["service_name"] == "payment_api"
        assert metadata["circuit_state"] == "open"
        assert metadata["executed_policies"] == ["circuit_breaker"]

    def test_recommended_action_is_replay(self):
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                return_value=DLQEntryResult.created("dlq-oc-1"),
            ) as mock_store,
        ):
            DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"),
                None,
                _rejection_result(),
            )

        kwargs = self._store_call(mock_store)
        assert kwargs["recommended_action"] == "replay"
        assert kwargs["error_code"] == "CircuitBreakerOpenError"

    def test_replay_payload_comes_from_the_call_context(self):
        """The captured call's arguments are what makes the entry replayable."""
        ctx = PolicyContext(
            order_id="ORD-77",
            user_id="42",
            extra={"request_data": {"order_id": "ORD-77", "amount": 100}},
        )
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                return_value=DLQEntryResult.created("dlq-oc-1"),
            ) as mock_store,
        ):
            DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"), ctx, _rejection_result()
            )

        kwargs = self._store_call(mock_store)
        assert kwargs["entity_id"] == "ORD-77"
        assert kwargs["user_id"] == 42
        assert kwargs["request_data"] == {"order_id": "ORD-77", "amount": 100}


class TestDLQSinkOpenCircuitBehavior:
    """Gating, fail-open posture, and the dispatch marker."""

    @staticmethod
    def _run(
        error: Exception,
        *,
        capture_enabled: bool = True,
        store_return=None,
        store_side_effect=None,
        result: PolicyResult | None = None,
    ):
        """Run the rejection branch and hand back (return value, store mock)."""
        store_kwargs: dict = {}
        if store_side_effect is not None:
            store_kwargs["side_effect"] = store_side_effect
        else:
            store_kwargs["return_value"] = store_return or DLQEntryResult.created(
                "dlq-oc"
            )
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(capture_enabled),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq", **store_kwargs
            ) as mock_store,
        ):
            ret = DLQSink().handle_failure(
                error, None, result if result is not None else _rejection_result()
            )
        return ret, mock_store

    def test_capture_flag_on_stores_and_returns_the_entry_id(self):
        ret, mock_store = self._run(CircuitBreakerOpenError("payment_api"))

        assert ret == "dlq-oc"
        assert mock_store.call_count == 1

    def test_capture_flag_off_stores_nothing(self):
        """Negative half: the flag restores the pre-capture behavior exactly."""
        ret, mock_store = self._run(
            CircuitBreakerOpenError("payment_api"), capture_enabled=False
        )

        assert ret is None
        mock_store.assert_not_called()

    def test_non_circuit_rejection_stores_nothing(self):
        """A bulkhead-full rejection is a REJECTED terminal too — not captured."""
        ret, mock_store = self._run(
            BulkheadFullError("payment_api", max_concurrent=2, active_count=2)
        )

        assert ret is None
        mock_store.assert_not_called()

    def test_settings_read_failure_skips_capture_not_the_rejection(self):
        """Fail-open: an unreadable settings singleton must not raise."""
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                side_effect=RuntimeError("settings backend down"),
            ),
            patch("baldur.services.retry_handler.sinks.store_to_dlq") as mock_store,
        ):
            ret = DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"), None, _rejection_result()
            )

        assert ret is None
        mock_store.assert_not_called()

    def test_settings_read_failure_is_observable_as_a_skip(self):
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                side_effect=RuntimeError("settings backend down"),
            ),
            patch("baldur.services.retry_handler.sinks.store_to_dlq"),
            capture_logs() as logs,
        ):
            DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"), None, _rejection_result()
            )

        assert [
            e for e in logs if e.get("event") == "dlq_sink.open_circuit_capture_skipped"
        ]

    def test_store_exception_does_not_propagate(self):
        ret, _ = self._run(
            CircuitBreakerOpenError("payment_api"),
            store_side_effect=RuntimeError("DLQ down"),
        )

        assert ret is None

    def test_store_failure_result_returns_none(self):
        ret, _ = self._run(
            CircuitBreakerOpenError("payment_api"),
            store_return=DLQEntryResult.failed("redis_down"),
        )

        assert ret is None

    def test_observe_only_mode_stores_nothing(self):
        """Shadow mode decides without acting — a durable entry is an action."""
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch("baldur.services.retry_handler.sinks.store_to_dlq") as mock_store,
            dry_run_active(),
        ):
            ret = DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"), None, _rejection_result()
            )

        assert ret is None
        mock_store.assert_not_called()

    def test_service_name_falls_back_to_the_exception_when_metadata_lacks_it(self):
        _ret, mock_store = self._run(
            CircuitBreakerOpenError("charge_gateway"),
            result=_rejection_result("charge_gateway", with_service_name=False),
        )

        assert mock_store.call_args.kwargs["domain"] == "charge_gateway"

    def test_retry_exhaustion_terminal_still_gates_on_should_dlq(self):
        """Regression guard: the new REJECTED branch must not change the
        FAILURE terminal's Dumb-Sink contract."""
        failure = PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            total_attempts=3,
            metadata={"should_dlq": False, "domain": "test"},
        )
        with patch("baldur.services.retry_handler.sinks.store_to_dlq") as mock_store:
            ret = DLQSink().handle_failure(ValueError("boom"), None, failure)

        assert ret is None
        mock_store.assert_not_called()


class TestDLQSinkOpenCircuitMarkerBehavior:
    """The dispatch marker is what makes one rejected call one entry."""

    @staticmethod
    def _dispatch(store_result) -> CircuitBreakerOpenError:
        error = CircuitBreakerOpenError("payment_api")
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                return_value=store_result,
            ),
        ):
            DLQSink().handle_failure(error, None, _rejection_result())
        return error

    def test_successful_dispatch_marks_the_exception_with_the_entry_id(self):
        error = self._dispatch(DLQEntryResult.created("dlq-oc-9"))

        assert error.dlq_capture_dispatched is True
        assert error.dlq_id == "dlq-oc-9"

    def test_async_pre_ack_marks_the_flag_even_with_no_entry_id(self):
        """The id-truthiness trap: the async outbox acks before an id exists,
        so a later layer testing the id would store the rejection twice."""
        error = self._dispatch(DLQEntryResult(success=True, dlq_id=None))

        assert error.dlq_capture_dispatched is True
        assert error.dlq_id is None

    def test_failed_store_leaves_the_exception_unmarked(self):
        """Nothing was parked, so the next capture layer must still try."""
        error = self._dispatch(DLQEntryResult.failed("down"))

        assert error.dlq_capture_dispatched is False

    def test_disabled_capture_leaves_the_exception_unmarked(self):
        error = CircuitBreakerOpenError("payment_api")
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(False),
            ),
            patch("baldur.services.retry_handler.sinks.store_to_dlq"),
        ):
            DLQSink().handle_failure(error, None, _rejection_result())

        assert error.dlq_capture_dispatched is False
