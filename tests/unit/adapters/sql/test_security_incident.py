"""
Unit tests for SQLSecurityIncidentRepository.

Coverage:
- create / get_by_id round-trip preserves JSON DTO fields.
- get_open_incidents / get_by_type / get_by_severity indexed lookups.
- get_recent_by_ip time-window filtering.
- count_by_type_since aggregation.
- update_status / mark_as_resolved status mutations.
- Edge cases: missing IDs return None/False.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from baldur.adapters.sql.security_incident import SQLSecurityIncidentRepository
from baldur.interfaces.repositories import SecurityIncidentStatus
from tests.factories.time_helpers import freeze_time


@pytest.fixture
def repo(get_sqlite_conn) -> SQLSecurityIncidentRepository:
    return SQLSecurityIncidentRepository(get_sqlite_conn)


class TestSQLSecurityIncidentCrudBehavior:
    """create / get_by_id round-trip + basic queries."""

    def test_create_returns_open_incident_with_generated_id(self, repo):
        entry = repo.create(incident_type="rate_limit_abuse", severity="high")
        assert entry.id > 0
        assert entry.status == SecurityIncidentStatus.OPEN.value
        assert entry.created_at is not None
        assert entry.updated_at is not None

    def test_get_by_id_round_trips_all_dto_fields(self, repo):
        created = repo.create(
            incident_type="injection_attempt",
            severity="critical",
            description="SQL injection detected",
            source_ip="10.0.0.1",
            user_agent="curl/7.68",
            user_id=42,
            entity_refs={"order": 1, "user": 42},
            raw_payload={"query": "SELECT *"},
        )
        fetched = repo.get_by_id(created.id)
        assert fetched is not None
        assert fetched.incident_type == "injection_attempt"
        assert fetched.severity == "critical"
        assert fetched.description == "SQL injection detected"
        assert fetched.source_ip == "10.0.0.1"
        assert fetched.user_agent == "curl/7.68"
        assert fetched.user_id == 42
        assert fetched.entity_refs == {"order": 1, "user": 42}
        assert fetched.raw_payload == {"query": "SELECT *"}

    def test_get_by_id_returns_none_for_missing(self, repo):
        assert repo.get_by_id(9999) is None

    def test_create_with_minimal_args_defaults_json_fields(self, repo):
        entry = repo.create(incident_type="suspicious_activity", severity="medium")
        fetched = repo.get_by_id(entry.id)
        assert fetched.user_agent == ""
        assert fetched.entity_refs == {}
        assert fetched.description == ""
        assert fetched.raw_payload == {}
        assert fetched.assigned_to_id is None
        assert fetched.investigation_notes == ""


class TestSQLSecurityIncidentQueryBehavior:
    """Filtered query methods use SQL indexes correctly."""

    def test_get_open_incidents_returns_only_open(self, repo):
        repo.create(incident_type="rate_limit_abuse", severity="high")
        repo.create(incident_type="injection_attempt", severity="critical")
        resolved = repo.create(incident_type="replay_attack", severity="medium")
        repo.mark_as_resolved(resolved.id)

        results = repo.get_open_incidents()
        assert len(results) == 2
        assert all(r.status == SecurityIncidentStatus.OPEN.value for r in results)

    def test_get_by_type_filters_correctly(self, repo):
        repo.create(incident_type="rate_limit_abuse", severity="high")
        repo.create(incident_type="rate_limit_abuse", severity="medium")
        repo.create(incident_type="injection_attempt", severity="critical")

        results = repo.get_by_type("rate_limit_abuse")
        assert len(results) == 2
        assert all(r.incident_type == "rate_limit_abuse" for r in results)

    def test_get_by_severity_filters_correctly(self, repo):
        repo.create(incident_type="rate_limit_abuse", severity="high")
        repo.create(incident_type="injection_attempt", severity="critical")
        repo.create(incident_type="replay_attack", severity="critical")

        results = repo.get_by_severity("critical")
        assert len(results) == 2
        assert all(r.severity == "critical" for r in results)

    def test_get_by_type_respects_limit(self, repo):
        for _ in range(5):
            repo.create(incident_type="rate_limit_abuse", severity="medium")

        results = repo.get_by_type("rate_limit_abuse", limit=3)
        assert len(results) == 3

    def test_get_open_incidents_ordered_by_created_at_desc(self, repo):
        with freeze_time("2026-04-14 10:00:00"):
            first = repo.create(incident_type="t1", severity="high")
        with freeze_time("2026-04-14 11:00:00"):
            second = repo.create(incident_type="t2", severity="high")

        results = repo.get_open_incidents()
        assert results[0].id == second.id
        assert results[1].id == first.id


class TestSQLSecurityIncidentIpQueryBehavior:
    """get_recent_by_ip time-window queries."""

    def test_get_recent_by_ip_returns_within_window(self, repo):
        with freeze_time("2026-04-14 08:00:00"):
            repo.create(
                incident_type="rate_limit_abuse",
                severity="high",
                source_ip="10.0.0.1",
            )
        with freeze_time("2026-04-14 09:00:00"):
            repo.create(
                incident_type="injection_attempt",
                severity="critical",
                source_ip="10.0.0.1",
            )
        with freeze_time("2026-04-14 09:30:00"):
            repo.create(
                incident_type="replay_attack",
                severity="medium",
                source_ip="10.0.0.2",
            )

        with freeze_time("2026-04-14 10:00:00"):
            results = repo.get_recent_by_ip("10.0.0.1", hours=24)

        assert len(results) == 2
        assert all(r.source_ip == "10.0.0.1" for r in results)

    def test_get_recent_by_ip_excludes_old_entries(self, repo):
        with freeze_time("2026-04-13 06:00:00"):
            repo.create(
                incident_type="rate_limit_abuse",
                severity="high",
                source_ip="10.0.0.1",
            )
        with freeze_time("2026-04-14 09:00:00"):
            repo.create(
                incident_type="injection_attempt",
                severity="critical",
                source_ip="10.0.0.1",
            )

        with freeze_time("2026-04-14 10:00:00"):
            results = repo.get_recent_by_ip("10.0.0.1", hours=4)

        assert len(results) == 1
        assert results[0].incident_type == "injection_attempt"


class TestSQLSecurityIncidentCountBehavior:
    """count_by_type_since aggregation."""

    def test_count_by_type_since_counts_within_range(self, repo):
        with freeze_time("2026-04-13 10:00:00"):
            repo.create(incident_type="rate_limit_abuse", severity="high")
        with freeze_time("2026-04-14 10:00:00"):
            repo.create(incident_type="rate_limit_abuse", severity="medium")
            repo.create(incident_type="injection_attempt", severity="critical")

        from datetime import datetime

        since = datetime(2026, 4, 14, 0, 0, 0, tzinfo=UTC)
        assert repo.count_by_type_since("rate_limit_abuse", since) == 1
        assert repo.count_by_type_since("injection_attempt", since) == 1
        assert repo.count_by_type_since("nonexistent", since) == 0


class TestSQLSecurityIncidentStatusMutationBehavior:
    """update_status / mark_as_resolved transitions."""

    def test_update_status_changes_status_and_updated_at(self, repo):
        with freeze_time("2026-04-14 10:00:00"):
            entry = repo.create(incident_type="rate_limit_abuse", severity="high")
            original_updated = entry.updated_at

        with freeze_time("2026-04-14 12:00:00"):
            ok = repo.update_status(
                entry.id,
                SecurityIncidentStatus.INVESTIGATING.value,
                investigation_notes="Checking logs",
                assigned_to_id=7,
            )

        assert ok is True
        fetched = repo.get_by_id(entry.id)
        assert fetched.status == SecurityIncidentStatus.INVESTIGATING.value
        assert fetched.investigation_notes == "Checking logs"
        assert fetched.assigned_to_id == 7
        assert fetched.updated_at >= original_updated

    def test_update_status_returns_false_for_missing(self, repo):
        assert repo.update_status(9999, "investigating") is False

    def test_mark_as_resolved_sets_resolved_at_and_status(self, repo):
        entry = repo.create(incident_type="rate_limit_abuse", severity="high")
        assert entry.resolved_at is None

        ok = repo.mark_as_resolved(entry.id, investigation_notes="False alarm")
        assert ok is True

        fetched = repo.get_by_id(entry.id)
        assert fetched.status == SecurityIncidentStatus.RESOLVED.value
        assert fetched.resolved_at is not None
        assert fetched.investigation_notes == "False alarm"

    def test_mark_as_resolved_returns_false_for_missing(self, repo):
        assert repo.mark_as_resolved(9999) is False

    def test_update_status_preserves_existing_json_fields(self, repo):
        entry = repo.create(
            incident_type="injection_attempt",
            severity="critical",
            description="SQL injection",
            raw_payload={"query": "DROP TABLE"},
            entity_refs={"user": 42},
        )
        repo.update_status(entry.id, SecurityIncidentStatus.INVESTIGATING.value)

        fetched = repo.get_by_id(entry.id)
        assert fetched.description == "SQL injection"
        assert fetched.raw_payload == {"query": "DROP TABLE"}
        assert fetched.entity_refs == {"user": 42}

    def test_mark_as_resolved_without_notes_preserves_existing_notes(self, repo):
        entry = repo.create(incident_type="rate_limit_abuse", severity="high")
        repo.update_status(
            entry.id,
            SecurityIncidentStatus.INVESTIGATING.value,
            investigation_notes="Initial notes",
        )
        repo.mark_as_resolved(entry.id)

        fetched = repo.get_by_id(entry.id)
        assert fetched.investigation_notes == "Initial notes"
