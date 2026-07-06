"""
통합 시나리오 테스트.
"""

from datetime import UTC, datetime


class TestIntegrationScenarios:
    """Integration tests for complete workflows."""

    def test_dlq_workflow(self):
        """Test complete DLQ workflow using in-memory repositories."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.interfaces.repositories import FailedOperationStatus

        repo = InMemoryFailedOperationRepository()

        # 1. Create failed operation
        entry = repo.create(
            domain="payment",
            failure_type="gateway_timeout",
            error_message="External API gateway timeout",
            entity_type="order",
            entity_id="12345",
            entity_refs={"order_id": 12345},
            max_retries=3,
        )

        assert entry.status == FailedOperationStatus.PENDING.value
        assert entry.retry_count == 0

        # 2. Retry and increment count
        repo.increment_retry_count(entry.id)
        repo.increment_retry_count(entry.id)

        updated = repo.get_by_id(entry.id)
        assert updated.retry_count == 2

        # 3. Resolve after successful retry
        repo.update_status(
            entry.id,
            FailedOperationStatus.RESOLVED.value,
            resolution_type="auto_retry",
            resolution_note="Succeeded on 2nd retry",
        )

        final = repo.get_by_id(entry.id)
        assert final.status == FailedOperationStatus.RESOLVED.value
        assert final.resolved_at is not None

    def test_circuit_breaker_workflow(self):
        """Test complete circuit breaker workflow."""
        from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository
        from baldur.interfaces.repositories import CircuitBreakerStateEnum

        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "toss_payment_api"

        # 1. Get or create (starts CLOSED)
        state = repo.get_or_create(service_name)
        assert state.state == CircuitBreakerStateEnum.CLOSED.value

        # 2. Record failures
        for _ in range(5):
            repo.increment_failure_count(service_name)

        state = repo.get_by_service_name(service_name)
        assert state.failure_count == 5

        # 3. Open the circuit
        repo.update_state(
            service_name,
            CircuitBreakerStateEnum.OPEN.value,
            failure_count=5,
            opened_at=datetime.now(UTC),
        )

        state = repo.get_by_service_name(service_name)
        assert state.state == CircuitBreakerStateEnum.OPEN.value
        assert state.opened_at is not None

        # 4. Half-open after timeout
        repo.update_state(service_name, CircuitBreakerStateEnum.HALF_OPEN.value)

        state = repo.get_by_service_name(service_name)
        assert state.state == CircuitBreakerStateEnum.HALF_OPEN.value

        # 5. Reset on success
        repo.reset_counts(service_name)
        repo.update_state(service_name, CircuitBreakerStateEnum.CLOSED.value)

        state = repo.get_by_service_name(service_name)
        assert state.state == CircuitBreakerStateEnum.CLOSED.value
        assert state.failure_count == 0

    def test_security_incident_workflow(self):
        """Test complete security incident workflow."""
        from baldur.adapters.memory import InMemorySecurityIncidentRepository
        from baldur.interfaces.repositories import SecurityIncidentStatus

        repo = InMemorySecurityIncidentRepository()

        # 1. Detect and record incident
        incident = repo.create(
            incident_type="webhook_signature_invalid",
            severity="critical",
            description="Invalid HMAC signature detected",
            source_ip="192.168.1.100",
            raw_payload={"invalid": "payload"},
        )

        assert incident.status == SecurityIncidentStatus.OPEN.value

        # 2. Start investigation
        repo.update_status(
            incident.id,
            SecurityIncidentStatus.INVESTIGATING.value,
            investigation_notes="Reviewing request logs",
            assigned_to_id=1,
        )

        updated = repo.get_by_id(incident.id)
        assert updated.status == SecurityIncidentStatus.INVESTIGATING.value

        # 3. Check for related incidents from same IP
        related = repo.find_by_source_ip("192.168.1.100")
        assert len(related) >= 1

        # 4. Resolve incident
        repo.mark_as_resolved(incident.id, "False alarm - clock skew on webhook server")

        final = repo.get_by_id(incident.id)
        assert final.status == SecurityIncidentStatus.RESOLVED.value
        assert final.resolved_at is not None
