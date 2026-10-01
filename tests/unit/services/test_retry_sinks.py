"""
Unit tests for DLQSink (Dead Letter Queue Sink).

Target: services/retry_handler/sinks.py
- DLQSink: DLQ store gated on the should_dlq flag, fail-open
- the two lanes (verdict, open circuit) and the record every exit that stores
  nothing leaves (``dlq_sink.capture_skipped`` reasons, the observe-only
  would-store decision, the capture service's own refusals)
- the open-circuit custody mark that keeps a nested site from parking one
  rejection twice
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.core.exceptions import TimeoutPolicyError
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)
from baldur.models.dlq import DLQConfig, DLQEntryResult
from baldur.services.bulkhead.exceptions import BulkheadFullError
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.dlq_capture import DLQCaptureService, reset_overflow_state
from baldur.services.retry_handler.sinks import DLQSink
from baldur.settings.dlq import reset_dlq_settings
from baldur.settings.dlq_outbox import reset_dlq_outbox_settings
from tests.factories import dry_run_active

# 518 batch (a): the sticky-flag baldur_pro resolver (``#485 D1b/G4``) and its
# ``_reset_baldur_pro_dlq_resolver`` cache reset were removed once
# ``baldur.dlq.helpers.store_to_dlq`` took over the fail-open contract. Patches
# now target the helper-binding location on the sink module directly, so no
# per-test resolver reset is needed.


# =============================================================================
# DLQSink — contract verification
# =============================================================================


class TestDLQSinkContract:
    """DLQSink structure and default verification."""

    def test_has_handle_failure_method(self):
        """DLQSink has a handle_failure method."""
        assert hasattr(DLQSink(), "handle_failure")


# =============================================================================
# DLQSink — behavior verification
# =============================================================================


class TestDLQSinkBehavior:
    """DLQSink behavior verification: the should_dlq flag and the fail-open principle."""

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
        """should_dlq=False does not call _store_to_dlq."""
        sink = DLQSink()
        result = self._make_result(should_dlq=False)
        ret = sink.handle_failure(Exception("err"), self._make_context(), result)
        assert ret is None

    def test_skips_when_should_dlq_key_missing(self):
        """A missing should_dlq key does not call _store_to_dlq."""
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
        """should_dlq=True calls store_to_dlq."""
        mock_store.return_value = MagicMock(success=True, dlq_id="dlq-123")
        sink = DLQSink()
        result = self._make_result(should_dlq=True)
        ctx = self._make_context()
        err = ValueError("fail")

        ret = sink.handle_failure(err, ctx, result)
        mock_store.assert_called_once()
        assert ret == "dlq-123"

    def test_handles_store_failure_gracefully(self):
        """A failing store_to_dlq call does not propagate an exception (fail-open)."""
        sink = DLQSink()
        result = self._make_result(should_dlq=True)
        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            side_effect=RuntimeError("DLQ down"),
        ):
            ret = sink.handle_failure(Exception("err"), self._make_context(), result)
            assert ret is None

    def test_handles_import_error_gracefully(self):
        """A store_to_dlq import failure does not propagate an exception (fail-open)."""
        sink = DLQSink()
        result = self._make_result(should_dlq=True)
        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            side_effect=ImportError("no module"),
        ):
            ret = sink.handle_failure(Exception("err"), self._make_context(), result)
            assert ret is None

    def test_context_none_is_safe(self):
        """context=None works without error."""
        sink = DLQSink()
        result = self._make_result(should_dlq=True)
        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            return_value=MagicMock(success=True, dlq_id="dlq-456"),
        ):
            ret = sink.handle_failure(Exception("err"), None, result)
            assert ret == "dlq-456"

    def test_timeout_terminal_with_a_verdict_stores_under_its_domain(self):
        """A TIMEOUT terminal carrying a verdict takes the final-failure branch —
        the branch a composer armed for unretried failures relies on."""
        # Given the terminal an armed composer builds when the bound cuts a call
        error = TimeoutPolicyError(5.0)
        result = PolicyResult(
            outcome=PolicyOutcome.TIMEOUT,
            error=error,
            total_attempts=1,
            metadata={
                "timeout_seconds": 5.0,
                "should_dlq": True,
                "domain": "summarize",
                "max_attempts": 1,
                "retry_history": [],
                "reason": "max_attempts",
            },
        )

        # When the sink handles it
        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            autospec=True,
            return_value=DLQEntryResult.created("dlq-t1"),
        ) as mock_store:
            ret = DLQSink().handle_failure(error, None, result)

        # Then one entry is stored under that domain, named for the timeout
        assert ret == "dlq-t1"
        kwargs = mock_store.call_args.kwargs
        assert kwargs["domain"] == "summarize"
        assert kwargs["failure_type"] == "MAX_RETRIES_TIMEOUTPOLICYERROR"
        assert kwargs["metadata"]["max_attempts"] == 1
        assert kwargs["metadata"]["retry_history"] == []


# =============================================================================
# DLQSink — skip vs error distinguishability (Cat 1.9, scenario plan §328)
# =============================================================================
#
# Criterion (plan §328 row 1.9): "DLQ sink distinguishes 'not stored (skip)'
# from 'store failed (error)'." The return value (str | None) alone cannot tell the
# terminals (skip / stored / failed / exception) apart — caller-side distinction is
# impossible without a sweeping change to the Protocol return type — so this pins
# the *log-level* visibility the current implementation already provides as a
# regression gate:
#
#   - skip:      `dlq_sink.create_dlq_entry_failed` is not emitted
#                (silent — store_to_dlq itself is never called)
#   - stored:    `dlq_sink.created_dlq_entry` (info) emit
#   - failed:    `dlq_sink.create_dlq_entry_failed` (error) emit, kwarg=result
#   - exception: `dlq_sink.create_dlq_entry_failed` (error) emit, kwarg=dlq_error
#
# Protocol-level distinguishability (a return-type change) is outside this test's
# scope — revisit when a FailureSink implementation other than the composer.py call
# site + ThrottleDLQSink is added (out-of-scope follow-up).


class TestDLQSinkLogDistinguishability:
    """DLQSink distinguishes the skip / failure paths through log visibility."""

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


class TestNonIntegerUserIdBehavior:
    """A user identifier with no integer form leaves the integer ``user_id``
    column empty and never costs the entry — on both store branches."""

    @pytest.mark.parametrize(
        ("named_user_id", "extra_user_id"),
        [("usr_7f3a", None), (None, "9b2c7e1a-uuid"), ("3.5", None)],
        ids=["named_prefixed_id", "extra_uuid_like", "named_decimal_string"],
    )
    def test_non_integer_user_id_leaves_the_column_empty(
        self, named_user_id, extra_user_id
    ):
        extra = {} if extra_user_id is None else {"user_id": extra_user_id}
        ctx = PolicyContext(user_id=named_user_id, extra=extra)

        fields = DLQSink._extract_context_fields(ctx)

        assert fields["user_id"] is None

    def test_final_failure_with_a_non_integer_user_id_is_stored(self):
        # Given a final failure whose context carries a prefixed user id
        ctx = PolicyContext(
            user_id="usr_7f3a",
            extra={"request_data": {"user_id": "usr_7f3a"}},
        )
        result = PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            total_attempts=1,
            metadata={"should_dlq": True, "domain": "billing"},
        )

        # When the sink handles it
        with patch(
            "baldur.services.retry_handler.sinks.store_to_dlq",
            autospec=True,
            return_value=DLQEntryResult.created("dlq-u1"),
        ) as mock_store:
            ret = DLQSink().handle_failure(RuntimeError("down"), ctx, result)

        # Then the entry is stored, the raw id kept in its request data
        assert ret == "dlq-u1"
        kwargs = mock_store.call_args.kwargs
        assert kwargs["user_id"] is None
        assert kwargs["request_data"] == {"user_id": "usr_7f3a"}

    def test_open_circuit_rejection_with_a_non_integer_user_id_is_stored(self):
        ctx = PolicyContext(user_id="usr_7f3a")

        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                autospec=True,
                return_value=DLQEntryResult.created("dlq-u2"),
            ) as mock_store,
        ):
            ret = DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"), ctx, _rejection_result()
            )

        assert ret == "dlq-u2"
        assert mock_store.call_args.kwargs["user_id"] is None


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

    def test_non_circuit_rejection_takes_the_verdict_lane(self):
        """A bulkhead-full rejection is a failed call: stored on its verdict,
        under its domain, never as an open-circuit capture."""
        error = BulkheadFullError("payment_api", max_concurrent=2, active_count=2)
        verdict = PolicyResult(
            outcome=PolicyOutcome.REJECTED,
            error=error,
            total_attempts=1,
            metadata={"should_dlq": True, "domain": "payment_api"},
        )
        _ret, mock_store = self._run(error, result=verdict)

        assert mock_store.call_count == 1
        assert mock_store.call_args.kwargs["domain"] == "payment_api"
        assert (
            mock_store.call_args.kwargs["failure_type"]
            == "MAX_RETRIES_BULKHEADFULLERROR"
        )

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
            e
            for e in logs
            if e.get("event") == "dlq_sink.capture_skipped"
            and e.get("reason") == "settings_unreadable"
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

    def test_local_fallback_record_marks_the_exception_as_taken_into_custody(self):
        """The store's backend failed but a local fallback record holds the
        entry: an enclosing site must not write a second copy."""
        error = self._dispatch(
            DLQEntryResult.fallback("backend down", "disk_persistent_buffer://dlq")
        )

        assert error.dlq_capture_dispatched is True
        assert error.dlq_id is None

    def test_raising_store_leaves_the_exception_unmarked(self):
        """The store kept nothing, so the enclosing site tries again."""
        error = CircuitBreakerOpenError("payment_api")
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                autospec=True,
                side_effect=RuntimeError("DLQ down"),
            ),
        ):
            DLQSink().handle_failure(error, None, _rejection_result())

        assert error.dlq_capture_dispatched is False

    def test_rejection_already_in_custody_is_not_stored_again(self):
        """The outer half of a nested ``dlq=True`` site: the rejection the
        inner site parked propagates out marked, and is skipped."""
        error = CircuitBreakerOpenError("payment_api")
        error.mark_dlq_capture_dispatched("dlq-inner")
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq", autospec=True
            ) as mock_store,
        ):
            ret = DLQSink().handle_failure(error, None, _rejection_result())

        assert ret is None
        mock_store.assert_not_called()
        assert error.dlq_id == "dlq-inner"


# =============================================================================
# DLQSink — every exit that stores nothing leaves a record
# =============================================================================
#
# Four of these records sit below the default WARNING level by design, which
# is why the DLQ guide names each rule's event and level: an operator reading
# why a call was not parked knows which level to turn on. Tests whose name
# carries "excluded" pin the record of a stated exclusion rule.

SKIP_EVENT = "dlq_sink.capture_skipped"

_STORE = "baldur.services.retry_handler.sinks.store_to_dlq"


def _skip_records(logs: list) -> list:
    return [e for e in logs if e.get("event") == SKIP_EVENT]


def _verdict_terminal(error: Exception, **metadata) -> PolicyResult:
    """A non-open-circuit terminal carrying ``metadata`` as its verdict."""
    return PolicyResult(
        outcome=PolicyOutcome.FAILURE,
        error=error,
        total_attempts=1,
        metadata=metadata,
    )


class TestDLQSinkSkipRecordBehavior:
    """An opt-out exit logs ``dlq_sink.capture_skipped`` at DEBUG with its
    reason; observe-only logs the would-store decision. Every case also pins
    that the store was never called."""

    def test_verdictless_failure_is_skipped_as_no_verdict(self):
        """A composer no call site armed — a caller's own ``compose()``."""
        error = RuntimeError("upstream 500")
        with patch(_STORE, autospec=True) as mock_store, capture_logs() as logs:
            ret = DLQSink().handle_failure(
                error, None, _verdict_terminal(error, domain="payment")
            )

        assert ret is None
        mock_store.assert_not_called()
        [record] = _skip_records(logs)
        assert record["log_level"] == "debug"
        assert record["reason"] == "no_verdict"
        assert record["error_type"] == "RuntimeError"

    def test_excluded_stage_declined_verdict_is_skipped_as_stage_declined(self):
        """Rule: a retry stage the caller built with ``enable_dlq=False``."""
        error = RuntimeError("upstream 500")
        with patch(_STORE, autospec=True) as mock_store, capture_logs() as logs:
            ret = DLQSink().handle_failure(
                error,
                None,
                _verdict_terminal(error, should_dlq=False, domain="payment"),
            )

        assert ret is None
        mock_store.assert_not_called()
        [record] = _skip_records(logs)
        assert record["log_level"] == "debug"
        assert record["reason"] == "stage_declined"
        assert record["domain"] == "payment"
        assert record["error_type"] == "RuntimeError"

    def test_excluded_open_circuit_capture_switched_off_is_skipped_with_its_reason(
        self,
    ):
        """Rule: ``BALDUR_DLQ_OPEN_CIRCUIT_CAPTURE_ENABLED=false``."""
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(False),
            ),
            patch(_STORE, autospec=True) as mock_store,
            capture_logs() as logs,
        ):
            ret = DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"), None, _rejection_result()
            )

        assert ret is None
        mock_store.assert_not_called()
        [record] = _skip_records(logs)
        assert record["log_level"] == "debug"
        assert record["reason"] == "open_circuit_capture_disabled"
        assert record["healing_domain"] == "payment_api"

    def test_unreadable_settings_are_skipped_as_settings_unreadable(self):
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                side_effect=RuntimeError("settings backend down"),
            ),
            patch(_STORE, autospec=True) as mock_store,
            capture_logs() as logs,
        ):
            ret = DLQSink().handle_failure(
                CircuitBreakerOpenError("payment_api"), None, _rejection_result()
            )

        assert ret is None
        mock_store.assert_not_called()
        [record] = _skip_records(logs)
        assert record["log_level"] == "debug"
        assert record["reason"] == "settings_unreadable"
        assert record["error"] == "settings backend down"

    def test_rejection_an_inner_site_parked_is_skipped_as_already_captured(self):
        """Not an exclusion: the call is parked, by the inner site."""
        error = CircuitBreakerOpenError("payment_api")
        error.mark_dlq_capture_dispatched("dlq-inner")
        with patch(_STORE, autospec=True) as mock_store, capture_logs() as logs:
            ret = DLQSink().handle_failure(error, None, _rejection_result())

        assert ret is None
        mock_store.assert_not_called()
        [record] = _skip_records(logs)
        assert record["log_level"] == "debug"
        assert record["reason"] == "already_captured"
        assert record["healing_domain"] == "payment_api"
        assert record["result"] == "dlq-inner"

    @pytest.mark.parametrize("lane", ["verdict", "open_circuit"])
    def test_excluded_observe_only_call_logs_the_would_store_decision(self, lane):
        """Rule: observe-only mode (dry-run, shadow, evaluation) — INFO, by
        dry-run's design, not a skip record."""
        if lane == "verdict":
            error: Exception = RuntimeError("upstream 500")
            terminal = _verdict_terminal(error, should_dlq=True, domain="payment")
        else:
            error = CircuitBreakerOpenError("payment")
            terminal = _rejection_result("payment")
        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=_capture_settings(),
            ),
            patch(_STORE, autospec=True) as mock_store,
            dry_run_active(),
            capture_logs() as logs,
        ):
            ret = DLQSink().handle_failure(error, None, terminal)

        assert ret is None
        mock_store.assert_not_called()
        suppressed = [
            e
            for e in logs
            if e.get("event") == "execution_mode.intervention_suppressed"
        ]
        assert len(suppressed) == 1
        assert suppressed[0]["log_level"] == "info"
        assert suppressed[0]["action"] == "dlq_store"
        assert suppressed[0]["service_name"] == "payment"
        assert _skip_records(logs) == []


