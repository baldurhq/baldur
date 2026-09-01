"""
Unit tests for DLQRecorder failure classification, entity extraction, and action lookup.

Tests pattern constants, classify_failure_type priority, extract_entity_refs
immutability, recommended action mapping, and store() result handling.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from baldur.adapters.celery.integrations.dlq_recorder import (
    _EXCEPTION_MESSAGE_PATTERNS,
    _EXCEPTION_TYPE_PATTERNS,
    _ID_PRIORITY,
    _RECOMMENDED_ACTIONS,
    DLQRecorder,
)
from baldur.models.dlq import DLQEntryResult
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError

# =========================================================================
# Contract Tests
# =========================================================================


class TestDLQRecorderPatternsContract:
    """Design-contract values for DLQ classification constants."""

    def test_exception_type_patterns_network_error_keywords(self) -> None:
        """NETWORK_ERROR type pattern includes connection, timeout, network, socket."""
        assert _EXCEPTION_TYPE_PATTERNS["NETWORK_ERROR"] == [
            "connection",
            "timeout",
            "network",
            "socket",
        ]

    def test_exception_message_patterns_has_7_entries(self) -> None:
        """_EXCEPTION_MESSAGE_PATTERNS has 7 pattern entries."""
        assert len(_EXCEPTION_MESSAGE_PATTERNS) == 7

    def test_exception_message_patterns_first_is_rate_limited(self) -> None:
        """First message pattern maps to RATE_LIMITED."""
        _keywords, failure_type = _EXCEPTION_MESSAGE_PATTERNS[0]
        assert failure_type == "RATE_LIMITED"

    def test_recommended_actions_maps_10_failure_types(self) -> None:
        """_RECOMMENDED_ACTIONS has 10 entries."""
        assert len(_RECOMMENDED_ACTIONS) == 10

    def test_recommended_actions_contains_expected_keys(self) -> None:
        """_RECOMMENDED_ACTIONS includes all expected failure types."""
        expected_keys = {
            "NETWORK_ERROR",
            "CIRCUIT_BREAKER_OPEN",
            "TIMEOUT",
            "CONNECTION_ERROR",
            "RATE_LIMITED",
            "AUTH_ERROR",
            "VALIDATION_ERROR",
            "EXTERNAL_SERVICE_ERROR",
            "GATEWAY_ERROR",
            "UNKNOWN_ERROR",
        }
        assert set(_RECOMMENDED_ACTIONS.keys()) == expected_keys

    def test_id_priority_has_8_entries(self) -> None:
        """_ID_PRIORITY has 8 entity patterns."""
        assert len(_ID_PRIORITY) == 8

    def test_id_priority_first_is_order_id(self) -> None:
        """First ID priority entry is ('order_id', 'order')."""
        assert _ID_PRIORITY[0] == ("order_id", "order")


# =========================================================================
# Behavior Tests — classify_failure_type
# =========================================================================


class TestClassifyFailureTypeBehavior:
    """Failure type classification logic for exceptions."""

    def test_connection_error_type_returns_network_error(self) -> None:
        """ConnectionError type name matches NETWORK_ERROR pattern."""
        exc = ConnectionError("failed to connect")
        result = DLQRecorder.classify_failure_type(exc)
        assert result == "NETWORK_ERROR"

    def test_timeout_error_type_returns_network_error(self) -> None:
        """TimeoutError type name matches NETWORK_ERROR pattern (via 'timeout')."""
        exc = TimeoutError("operation timed out")
        result = DLQRecorder.classify_failure_type(exc)
        assert result == "NETWORK_ERROR"

    def test_rate_limit_message_returns_rate_limited(self) -> None:
        """Exception message containing 'rate limit' returns RATE_LIMITED."""
        exc = Exception("rate limit exceeded")
        result = DLQRecorder.classify_failure_type(exc)
        assert result == "RATE_LIMITED"

    def test_429_message_returns_rate_limited(self) -> None:
        """Exception message containing '429' returns RATE_LIMITED."""
        exc = Exception("HTTP 429 Too Many Requests")
        result = DLQRecorder.classify_failure_type(exc)
        assert result == "RATE_LIMITED"

    def test_auth_message_returns_auth_error(self) -> None:
        """Exception message containing 'unauthorized' returns AUTH_ERROR."""
        exc = Exception("unauthorized access")
        result = DLQRecorder.classify_failure_type(exc)
        assert result == "AUTH_ERROR"

    def test_validation_message_returns_validation_error(self) -> None:
        """Exception message containing 'validation' returns VALIDATION_ERROR."""
        exc = ValueError("validation failed for field X")
        result = DLQRecorder.classify_failure_type(exc)
        assert result == "VALIDATION_ERROR"

    def test_503_message_returns_external_service_error(self) -> None:
        """Exception message containing '503' returns EXTERNAL_SERVICE_ERROR."""
        exc = Exception("Service Unavailable 503")
        result = DLQRecorder.classify_failure_type(exc)
        assert result == "EXTERNAL_SERVICE_ERROR"

    def test_unknown_error_for_unrecognized_exception(self) -> None:
        """Unrecognized exception returns UNKNOWN_ERROR."""
        exc = RuntimeError("something completely unexpected happened")
        result = DLQRecorder.classify_failure_type(exc)
        assert result == "UNKNOWN_ERROR"

    def test_type_pattern_takes_priority_over_message_pattern(self) -> None:
        """Exception type name match takes priority over message match."""
        # TimeoutError type matches NETWORK_ERROR even though message has 'gateway'
        exc = TimeoutError("gateway timeout")
        result = DLQRecorder.classify_failure_type(exc)
        assert result == "NETWORK_ERROR"


# =========================================================================
# Behavior Tests — extract_entity_refs
# =========================================================================


class TestExtractEntityRefsBehavior:
    """Entity reference extraction from task kwargs."""

    def test_explicit_entity_type_and_id_takes_priority(self) -> None:
        """Explicit entity_type/entity_id in kwargs are used directly."""
        kwargs = {"entity_type": "invoice", "entity_id": "INV-001"}
        result = DLQRecorder.extract_entity_refs(kwargs)
        assert result["entity_type"] == "invoice"
        assert result["entity_id"] == "INV-001"

    def test_explicit_entity_with_user_id_included(self) -> None:
        """user_id is included when present alongside explicit entity."""
        kwargs = {
            "entity_type": "invoice",
            "entity_id": "INV-001",
            "user_id": "user-42",
        }
        result = DLQRecorder.extract_entity_refs(kwargs)
        assert result["user_id"] == "user-42"

    def test_order_id_inferred_from_kwargs(self) -> None:
        """'order_id' in kwargs infers entity_type='order'."""
        kwargs = {"order_id": "ORD-123", "amount": 100}
        result = DLQRecorder.extract_entity_refs(kwargs)
        assert result["entity_type"] == "order"
        assert result["entity_id"] == "ORD-123"

    def test_payment_id_inferred_from_kwargs(self) -> None:
        """'payment_id' in kwargs infers entity_type='payment'."""
        kwargs = {"payment_id": "PAY-456"}
        result = DLQRecorder.extract_entity_refs(kwargs)
        assert result["entity_type"] == "payment"
        assert result["entity_id"] == "PAY-456"

    def test_none_kwargs_returns_empty_dict(self) -> None:
        """None kwargs returns empty dict."""
        result = DLQRecorder.extract_entity_refs(None)
        assert result == {}

    def test_empty_kwargs_returns_empty_dict(self) -> None:
        """Empty kwargs dict returns empty dict."""
        result = DLQRecorder.extract_entity_refs({})
        assert result == {}

    def test_no_matching_id_without_user_id_returns_empty(self) -> None:
        """kwargs without any recognized ID key returns empty dict."""
        kwargs = {"foo": "bar", "baz": 42}
        result = DLQRecorder.extract_entity_refs(kwargs)
        assert result == {}

    def test_user_id_only_infers_user_entity_and_includes_user_id(self) -> None:
        """kwargs with only user_id infers entity_type='user' and includes user_id."""
        kwargs = {"foo": "bar", "user_id": "user-1"}
        result = DLQRecorder.extract_entity_refs(kwargs)
        assert result["entity_type"] == "user"
        assert result["entity_id"] == "user-1"
        assert result["user_id"] == "user-1"

    def test_none_id_value_is_skipped(self) -> None:
        """Kwargs with None id value are skipped during inference."""
        kwargs = {"order_id": None, "payment_id": "PAY-001"}
        result = DLQRecorder.extract_entity_refs(kwargs)
        assert result["entity_type"] == "payment"
        assert result["entity_id"] == "PAY-001"

    def test_extract_does_not_mutate_input_kwargs(self) -> None:
        """Input kwargs dict is not mutated by extraction."""
        kwargs = {"order_id": "ORD-1", "extra": "data"}
        kwargs_copy = kwargs.copy()
        DLQRecorder.extract_entity_refs(kwargs)
        assert kwargs == kwargs_copy

    def test_id_priority_order_respected(self) -> None:
        """When multiple ID keys exist, first in _ID_PRIORITY wins."""
        # order_id has higher priority than payment_id
        kwargs = {"payment_id": "PAY-1", "order_id": "ORD-1"}
        result = DLQRecorder.extract_entity_refs(kwargs)
        assert result["entity_type"] == "order"
        assert result["entity_id"] == "ORD-1"


# =========================================================================
# Behavior Tests — get_recommended_action
# =========================================================================


class TestGetRecommendedActionBehavior:
    """Recommended action lookup for failure types."""

    def test_known_failure_type_returns_specific_action(self) -> None:
        """Known failure type returns its mapped action."""
        result = DLQRecorder.get_recommended_action("NETWORK_ERROR")
        assert result == _RECOMMENDED_ACTIONS["NETWORK_ERROR"]

    def test_rate_limited_returns_specific_action(self) -> None:
        """RATE_LIMITED returns wait-and-retry recommendation."""
        result = DLQRecorder.get_recommended_action("RATE_LIMITED")
        assert result == _RECOMMENDED_ACTIONS["RATE_LIMITED"]

    def test_unknown_failure_type_returns_fallback(self) -> None:
        """Unrecognized failure type returns fallback action."""
        result = DLQRecorder.get_recommended_action("NEVER_SEEN_BEFORE")
        assert result == "Review and retry manually"


# =========================================================================
# store() Result Handling
# =========================================================================


class TestDLQRecorderStoreResultHandling:
    """store() logs from the backing's DLQEntryResult, not unconditionally."""

    def _store(self) -> None:
        DLQRecorder().store(
            domain="payment",
            task_name="tasks.charge",
            task_id="t-1",
            exception=ValueError("boom"),
            args=(),
            kwargs={},
            einfo=None,
        )

    def test_success_result_logs_entry_stored(self) -> None:
        """success=True logs baldur_dlq.entry_stored with the real dlq_id."""
        with (
            patch(
                "baldur.adapters.celery.integrations.dlq_recorder.store_to_dlq",
                return_value=DLQEntryResult.created("dlq-1"),
            ),
            patch(
                "baldur.adapters.celery.integrations.dlq_recorder.logger",
            ) as mock_logger,
        ):
            self._store()

        mock_logger.info.assert_called_once()
        assert mock_logger.info.call_args[0][0] == "baldur_dlq.entry_stored"
        assert mock_logger.info.call_args[1]["dlq_id"] == "dlq-1"
        mock_logger.warning.assert_not_called()

    def test_rejected_result_logs_entry_store_failed_warning(self) -> None:
        """success=False logs baldur_dlq.entry_store_failed at WARNING, never entry_stored."""
        with (
            patch(
                "baldur.adapters.celery.integrations.dlq_recorder.store_to_dlq",
                return_value=DLQEntryResult.failed("DLQ is disabled"),
            ),
            patch(
                "baldur.adapters.celery.integrations.dlq_recorder.logger",
            ) as mock_logger,
        ):
            self._store()

        mock_logger.info.assert_not_called()
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == "baldur_dlq.entry_store_failed"
        assert mock_logger.warning.call_args[1]["error"] == "DLQ is disabled"


