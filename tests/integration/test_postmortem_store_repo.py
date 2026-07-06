"""Postmortem Store + Repository Integration Tests

Verifies the end-to-end flow between store.py public API and
InMemoryPostmortemRepository without any infrastructure dependency.

Test Categories:
    A. Save + Retrieve: add_healing_incident -> repo.save -> get_incident_by_id
    B. List + Filter: repo.find through get_healing_incidents with DB path
    C. Count: repo.count through get_healing_incidents_count
    D. Update: update_incident_fields -> repo.update_fields

Note: All tests use in-memory mock repositories - no DB dependency.
      This enables parallel test execution with pytest-xdist.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import patch

from baldur.adapters.memory.postmortem import InMemoryPostmortemRepository
from baldur_pro.services.postmortem.store import (
    add_healing_incident,
    clear_healing_incidents,
    get_healing_incidents,
    get_healing_incidents_count,
    get_incident_by_id,
    set_db_persistence_enabled,
    update_incident_fields,
)

# =============================================================================
# A. Save + Retrieve Integration
# =============================================================================


class TestPostmortemSaveRetrieveIntegration:
    """store.py add_healing_incident -> InMemoryPostmortemRepository -> get_incident_by_id.

    Validates:
    - add_healing_incident persists to both cache and repository
    - get_incident_by_id retrieves from repository when cache misses
    """

    def setup_method(self):
        """Set up fresh repository and enable DB persistence."""
        self.repo = InMemoryPostmortemRepository()
        clear_healing_incidents()

    def teardown_method(self):
        """Restore defaults."""
        set_db_persistence_enabled(False)
        clear_healing_incidents()

    @patch(
        "baldur_pro.services.postmortem.store._get_postmortem_repo",
        autospec=True,
    )
    def test_add_incident_persists_to_repo(self, mock_get_repo):
        """add_healing_incident saves to both cache and repository.

        Purpose:
            Verify end-to-end save flow from public API to repository.
        Expected:
            - Incident stored in in-memory cache
            - Incident stored in repository via PostmortemData.from_incident_dict
        """
        mock_get_repo.return_value = self.repo
        set_db_persistence_enabled(True)

        add_healing_incident(
            {
                "incident_id": "e2e-001",
                "affected_services": ["payment"],
                "duration_seconds": 120,
            }
        )

        # Verify cache
        cached = get_incident_by_id("e2e-001", use_db=False)
        assert cached is not None
        assert cached["incident_id"] == "e2e-001"

        # Verify repository
        repo_data = self.repo.get_by_incident_id("e2e-001")
        assert repo_data is not None
        assert repo_data.incident_id == "e2e-001"
        assert repo_data.duration_seconds == 120

    @patch(
        "baldur_pro.services.postmortem.store._get_postmortem_repo",
        autospec=True,
    )
    def test_get_by_id_falls_back_to_repo_when_cache_empty(self, mock_get_repo):
        """get_incident_by_id retrieves from repo when cache is cleared.

        Purpose:
            Verify the DB fallback path works end-to-end.
        Expected:
            - After clearing cache, get_incident_by_id still returns data from repo
        """
        mock_get_repo.return_value = self.repo
        set_db_persistence_enabled(True)

        add_healing_incident({"incident_id": "e2e-002"})
        clear_healing_incidents()  # Clear cache only

        result = get_incident_by_id("e2e-002", use_db=True)
        assert result is not None
        assert result["incident_id"] == "e2e-002"


# =============================================================================
# B. List + Filter Integration
# =============================================================================


class TestPostmortemListFilterIntegration:
    """store.py get_healing_incidents -> InMemoryPostmortemRepository.find.

    Validates:
    - get_healing_incidents delegates to repo.find when filters are present
    - Filter parameters (start_date, service, min_duration) are correctly forwarded
    """

    def setup_method(self):
        """Set up fresh repository."""
        self.repo = InMemoryPostmortemRepository()
        clear_healing_incidents()

    def teardown_method(self):
        """Restore defaults."""
        set_db_persistence_enabled(False)
        clear_healing_incidents()

    @patch(
        "baldur_pro.services.postmortem.store._get_postmortem_repo",
        autospec=True,
    )
    def test_filters_route_through_repo(self, mock_get_repo):
        """Filtered queries go through repo.find and return dicts.

        Purpose:
            Verify that applying filters triggers the DB path through repository.
        Expected:
            - Only matching incidents are returned
            - Results are dict format (not PostmortemData)
        """
        mock_get_repo.return_value = self.repo
        set_db_persistence_enabled(True)

        # Save two incidents with different services
        add_healing_incident(
            {
                "incident_id": "filter-001",
                "affected_services": ["svc-a"],
                "started_at": "2026-01-15T10:00:00Z",
                "duration_seconds": 60,
            }
        )
        add_healing_incident(
            {
                "incident_id": "filter-002",
                "affected_services": ["svc-b"],
                "started_at": "2026-02-15T10:00:00Z",
                "duration_seconds": 600,
            }
        )

        # Filter by service
        results = get_healing_incidents(limit=10, service="svc-a")
        assert len(results) == 1
        assert results[0]["incident_id"] == "filter-001"


# =============================================================================
# C. Count Integration
# =============================================================================


class TestPostmortemCountIntegration:
    """store.py get_healing_incidents_count -> InMemoryPostmortemRepository.count.

    Validates:
    - Filtered count queries go through repo.count
    """

    def setup_method(self):
        """Set up fresh repository."""
        self.repo = InMemoryPostmortemRepository()
        clear_healing_incidents()

    def teardown_method(self):
        """Restore defaults."""
        set_db_persistence_enabled(False)
        clear_healing_incidents()

    @patch(
        "baldur_pro.services.postmortem.store._get_postmortem_repo",
        autospec=True,
    )
    def test_filtered_count_goes_through_repo(self, mock_get_repo):
        """Filtered count queries use repo.count.

        Purpose:
            Verify that count with filters delegates to repository.
        Expected:
            - Count matches number of filter-matching records in repo
        """
        mock_get_repo.return_value = self.repo
        set_db_persistence_enabled(True)

        add_healing_incident(
            {
                "incident_id": "cnt-001",
                "started_at": "2026-01-15T10:00:00Z",
                "duration_seconds": 60,
            }
        )
        add_healing_incident(
            {
                "incident_id": "cnt-002",
                "started_at": "2026-02-15T10:00:00Z",
                "duration_seconds": 600,
            }
        )

        count = get_healing_incidents_count(min_duration=300.0)
        assert count == 1


# =============================================================================
# D. Update Integration
# =============================================================================


class TestPostmortemUpdateIntegration:
    """store.py update_incident_fields -> InMemoryPostmortemRepository.update_fields.

    Validates:
    - Field updates propagate to both cache and repository
    """

    def setup_method(self):
        """Set up fresh repository."""
        self.repo = InMemoryPostmortemRepository()
        clear_healing_incidents()

    def teardown_method(self):
        """Restore defaults."""
        set_db_persistence_enabled(False)
        clear_healing_incidents()

    @patch(
        "baldur_pro.services.postmortem.store._get_postmortem_repo",
        autospec=True,
    )
    def test_update_propagates_to_cache_and_repo(self, mock_get_repo):
        """update_incident_fields updates both in-memory cache and repository.

        Purpose:
            Verify that update_incident_fields writes to both storage layers.
        Expected:
            - Cache dict is updated
            - Repository data is updated via repo.update_fields
        """
        mock_get_repo.return_value = self.repo
        set_db_persistence_enabled(True)

        add_healing_incident(
            {
                "incident_id": "upd-001",
                "source": "auto",
            }
        )

        update_incident_fields("upd-001", {"source": "manual"})

        # Verify cache
        cached = get_incident_by_id("upd-001", use_db=False)
        assert cached["source"] == "manual"

        # Verify repo
        repo_data = self.repo.get_by_incident_id("upd-001")
        assert repo_data is not None
        assert repo_data.source == "manual"