class _DomainAtCapacityRepository(InMemoryFailedOperationRepository):
    """An in-memory repository whose every domain reports a full queue, so
    the overflow check refuses without seeding thousands of entries."""

    def count_by_domain(self, domain: str) -> int:
        return 10_000_000


@pytest.fixture
def sync_store_under_reject_overflow(monkeypatch) -> Iterator[None]:
    """The ``reject`` overflow strategy, checked on every store, with the
    outbox off so the store — and its refusal — runs on the calling thread."""
    monkeypatch.setenv("BALDUR_DLQ_OVERFLOW_STRATEGY", "reject")
    monkeypatch.setenv("BALDUR_DLQ_OVERFLOW_CHECK_INTERVAL", "1")
    monkeypatch.setenv("BALDUR_DLQ_OUTBOX_ENABLED", "false")
    reset_dlq_settings()
    reset_dlq_outbox_settings()
    reset_overflow_state()
    yield
    reset_dlq_settings()
    reset_dlq_outbox_settings()
    reset_overflow_state()


class TestDLQSinkCaptureServiceExclusionBehavior:
    """The capture service's own refusals reach the sink as a failed store;
    the records come from the real service behind the backing seam."""

    @staticmethod
    def _park(service: DLQCaptureService) -> tuple[str | None, list]:
        error = RuntimeError("upstream 500")
        with (
            patch(
                "baldur.services.dlq_capture.resolve_dlq_backing",
                autospec=True,
                return_value=service,
            ),
            capture_logs() as logs,
        ):
            ret = DLQSink().handle_failure(
                error,
                None,
                _verdict_terminal(error, should_dlq=True, domain="payment"),
            )
        return ret, logs

    def test_excluded_dlq_switched_off_logs_store_skipped_and_entry_failed(self):
        """Rule: ``BALDUR_DLQ_ENABLED=false`` — the service's DEBUG skip and
        the sink's ERROR for the store it did not keep."""
        repository = InMemoryFailedOperationRepository()
        service = DLQCaptureService(
            config=DLQConfig(enabled=False), repository=repository
        )

        ret, logs = self._park(service)

        assert ret is None
        assert repository.count_all() == 0
        records = {(e["event"], e["log_level"]) for e in logs}
        assert ("dlq.store_skipped_disabled", "debug") in records
        assert ("dlq_sink.create_dlq_entry_failed", "error") in records

    def test_excluded_queue_full_under_reject_logs_store_rejected_overflow(
        self, sync_store_under_reject_overflow
    ):
        """Rule: the queue is full under the ``reject`` overflow strategy."""
        repository = _DomainAtCapacityRepository()
        service = DLQCaptureService(config=DLQConfig(), repository=repository)

        ret, logs = self._park(service)

        assert ret is None
        assert repository.count_all() == 0
        rejected = [e for e in logs if e.get("event") == "dlq.store_rejected_overflow"]
        assert len(rejected) == 1
        assert rejected[0]["log_level"] == "warning"
        assert rejected[0]["domain"] == "payment"