# =========================================================================
# Contract Tests — open-circuit classification
# =========================================================================


class TestDLQRecorderOpenCircuitPatternsContract:
    """The circuit-breaker rejection's classification is a type-name match."""

    def test_circuit_breaker_open_is_an_exception_type_pattern(self) -> None:
        """Matching on the type name — not the message — is what keeps a
        service name containing a keyword from choosing the failure type."""
        assert _EXCEPTION_TYPE_PATTERNS["CIRCUIT_BREAKER_OPEN"] == [
            "circuitbreakeropen"
        ]

    def test_circuit_breaker_open_has_a_recommended_action(self) -> None:
        """A stored entry needs an operator-facing next step."""
        assert (
            _RECOMMENDED_ACTIONS["CIRCUIT_BREAKER_OPEN"]
            == "Wait for circuit recovery, then auto-replay"
        )


# =========================================================================
# Behavior Tests — open-circuit classification
# =========================================================================


class TestDLQRecorderClassificationBehavior:
    """``classify_failure_type`` on a circuit-breaker rejection.

    The field is replay-selecting: an entry typed ``UNKNOWN_ERROR`` cannot be
    picked out of the queue by what actually went wrong.
    """

    def test_circuit_breaker_open_error_classifies_as_circuit_breaker_open(
        self,
    ) -> None:
        exc = CircuitBreakerOpenError("payment_api")

        assert DLQRecorder.classify_failure_type(exc) == "CIRCUIT_BREAKER_OPEN"

    def test_service_name_with_a_gateway_keyword_does_not_divert_the_type(
        self,
    ) -> None:
        """The rejection message embeds the breaker's name. A service called
        ``payment-gateway`` must still classify by the exception's type, not by
        the ``gateway`` keyword its message happens to carry."""
        exc = CircuitBreakerOpenError("payment-gateway")

        assert "gateway" in str(exc).lower()
        assert DLQRecorder.classify_failure_type(exc) == "CIRCUIT_BREAKER_OPEN"

    def test_service_name_with_an_auth_keyword_does_not_divert_the_type(self) -> None:
        exc = CircuitBreakerOpenError("auth-service")

        assert DLQRecorder.classify_failure_type(exc) == "CIRCUIT_BREAKER_OPEN"

    def test_recommended_action_follows_the_classification(self) -> None:
        exc = CircuitBreakerOpenError("payment_api")

        failure_type = DLQRecorder.classify_failure_type(exc)

        assert (
            DLQRecorder.get_recommended_action(failure_type)
            == _RECOMMENDED_ACTIONS["CIRCUIT_BREAKER_OPEN"]
        )


