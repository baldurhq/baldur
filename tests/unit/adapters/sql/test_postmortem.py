"""
Unit tests for SQLPostmortemRepository.

Coverage:
- save / get_by_incident_id round-trip preserves JSON DTO fields.
- find() with SQL filters (start_date, end_date, min_duration).
- find() with service post-filter and safety guard logging.
- count() SQL vs post-filter paths.
- update_fields() column + JSON + deep-merge branches.
- Duplicate incident_id save returns False.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from baldur.adapters.sql.postmortem import (
    SQLPostmortemRepository,
)
from baldur.interfaces.repositories import PostmortemData


@pytest.fixture
def repo(get_sqlite_conn) -> SQLPostmortemRepository:
    return SQLPostmortemRepository(get_sqlite_conn)


def _make_postmortem(
    incident_id: str = "inc-001",
    *,
    started_at: datetime | None = None,
    resolved_at: datetime | None = None,
    duration_seconds: float = 120.0,
    affected_services: list[str] | None = None,
    source: str = "auto",
) -> PostmortemData:
    return PostmortemData(
        incident_id=incident_id,
        started_at=started_at or datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        resolved_at=resolved_at or datetime(2026, 4, 14, 10, 2, 0, tzinfo=UTC),
        duration_seconds=duration_seconds,
        affected_services=affected_services or ["api-gateway"],
        timeline=[{"time": "10:00", "event": "alert"}],
        auto_actions=[{"action": "restart", "target": "pod-1"}],
        recommendations=["Add retry logic"],
        system_snapshot={"cpu": 80, "memory": 70},
        source=source,
    )


class TestSQLPostmortemCrudBehavior:
    """save / get_by_incident_id round-trip."""

    def test_save_and_get_round_trips_all_fields(self, repo):
        pm = _make_postmortem("inc-001")
        assert repo.save(pm) is True

        fetched = repo.get_by_incident_id("inc-001")
        assert fetched is not None
        assert fetched.incident_id == "inc-001"
        assert fetched.duration_seconds == 120.0
        assert fetched.source == "auto"
        assert fetched.affected_services == ["api-gateway"]
        assert fetched.timeline == [{"time": "10:00", "event": "alert"}]
        assert fetched.auto_actions == [{"action": "restart", "target": "pod-1"}]
        assert fetched.recommendations == ["Add retry logic"]
        assert fetched.system_snapshot == {"cpu": 80, "memory": 70}

    def test_get_by_incident_id_returns_none_for_missing(self, repo):
        assert repo.get_by_incident_id("nonexistent") is None

    def test_duplicate_incident_id_save_returns_false(self, repo):
        pm1 = _make_postmortem("inc-dup")
        pm2 = _make_postmortem("inc-dup")
        assert repo.save(pm1) is True
        assert repo.save(pm2) is False

    def test_save_with_empty_json_fields(self, repo):
        pm = PostmortemData(
            incident_id="inc-empty",
            started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        )
        assert repo.save(pm) is True
        fetched = repo.get_by_incident_id("inc-empty")
        assert fetched.affected_services == []
        assert fetched.timeline == []
        assert fetched.auto_actions == []
        assert fetched.recommendations == []
        assert fetched.system_snapshot == {}


class TestSQLPostmortemFindBehavior:
    """find() with SQL-level and post-filter queries."""

    def test_find_by_date_range(self, repo):
        repo.save(
            _make_postmortem(
                "inc-01",
                started_at=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_postmortem(
                "inc-02",
                started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_postmortem(
                "inc-03",
                started_at=datetime(2026, 4, 18, 10, 0, 0, tzinfo=UTC),
            )
        )

        results = repo.find(
            start_date=datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 16, 0, 0, 0, tzinfo=UTC),
        )
        assert len(results) == 1
        assert results[0].incident_id == "inc-02"

    def test_find_by_min_duration(self, repo):
        repo.save(_make_postmortem("inc-short", duration_seconds=30.0))
        repo.save(_make_postmortem("inc-long", duration_seconds=300.0))

        results = repo.find(min_duration=60.0)
        assert len(results) == 1
        assert results[0].incident_id == "inc-long"

    def test_find_by_service_post_filter(self, repo):
        repo.save(
            _make_postmortem("inc-api", affected_services=["api-gateway", "auth"])
        )
        repo.save(_make_postmortem("inc-db", affected_services=["database"]))

        results = repo.find(service="api-gateway")
        assert len(results) == 1
        assert results[0].incident_id == "inc-api"

    def test_find_service_post_filter_respects_limit(self, repo):
        for i in range(10):
            repo.save(
                _make_postmortem(
                    f"inc-{i:03d}",
                    affected_services=["api-gateway"],
                    started_at=datetime(2026, 4, 14, 10, i, 0, tzinfo=UTC),
                )
            )

        results = repo.find(service="api-gateway", limit=3)
        assert len(results) == 3

    def test_find_returns_desc_by_started_at(self, repo):
        repo.save(
            _make_postmortem(
                "inc-old",
                started_at=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_postmortem(
                "inc-new",
                started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )

        results = repo.find()
        assert results[0].incident_id == "inc-new"
        assert results[1].incident_id == "inc-old"

    def test_find_no_filters_returns_all(self, repo):
        repo.save(_make_postmortem("inc-a"))
        repo.save(_make_postmortem("inc-b"))
        assert len(repo.find()) == 2


class TestSQLPostmortemCountBehavior:
    """count() SQL aggregation and post-filter paths."""

    def test_count_without_service_uses_sql(self, repo):
        repo.save(_make_postmortem("inc-a"))
        repo.save(_make_postmortem("inc-b"))
        assert repo.count() == 2

    def test_count_with_date_range(self, repo):
        repo.save(
            _make_postmortem(
                "inc-01",
                started_at=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_postmortem(
                "inc-02",
                started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )

        c = repo.count(
            start_date=datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC),
        )
        assert c == 1

    def test_count_with_service_uses_post_filter(self, repo):
        repo.save(_make_postmortem("inc-api", affected_services=["api-gateway"]))
        repo.save(_make_postmortem("inc-db", affected_services=["database"]))
        assert repo.count(service="api-gateway") == 1


class TestSQLPostmortemUpdateFieldsBehavior:
    """update_fields() column + JSON field updates."""

    def test_update_scalar_column_field(self, repo):
        repo.save(_make_postmortem("inc-001", duration_seconds=60.0))
        ok = repo.update_fields("inc-001", {"duration_seconds": 180.0})
        assert ok is True
        fetched = repo.get_by_incident_id("inc-001")
        assert fetched.duration_seconds == 180.0

    def test_update_json_list_field_replaces(self, repo):
        repo.save(_make_postmortem("inc-001", affected_services=["api-gateway"]))
        ok = repo.update_fields(
            "inc-001", {"affected_services": ["api-gateway", "auth"]}
        )
        assert ok is True
        fetched = repo.get_by_incident_id("inc-001")
        assert fetched.affected_services == ["api-gateway", "auth"]

    def test_update_json_dict_field_deep_merges(self, repo):
        repo.save(_make_postmortem("inc-001"))
        ok = repo.update_fields("inc-001", {"system_snapshot": {"disk": 50}})
        assert ok is True
        fetched = repo.get_by_incident_id("inc-001")
        assert fetched.system_snapshot["cpu"] == 80
        assert fetched.system_snapshot["disk"] == 50

    def test_update_fields_returns_false_for_missing(self, repo):
        assert repo.update_fields("nonexistent", {"source": "manual"}) is False

    def test_update_source_column(self, repo):
        repo.save(_make_postmortem("inc-001", source="auto"))
        repo.update_fields("inc-001", {"source": "manual"})
        fetched = repo.get_by_incident_id("inc-001")
        assert fetched.source == "manual"


class TestSQLPostmortemPostFilterSafetyBehavior:
    """Post-filter safety guards: multiplier and warning threshold."""

    def test_high_discard_rate_logs_warning(self, repo, monkeypatch):
        for i in range(20):
            repo.save(
                _make_postmortem(
                    f"inc-{i:03d}",
                    affected_services=["other-service"],
                    started_at=datetime(2026, 4, 14, 10, i, 0, tzinfo=UTC),
                )
            )
        repo.save(
            _make_postmortem(
                "inc-match",
                affected_services=["target-service"],
                started_at=datetime(2026, 4, 14, 10, 30, 0, tzinfo=UTC),
            )
        )

        logged_events: list[str] = []

        class _SpyLogger:
            def warning(self, event, **kw):
                logged_events.append(event)

            def __getattr__(self, name):
                return lambda *a, **kw: None

        from baldur.adapters.sql import postmortem as pm_mod

        monkeypatch.setattr(pm_mod, "logger", _SpyLogger())

        repo.find(service="target-service", limit=5)

        assert any("high_discard_rate" in e for e in logged_events)