# =============================================================================
# DLQSink — the capture mark on a stored terminal
# =============================================================================


class _RefusesAttributes(Exception):
    """An exception type that refuses new attributes (a frozen or slotted type)."""

    def __setattr__(self, name, value):
        raise AttributeError(f"{type(self).__name__} is frozen")


class TestDLQSinkCaptureMarkBehavior:
    """A terminal the sink parked is marked, so the Celery hook does not park it again."""

    _STORE = "baldur.services.retry_handler.sinks.store_to_dlq"

    def _verdict(self) -> PolicyResult:
        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            total_attempts=1,
            metadata={"should_dlq": True, "domain": "job.summarize"},
        )

    def test_successful_store_marks_the_exception(self):
        """Stored → ``dlq_capture_dispatched`` is set on the very object raised."""
        error = TimeoutError("no endpoint answered")
        with patch(
            self._STORE, autospec=True, return_value=DLQEntryResult.created("dlq-7")
        ) as store:
            stored_id = DLQSink().handle_failure(error, None, self._verdict())

        assert stored_id == "dlq-7"
        assert store.call_args.kwargs["failure_type"] == "MAX_RETRIES_TIMEOUTERROR"
        assert error.dlq_capture_dispatched is True

    def test_failed_store_leaves_the_exception_unmarked(self):
        """Not stored → no mark, so a later capture layer still parks it."""
        error = TimeoutError("no endpoint answered")
        with patch(
            self._STORE,
            autospec=True,
            return_value=DLQEntryResult.failed("overflow rejected"),
        ):
            DLQSink().handle_failure(error, None, self._verdict())

        assert getattr(error, "dlq_capture_dispatched", False) is False

    def test_raising_store_leaves_the_exception_unmarked(self):
        """A store that raises parked nothing, so nothing is marked."""
        error = TimeoutError("no endpoint answered")
        with patch(self._STORE, autospec=True, side_effect=RuntimeError("DLQ down")):
            DLQSink().handle_failure(error, None, self._verdict())

        assert getattr(error, "dlq_capture_dispatched", False) is False

    def test_exception_refusing_attributes_is_stored_and_left_unmarked(self):
        """Best effort: a type that refuses the mark is still stored, and the sink says why."""
        error = _RefusesAttributes("frozen")
        with (
            patch(
                self._STORE,
                autospec=True,
                return_value=DLQEntryResult.created("dlq-8"),
            ),
            capture_logs() as logs,
        ):
            stored_id = DLQSink().handle_failure(error, None, self._verdict())

        assert stored_id == "dlq-8"
        assert not hasattr(error, "dlq_capture_dispatched")
        assert [
            log["error_type"]
            for log in logs
            if log["event"] == "dlq_sink.capture_mark_skipped"
        ] == ["_RefusesAttributes"]