# =========================================================================
# Behavior Tests — cross-layer dedup
# =========================================================================


class TestDLQRecorderDedupBehavior:
    """One rejected call, one entry.

    A task body whose ``protect(dlq=True)`` call was rejected raises that same
    exception object out of the task, so the Celery capture site sees a
    rejection the policy-chain sink has already parked. Storing it again would
    put one call in the queue twice — and a duplicate replay is the outcome the
    queue exists to prevent.
    """

    @staticmethod
    def _store(exception: Exception) -> None:
        DLQRecorder().store(
            domain="payment",
            task_name="tasks.charge",
            task_id="t-1",
            exception=exception,
            args=(),
            kwargs={},
            einfo=None,
        )

    def test_already_captured_rejection_is_not_stored_again(self) -> None:
        error = CircuitBreakerOpenError("payment_api")
        error.mark_dlq_capture_dispatched("dlq-1")

        with patch(
            "baldur.adapters.celery.integrations.dlq_recorder.store_to_dlq"
        ) as mock_store:
            self._store(error)

        mock_store.assert_not_called()

    def test_unmarked_rejection_is_stored(self) -> None:
        """Positive control: without the marker this IS the capture site."""
        with patch(
            "baldur.adapters.celery.integrations.dlq_recorder.store_to_dlq",
            return_value=DLQEntryResult.created("dlq-2"),
        ) as mock_store:
            self._store(CircuitBreakerOpenError("payment_api"))

        mock_store.assert_called_once()

    def test_async_pre_ack_marker_still_suppresses_the_second_store(self) -> None:
        """The outbox acks with no id — the flag, not the id, is the signal."""
        error = CircuitBreakerOpenError("payment_api")
        error.mark_dlq_capture_dispatched(None)

        with patch(
            "baldur.adapters.celery.integrations.dlq_recorder.store_to_dlq"
        ) as mock_store:
            self._store(error)

        assert error.dlq_id is None
        mock_store.assert_not_called()

    def test_ordinary_failure_is_unaffected_by_the_dedup_gate(self) -> None:
        """Only an exception a capture layer marked is skipped."""
        with patch(
            "baldur.adapters.celery.integrations.dlq_recorder.store_to_dlq",
            return_value=DLQEntryResult.created("dlq-3"),
        ) as mock_store:
            self._store(ValueError("boom"))

        mock_store.assert_called_once()

    def test_skip_is_observable_as_a_debug_log(self) -> None:
        error = CircuitBreakerOpenError("payment_api")
        error.mark_dlq_capture_dispatched("dlq-1")

        with (
            patch("baldur.adapters.celery.integrations.dlq_recorder.store_to_dlq"),
            patch(
                "baldur.adapters.celery.integrations.dlq_recorder.logger"
            ) as mock_logger,
        ):
            self._store(error)

        mock_logger.debug.assert_called_once()
        assert (
            mock_logger.debug.call_args[0][0]
            == "baldur_dlq.entry_store_skipped_already_captured"
        )

    def test_one_rejected_call_yields_exactly_one_store_across_both_layers(
        self,
    ) -> None:
        """Exactly-once across the sink and the Celery layer in one attempt.

        Both layers are counted against ONE store double, so the assertion is
        the entry count a real attempt produces — not two independent
        per-layer checks that could each pass while the pair double-stores.
        """
        from baldur.interfaces.resilience_policy import PolicyOutcome, PolicyResult
        from baldur.services.retry_handler.sinks import DLQSink

        error = CircuitBreakerOpenError("payment_api")
        rejection = PolicyResult(
            outcome=PolicyOutcome.REJECTED,
            total_attempts=1,
            metadata={"service_name": "payment_api", "state": "open"},
        )
        calls: list[str] = []

        def _record_store(**kwargs: object) -> DLQEntryResult:
            calls.append(str(kwargs.get("failure_type")))
            return DLQEntryResult.created("dlq-only-one")

        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=SimpleNamespace(open_circuit_capture_enabled=True),
            ),
            patch(
                "baldur.services.retry_handler.sinks.store_to_dlq",
                side_effect=_record_store,
            ),
            patch(
                "baldur.adapters.celery.integrations.dlq_recorder.store_to_dlq",
                side_effect=_record_store,
            ),
        ):
            # Layer 1 — the policy-chain sink parks the rejection.
            DLQSink().handle_failure(error, None, rejection)
            # Layer 2 — the same exception object propagates out of the task.
            self._store(error)

        assert calls == ["CIRCUIT_BREAKER_OPEN"]
