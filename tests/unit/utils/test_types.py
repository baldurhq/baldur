"""
Unit tests for core types.
"""

from datetime import datetime


class TestCircuitState:
    """Tests for CircuitState enum."""

    def test_states_exist(self):
        from baldur.interfaces.repositories import (
            CircuitBreakerStateEnum as CircuitState,
        )

        assert CircuitState.CLOSED == "closed"
        assert CircuitState.OPEN == "open"
        assert CircuitState.HALF_OPEN == "half_open"


class TestFailedOperationData:
    """Tests for FailedOperationData dataclass (interfaces/repositories.py canonical source)."""

    def test_create_with_required_fields(self):
        from baldur.interfaces.repositories import FailedOperationData

        data = FailedOperationData(
            id=1,
            domain="payment",
            failure_type="network",
            status="pending",
        )

        assert data.id == 1
        assert data.domain == "payment"
        assert data.retry_count == 0  # default
        assert data.max_retries == 2  # default
        assert data.metadata == {}  # default

    def test_create_with_all_fields(self):
        from baldur.interfaces.repositories import FailedOperationData

        now = datetime.now()
        data = FailedOperationData(
            id=42,
            domain="order",
            failure_type="database",
            status="processing",
            created_at=now,
            metadata={"order_id": 123},
            error_message="DB connection failed",
            retry_count=2,
            max_retries=5,
        )

        assert data.id == 42
        assert data.metadata["order_id"] == 123
        assert data.retry_count == 2

    def test_properties(self):
        from baldur.interfaces.repositories import FailedOperationData

        data = FailedOperationData(
            id=1,
            domain="order",
            failure_type="network",
            status="pending",
            retry_count=0,
            max_retries=3,
        )
        assert data.is_pending is True
        assert data.is_resolved is False
        assert data.can_retry is True

    def test_can_retry_false_when_exhausted(self):
        from baldur.interfaces.repositories import FailedOperationData

        data = FailedOperationData(
            id=1,
            domain="order",
            failure_type="network",
            status="pending",
            retry_count=3,
            max_retries=3,
        )
        assert data.can_retry is False


class TestCircuitBreakerStateData:
    """Tests for CircuitBreakerStateData dataclass (interfaces/repositories.py canonical source)."""

    def test_create_with_defaults(self):
        from baldur.interfaces.repositories import CircuitBreakerStateData

        data = CircuitBreakerStateData(
            service_name="test-service",
        )

        assert data.service_name == "test-service"
        assert data.state == "closed"
        assert data.failure_count == 0

    def test_properties(self):
        from baldur.interfaces.repositories import CircuitBreakerStateData

        closed = CircuitBreakerStateData(service_name="svc", state="closed")
        assert closed.is_closed is True
        assert closed.is_open is False
        assert closed.is_half_open is False

        opened = CircuitBreakerStateData(service_name="svc", state="open")
        assert opened.is_open is True
        assert opened.is_closed is False
