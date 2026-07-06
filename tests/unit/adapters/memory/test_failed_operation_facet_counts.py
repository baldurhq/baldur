"""Unit tests for InMemoryFailedOperationRepository.get_facet_counts (542 D3).

Standard faceted-search semantics: ``by_status`` is scoped by ``domain`` and
``by_domain`` is scoped by ``status`` — each facet excludes its own selection
so the dimension being chosen keeps every option. Zero-count buckets MUST be
dropped explicitly because ``_remove_from_index`` (failed_operation.py:121-129)
uses ``set.discard`` without deleting an emptied key, so a fully-drained
status/domain lingers as an empty set in the 1D/2D indexes and would otherwise
surface as ``:0``, breaking parity with SQL (``GROUP BY``) and Redis
(``ZCARD`` + ``if count:``).

Coverage axes (Test Assessment 542):
- parametrize 4 filter shapes: (None, None), (status, None), (None, domain),
  (status, domain)
- zero-drop on a drained status (transitioned every entry away)
- zero-drop on a drained domain (deleted every entry)
"""

from __future__ import annotations

import pytest

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationStatus


@pytest.fixture
def repo() -> InMemoryFailedOperationRepository:
    return InMemoryFailedOperationRepository()


def _seed(repo, *, domain: str, status: str) -> str:
    """Create one entry and (if needed) move it to ``status``; return id."""
    entry = repo.create(domain=domain, failure_type="timeout")
    if status != FailedOperationStatus.PENDING.value:
        repo.update_status(entry.id, status)
    return entry.id


# Statuses used throughout. PENDING and RESOLVED both have memory-adapter
# indexes; using two distinct statuses lets the by_status scope assertions
# distinguish hits across statuses.
_PENDING = FailedOperationStatus.PENDING.value
_RESOLVED = FailedOperationStatus.RESOLVED.value


