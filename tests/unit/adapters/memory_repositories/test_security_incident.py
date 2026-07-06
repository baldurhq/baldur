"""
InMemorySecurityIncidentRepository 테스트.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest


class TestInMemorySecurityIncidentRepository:
    """Tests for InMemorySecurityIncidentRepository."""

    @pytest.fixture
    def repo(self):
        """Create a fresh repository for each test."""
        from baldur.adapters.memory import InMemorySecurityIncidentRepository

        return InMemorySecurityIncidentRepository()

    def test_create_incident(self, repo):
        """Test creating a new security incident."""
        from baldur.interfaces.repositories import SecurityIncidentStatus

        incident = repo.create(
            incident_type="webhook_signature_invalid",
            severity="high",
            description="Webhook signature validation failed",
            source_ip="192.168.1.100",
            user_agent="curl/7.68.0",
            user_id=42,
            raw_payload={"key": "value"},
        )

        assert incident.id == 1
        assert incident.incident_type == "webhook_signature_invalid"
        assert incident.severity == "high"
        assert incident.status == SecurityIncidentStatus.OPEN.value
        assert incident.description == "Webhook signature validation failed"
        assert incident.source_ip == "192.168.1.100"
        assert incident.user_id == 42
        assert incident.created_at is not None

    def test_get_by_id(self, repo):
        """Test retrieving an incident by ID."""
        created = repo.create(
            incident_type="unauthorized_access",
            severity="critical",
            description="Admin panel access attempt",
        )

        retrieved = repo.get_by_id(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.incident_type == "unauthorized_access"

    def test_get_by_id_not_found(self, repo):
        """Test retrieving a non-existent incident."""
        result = repo.get_by_id(99999)
        assert result is None

    def test_update_status(self, repo):
        """Test updating incident status."""
        from baldur.interfaces.repositories import SecurityIncidentStatus

        incident = repo.create(
            incident_type="rate_limit_abuse",
            severity="medium",
            description="Excessive API calls",
        )

        result = repo.update_status(
            incident.id,
            SecurityIncidentStatus.INVESTIGATING.value,
            investigation_notes="Looking into the issue",
            assigned_to_id=1,
        )

        assert result is True

        updated = repo.get_by_id(incident.id)
        assert updated.status == SecurityIncidentStatus.INVESTIGATING.value
        assert updated.investigation_notes == "Looking into the issue"
        assert updated.assigned_to_id == 1

    def test_find_by_type(self, repo):
        """Test finding incidents by type."""
        repo.create(incident_type="type_a", severity="high", description="Incident 1")
        repo.create(incident_type="type_a", severity="medium", description="Incident 2")
        repo.create(incident_type="type_b", severity="low", description="Incident 3")

        results = repo.find_by_type("type_a")
        assert len(results) == 2

        results = repo.find_by_type("type_b")
        assert len(results) == 1

    def test_find_by_source_ip(self, repo):
        """Test finding incidents by source IP."""
        repo.create(
            incident_type="test",
            severity="high",
            description="Test 1",
            source_ip="10.0.0.1",
        )
        repo.create(
            incident_type="test",
            severity="high",
            description="Test 2",
            source_ip="10.0.0.1",
        )
        repo.create(
            incident_type="test",
            severity="high",
            description="Test 3",
            source_ip="10.0.0.2",
        )

        results = repo.find_by_source_ip("10.0.0.1")
        assert len(results) == 2

    def test_count_by_source_ip(self, repo):
        """Test counting incidents by source IP."""
        now = datetime.now(UTC)
        repo.create(
            incident_type="test",
            severity="high",
            description="Test",
            source_ip="192.168.1.1",
        )
        repo.create(
            incident_type="test",
            severity="high",
            description="Test",
            source_ip="192.168.1.1",
        )

        count = repo.count_by_source_ip("192.168.1.1", since=now - timedelta(hours=1))
        assert count == 2

    def test_get_open_incidents(self, repo):
        """Test getting all open incidents."""
        from baldur.interfaces.repositories import SecurityIncidentStatus

        incident1 = repo.create(
            incident_type="test", severity="high", description="Open incident"
        )
        incident2 = repo.create(
            incident_type="test", severity="high", description="Closed incident"
        )
        repo.update_status(incident2.id, SecurityIncidentStatus.RESOLVED.value)

        open_incidents = repo.get_open_incidents()
        assert len(open_incidents) == 1
        assert open_incidents[0].id == incident1.id

    def test_mark_as_resolved(self, repo):
        """Test marking an incident as resolved."""
        from baldur.interfaces.repositories import SecurityIncidentStatus

        incident = repo.create(
            incident_type="test", severity="high", description="Test incident"
        )

        result = repo.mark_as_resolved(incident.id, investigation_notes="Issue fixed")
        assert result is True

        updated = repo.get_by_id(incident.id)
        assert updated.status == SecurityIncidentStatus.RESOLVED.value
        assert updated.investigation_notes == "Issue fixed"
        assert updated.resolved_at is not None

    def test_get_by_severity(self, repo):
        """Test getting incidents by severity."""
        repo.create(incident_type="test", severity="critical", description="Test 1")
        repo.create(incident_type="test", severity="high", description="Test 2")
        repo.create(incident_type="test", severity="critical", description="Test 3")

        critical = repo.get_by_severity("critical")
        assert len(critical) == 2

        high = repo.get_by_severity("high")
        assert len(high) == 1

    def test_thread_safety(self, repo):
        """Test thread safety with concurrent operations."""
        results = []
        errors = []

        def create_incident(n):
            try:
                incident = repo.create(
                    incident_type=f"type_{n}",
                    severity="high",
                    description=f"Incident {n}",
                )
                results.append(incident.id)
            except Exception as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(create_incident, range(50))

        assert len(errors) == 0
        assert len(set(results)) == 50
