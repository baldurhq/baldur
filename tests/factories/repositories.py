"""
In-Memory Repositories for Testing.

In-memory repository implementations for tests, so a test can exercise
repository-backed code without a real DB/Redis connection.

Consolidates the MockRepository classes that used to be redefined in each
individual test file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from tests.factories.constants import DefaultValues
from tests.factories.data_factory import MockCircuitBreakerStateData


def _resolve_expiry(ttl_minutes: int | None) -> datetime | None:
    """Mirror the repository contract: None or <= 0 stores no expiry."""
    if ttl_minutes is None or ttl_minutes <= 0:
        return None
    return datetime.now(UTC) + timedelta(minutes=ttl_minutes)


class InMemoryCircuitBreakerRepository:
    """
    In-memory repository for Circuit Breaker state.

    Implements the CircuitBreakerStateRepository interface, so CB state
    management can be tested without a real DB/Redis.

    Usage:
        repo = InMemoryCircuitBreakerRepository()

        # Read/create state
        state = repo.get_or_create("payment-api")
        assert state.state == "closed"

        # Force open
        success, prev, new = repo.atomic_force_open(
            "payment-api",
            reason="Maintenance",
            controlled_by_id=1,
            ttl_minutes=30
        )

        # Verify state
        state = repo.get_or_create("payment-api")
        assert state.state == "open"
    """

    def __init__(self):
        self._states: dict[str, MockCircuitBreakerStateData] = {}
        # Controls whether atomic operations report success
        self._atomic_success: bool = True

    def set_atomic_success(self, success: bool) -> None:
        """Set whether atomic operations report success (test control)."""
        self._atomic_success = success

    def get_or_create(self, service_name: str) -> MockCircuitBreakerStateData:
        """
        Read a service's CB state, creating it when absent.

        Args:
            service_name: Service name

        Returns:
            MockCircuitBreakerStateData instance
        """
        if service_name not in self._states:
            self._states[service_name] = MockCircuitBreakerStateData(
                service_name=service_name
            )
        return self._states[service_name]

    def get(self, service_name: str) -> MockCircuitBreakerStateData | None:
        """
        Read a service's CB state (None when absent).

        Args:
            service_name: Service name

        Returns:
            MockCircuitBreakerStateData or None
        """
        return self._states.get(service_name)

    def atomic_force_open(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
        ttl_minutes: int | None = None,
    ) -> tuple[bool, str | None, str | None]:
        """
        Atomically force the circuit breaker open.

        Args:
            service_name: Service name
            reason: Reason for opening
            controlled_by_id: Controlling user ID
            ttl_minutes: Manual-override lifetime in minutes; None or <= 0
                stores no expiry

        Returns:
            (success, previous_state, new_state) tuple
        """
        if not self._atomic_success:
            return (False, None, None)

        state = self.get_or_create(service_name)
        previous_state = state.state
        state.state = DefaultValues.CB_STATE_OPEN
        state.opened_at = datetime.now(UTC)
        state.opened_by_id = controlled_by_id
        state.opened_reason = reason
        state.manually_controlled = True
        state.controlled_by_id = controlled_by_id
        state.control_reason = reason
        state.manual_override_expires_at = _resolve_expiry(ttl_minutes)

        return (True, previous_state, DefaultValues.CB_STATE_OPEN)

    def atomic_force_close(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
        ttl_minutes: int | None = None,
    ) -> tuple[bool, str | None, str | None]:
        """
        Atomically force the circuit breaker closed.

        Args:
            service_name: Service name
            reason: Reason for closing
            controlled_by_id: Controlling user ID
            ttl_minutes: Manual-override lifetime in minutes; None or <= 0
                stores no expiry

        Returns:
            (success, previous_state, new_state) tuple
        """
        if not self._atomic_success:
            return (False, None, None)

        state = self.get_or_create(service_name)
        previous_state = state.state
        state.state = DefaultValues.CB_STATE_CLOSED
        state.manually_controlled = False
        state.controlled_by_id = controlled_by_id
        state.control_reason = reason
        state.manual_override_expires_at = _resolve_expiry(ttl_minutes)

        return (True, previous_state, DefaultValues.CB_STATE_CLOSED)

    def atomic_reset(self, service_name: str) -> bool:
        """
        Atomically reset the circuit breaker.

        Args:
            service_name: Service name

        Returns:
            Whether the reset succeeded
        """
        state = self.get_or_create(service_name)
        state.state = DefaultValues.CB_STATE_CLOSED
        state.failure_count = 0
        state.success_count = 0
        state.opened_at = None
        state.opened_by_id = None
        state.opened_reason = ""
        state.manually_controlled = False
        state.controlled_by_id = None
        state.control_reason = ""
        return True

    def update_failure_count(
        self,
        service_name: str,
        increment: int = 1,
    ) -> int:
        """
        Update the failure count.

        Args:
            service_name: Service name
            increment: Amount to add

        Returns:
            New failure count
        """
        state = self.get_or_create(service_name)
        state.failure_count += increment
        state.last_failure_at = datetime.now(UTC)
        return state.failure_count

    def update_success_count(
        self,
        service_name: str,
        increment: int = 1,
    ) -> int:
        """
        Update the success count.

        Args:
            service_name: Service name
            increment: Amount to add

        Returns:
            New success count
        """
        state = self.get_or_create(service_name)
        state.success_count += increment
        state.last_success_at = datetime.now(UTC)
        return state.success_count

    def list_all(self) -> list[MockCircuitBreakerStateData]:
        """List every CB state."""
        return list(self._states.values())

    def clear(self) -> None:
        """Clear every state."""
        self._states.clear()

    def reset_half_open_count(self, service_name: str) -> None:
        """476 G8: clear HALF_OPEN counter on a service."""
        state = self._states.get(service_name)
        if state is None:
            return
        if hasattr(state, "half_open_request_count"):
            state.half_open_request_count = 0
        if hasattr(state, "half_open_window_started_at"):
            state.half_open_window_started_at = None

    def try_acquire_half_open_slot(
        self, service_name: str, limit: int, stuck_timeout_seconds: int
    ) -> tuple[bool, str, str]:
        """476 D2: minimal RLock-free state-machine for tests."""
        state = self.get_or_create(service_name)
        current_state = state.state
        count = getattr(state, "half_open_request_count", 0) or 0

        if current_state == DefaultValues.CB_STATE_OPEN:
            state.state = "half_open"
            if hasattr(state, "success_count"):
                state.success_count = 0
            if hasattr(state, "half_open_request_count"):
                state.half_open_request_count = 1
            return (True, DefaultValues.CB_STATE_OPEN, "half_open")

        if current_state == "half_open" and count < limit:
            if hasattr(state, "half_open_request_count"):
                state.half_open_request_count = count + 1
            return (True, "half_open", "half_open")

        if current_state == "half_open":
            return (False, "half_open", "half_open")

        return (False, current_state, current_state)


class InMemoryRateLimitTracker:
    """
    In-memory implementation of the Rate Limit Tracker.

    Replaces MockRateLimitTracker from test_protection.py.

    Usage:
        tracker = InMemoryRateLimitTracker()

        tracker.record_rate_limit("payment-api")
        tracker.record_request("payment-api")

        count = tracker.get_rate_limit_count("payment-api", window_seconds=60)
    """

    def __init__(self):
        self._rate_limits: dict[str, int] = {}
        self._requests: dict[str, int] = {}
        self._backoff: dict[str, int] = {}

    def record_rate_limit(self, service_name: str) -> None:
        """Record a rate-limited response."""
        self._rate_limits.setdefault(service_name, 0)
        self._rate_limits[service_name] += 1

    def record_request(self, service_name: str) -> None:
        """Record a request."""
        self._requests.setdefault(service_name, 0)
        self._requests[service_name] += 1

    def get_rate_limit_count(self, service_name: str, window_seconds: int) -> int:
        """Read the rate-limit count within the given window."""
        return self._rate_limits.get(service_name, 0)

    def get_request_count(self, service_name: str, window_seconds: int) -> int:
        """Read the request count within the given window."""
        return self._requests.get(service_name, 0)

    def get_backoff_level(self, service_name: str) -> int:
        """Read the current backoff level."""
        return self._backoff.get(service_name, 0)

    def increment_backoff(self, service_name: str) -> int:
        """Increase the backoff level."""
        self._backoff.setdefault(service_name, 0)
        self._backoff[service_name] += 1
        return self._backoff[service_name]

    def reset_backoff(self, service_name: str) -> None:
        """Reset the backoff level."""
        self._backoff[service_name] = 0

    def clear(self) -> None:
        """Clear every tracked value."""
        self._rate_limits.clear()
        self._requests.clear()
        self._backoff.clear()


@dataclass
class MockDLQEntry:
    """Mock data for a DLQ entry."""

    id: int
    domain: str
    failure_type: str
    status: str
    retry_count: int = 0
    max_retries: int = 3
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    error_code: str = "TIMEOUT"
    error_message: str = "Connection timed out"
    snapshot_data: dict[str, Any] = field(
        default_factory=lambda: {"order_id": "order-123"}
    )
    request_data: dict[str, Any] = field(default_factory=lambda: {"method": "POST"})
    response_data: dict[str, Any] = field(default_factory=lambda: {"status_code": 500})
    metadata: dict[str, Any] = field(default_factory=dict)


class InMemoryDLQRepository:
    """
    In-memory repository for the DLQ (Dead Letter Queue).

    Implements the FailedOperationRepository interface.

    Usage:
        repo = InMemoryDLQRepository()

        # Add an entry
        entry = repo.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
            error_message="Connection timed out"
        )

        # Read it back
        entry = repo.get_by_id(1)

        # Increment the retry count
        repo.increment_retry_count(1)
    """

    def __init__(self):
        self._entries: dict[int, MockDLQEntry] = {}
        self._next_id = 1

    def create(
        self,
        domain: str = DefaultValues.DOMAIN_PAYMENT,
        failure_type: str = DefaultValues.FAILURE_PG_TIMEOUT,
        status: str = DefaultValues.STATUS_PENDING,
        error_message: str = "Connection timed out",
        **kwargs,
    ) -> MockDLQEntry:
        """
        Create a new DLQ entry.

        Args:
            domain: Business domain
            failure_type: Failure type
            status: Entry status
            error_message: Error message
            **kwargs: Additional fields

        Returns:
            The created MockDLQEntry
        """
        entry = MockDLQEntry(
            id=self._next_id,
            domain=domain,
            failure_type=failure_type,
            status=status,
            error_message=error_message,
            **kwargs,
        )
        self._entries[self._next_id] = entry
        self._next_id += 1
        return entry

    def get_by_id(self, pk: int) -> MockDLQEntry | None:
        """Read an entry by ID."""
        return self._entries.get(pk)

    def increment_retry_count(self, pk: int) -> bool:
        """Increment the retry count."""
        entry = self._entries.get(pk)
        if entry is None:
            return False
        entry.retry_count += 1
        return True

    def update_status(self, pk: int, status: str) -> bool:
        """Update the entry status."""
        entry = self._entries.get(pk)
        if entry is None:
            return False
        entry.status = status
        if status == "resolved":
            entry.resolved_at = datetime.now(UTC)
        return True

    def list_pending(
        self,
        domain: str | None = None,
        limit: int = 100,
    ) -> list[MockDLQEntry]:
        """
        List entries in the pending state.

        Args:
            domain: Domain to filter by (None means all)
            limit: Maximum number of entries

        Returns:
            List of MockDLQEntry
        """
        entries = [
            e
            for e in self._entries.values()
            if e.status == DefaultValues.STATUS_PENDING
            and (domain is None or e.domain == domain)
        ]
        return entries[:limit]

    def list_all(self) -> list[MockDLQEntry]:
        """List every entry."""
        return list(self._entries.values())

    def delete(self, pk: int) -> bool:
        """Delete an entry."""
        if pk in self._entries:
            del self._entries[pk]
            return True
        return False

    def clear(self) -> None:
        """Clear every entry."""
        self._entries.clear()
        self._next_id = 1