class TestInMemoryFacetCountsBehavior:
    """get_facet_counts across the four filter shapes + zero-drop parity."""

    def test_unfiltered_returns_complete_status_and_domain_counts(self, repo):
        """No filter → by_status spans all statuses, by_domain spans all domains."""
        _seed(repo, domain="payment", status=_PENDING)
        _seed(repo, domain="payment", status=_PENDING)
        _seed(repo, domain="payment", status=_RESOLVED)
        _seed(repo, domain="inventory", status=_PENDING)

        result = repo.get_facet_counts()

        assert result == {
            "by_status": {_PENDING: 3, _RESOLVED: 1},
            "by_domain": {"payment": 3, "inventory": 1},
        }

    def test_domain_scope_narrows_by_status_to_that_domain(self, repo):
        """domain=X scopes by_status to entries within domain X (D2)."""
        _seed(repo, domain="payment", status=_PENDING)
        _seed(repo, domain="payment", status=_RESOLVED)
        _seed(repo, domain="inventory", status=_PENDING)
        _seed(repo, domain="inventory", status=_PENDING)

        result = repo.get_facet_counts(domain="payment")

        # by_status reflects only the payment domain.
        assert result["by_status"] == {_PENDING: 1, _RESOLVED: 1}
        # by_domain keeps every domain (its own axis is unscoped — D2).
        assert result["by_domain"] == {"payment": 2, "inventory": 2}

    def test_status_scope_narrows_by_domain_to_that_status(self, repo):
        """status=Y scopes by_domain to entries within status Y (D2)."""
        _seed(repo, domain="payment", status=_PENDING)
        _seed(repo, domain="inventory", status=_PENDING)
        _seed(repo, domain="payment", status=_RESOLVED)
        _seed(repo, domain="inventory", status=_RESOLVED)
        _seed(repo, domain="inventory", status=_RESOLVED)

        result = repo.get_facet_counts(status=_RESOLVED)

        # by_domain reflects only RESOLVED entries.
        assert result["by_domain"] == {"payment": 1, "inventory": 2}
        # by_status keeps every status (its own axis is unscoped — D2).
        assert result["by_status"] == {_PENDING: 2, _RESOLVED: 3}

    def test_both_filters_scope_both_axes(self, repo):
        """When both filters are set, each axis scopes by the OTHER (D2)."""
        _seed(repo, domain="payment", status=_PENDING)
        _seed(repo, domain="payment", status=_RESOLVED)
        _seed(repo, domain="inventory", status=_PENDING)
        _seed(repo, domain="inventory", status=_RESOLVED)
        _seed(repo, domain="inventory", status=_RESOLVED)

        result = repo.get_facet_counts(status=_RESOLVED, domain="payment")

        # by_status is scoped by domain="payment" (not by status — D2).
        assert result["by_status"] == {_PENDING: 1, _RESOLVED: 1}
        # by_domain is scoped by status=RESOLVED (not by domain — D2).
        assert result["by_domain"] == {"payment": 1, "inventory": 2}

    def test_empty_repository_returns_empty_maps(self, repo):
        """Boundary: no entries → both maps empty (no zero-keys)."""
        assert repo.get_facet_counts() == {"by_status": {}, "by_domain": {}}

    def test_drained_status_does_not_surface_as_zero(self, repo):
        """Zero-drop on a status whose every entry transitioned away (D3).

        ``_remove_from_index`` discards the id but leaves the now-empty set in
        ``_index_by_status``; without the ``if ids`` guard the drained status
        would surface as ``:0`` and break SQL/Redis parity.
        """
        e1 = _seed(repo, domain="payment", status=_PENDING)
        e2 = _seed(repo, domain="payment", status=_PENDING)
        # Transition every PENDING entry away → PENDING bucket fully drained.
        repo.update_status(e1, _RESOLVED)
        repo.update_status(e2, _RESOLVED)

        result = repo.get_facet_counts()

        # The lingering empty PENDING set must NOT surface as a zero count.
        assert _PENDING not in result["by_status"]
        assert result["by_status"] == {_RESOLVED: 2}

    def test_drained_domain_does_not_surface_as_zero(self, repo):
        """Zero-drop on a fully-deleted domain (D3, mirrors the status case)."""
        e1 = _seed(repo, domain="payment", status=_PENDING)
        _seed(repo, domain="inventory", status=_PENDING)
        repo.delete(e1)

        result = repo.get_facet_counts()

        assert "payment" not in result["by_domain"]
        assert result["by_domain"] == {"inventory": 1}

    def test_scoped_facet_drops_zero_for_unrelated_status(self, repo):
        """domain=X never surfaces a status with zero entries in X (2D index)."""
        _seed(repo, domain="payment", status=_PENDING)
        _seed(repo, domain="inventory", status=_RESOLVED)

        result = repo.get_facet_counts(domain="payment")

        # payment has no RESOLVED entries → RESOLVED absent (not :0).
        assert _RESOLVED not in result["by_status"]
        assert result["by_status"] == {_PENDING: 1}

    def test_scope_with_unknown_domain_returns_empty_by_status(self, repo):
        """Edge: a domain that does not exist → empty by_status (not error)."""
        _seed(repo, domain="payment", status=_PENDING)

        result = repo.get_facet_counts(domain="ghost")

        assert result["by_status"] == {}
        # by_domain is unscoped, so it still lists existing domains.
        assert result["by_domain"] == {"payment": 1}

    def test_returns_independent_dicts_no_internal_aliasing(self, repo):
        """Result mutation must not corrupt the repo's internal indexes."""
        _seed(repo, domain="payment", status=_PENDING)

        result = repo.get_facet_counts()
        result["by_status"]["fake"] = 999
        result["by_domain"]["fake"] = 999

        # Re-query and confirm the repo did not absorb the mutation.
        again = repo.get_facet_counts()
        assert "fake" not in again["by_status"]
        assert "fake" not in again["by_domain"]


class TestInMemoryFacetCountsContract:
    """Contract: return-value shape is stable across filter combinations."""

    @pytest.mark.parametrize(
        ("status", "domain"),
        [
            (None, None),
            (_RESOLVED, None),
            (None, "payment"),
            (_RESOLVED, "payment"),
        ],
    )
    def test_result_has_exactly_by_status_and_by_domain_keys(
        self, repo, status, domain
    ):
        """All 4 filter shapes return the same top-level shape."""
        _seed(repo, domain="payment", status=_RESOLVED)

        result = repo.get_facet_counts(status=status, domain=domain)

        assert set(result.keys()) == {"by_status", "by_domain"}
        assert isinstance(result["by_status"], dict)
        assert isinstance(result["by_domain"], dict)
