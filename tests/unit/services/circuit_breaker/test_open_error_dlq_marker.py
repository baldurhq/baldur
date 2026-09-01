"""``CircuitBreakerOpenError``'s DLQ-capture marker.

One rejected call must produce one DLQ entry even though several layers in
the same process see the same propagating exception: the policy-chain sink
parks it, and the Celery capture sites must then skip their own store.

The marker is a flag, deliberately NOT the entry id — the async outbox
acknowledges the store before an id exists, so a later layer testing the id's
truthiness would park the same rejection a second time.
"""

from __future__ import annotations

from baldur.interfaces.resilience_policy import PolicyRejectedException
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError

# =============================================================================
# Behavior — marker lifecycle
# =============================================================================


class TestCircuitBreakerOpenErrorBehavior:
    """``mark_dlq_capture_dispatched`` and the state it leaves behind."""

    def test_fresh_error_is_unmarked(self):
        """A rejection nobody parked yet must not make a later layer skip."""
        error = CircuitBreakerOpenError("payment_api")

        assert error.dlq_capture_dispatched is False
        assert error.dlq_id is None

    def test_marking_with_an_id_records_both_flag_and_id(self):
        error = CircuitBreakerOpenError("payment_api")

        error.mark_dlq_capture_dispatched("dlq-1")

        assert error.dlq_capture_dispatched is True
        assert error.dlq_id == "dlq-1"

    def test_marking_without_an_id_still_sets_the_flag(self):
        """The async pre-ack path: dispatched, no id yet — still one entry."""
        error = CircuitBreakerOpenError("payment_api")

        error.mark_dlq_capture_dispatched()

        assert error.dlq_capture_dispatched is True
        assert error.dlq_id is None

    def test_marking_twice_is_idempotent(self):
        error = CircuitBreakerOpenError("payment_api")

        error.mark_dlq_capture_dispatched("dlq-1")
        error.mark_dlq_capture_dispatched("dlq-1")

        assert error.dlq_capture_dispatched is True
        assert error.dlq_id == "dlq-1"

    def test_a_later_idless_mark_does_not_erase_a_recorded_id(self):
        """The id is forensic: once a store returned one, keep it."""
        error = CircuitBreakerOpenError("payment_api")
        error.mark_dlq_capture_dispatched("dlq-1")

        error.mark_dlq_capture_dispatched(None)

        assert error.dlq_id == "dlq-1"

    def test_marker_state_is_per_instance(self):
        """Two rejections of the same breaker are two work units."""
        first = CircuitBreakerOpenError("payment_api")
        second = CircuitBreakerOpenError("payment_api")

        first.mark_dlq_capture_dispatched("dlq-1")

        assert second.dlq_capture_dispatched is False
        assert second.dlq_id is None

    def test_marking_leaves_the_rejection_identity_intact(self):
        """The marker is bookkeeping — it must not change what the caller sees."""
        error = CircuitBreakerOpenError("payment_api")

        error.mark_dlq_capture_dispatched("dlq-1")

        assert error.service_name == "payment_api"
        assert error.extra_context() == {"service_name": "payment_api"}
        assert isinstance(error, PolicyRejectedException)
