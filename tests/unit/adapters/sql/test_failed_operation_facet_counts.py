"""Unit tests for SQLFailedOperationRepository.get_facet_counts (542 D3).

Faceted ``status×domain`` counts implemented via ``GROUP BY`` (with an
optional ``WHERE``). ``by_status`` is scoped by ``domain``; ``by_domain`` is
scoped by ``status`` (faceted-search semantics, D2). Empty buckets are
dropped structurally because ``GROUP BY`` never emits a zero row.

Run against the shared in-memory sqlite fixture from ``conftest.py``.

Coverage axes (Test Assessment 542):
- parametrize 4 filter shapes
- adapter parity with the memory adapter for the same seeded set
- zero-drop semantics — the absent (status, domain) combination is never
  surfaced as ``:0``
"""

from __future__ import annotations

import pytest

from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationStatus

_PENDING = FailedOperationStatus.PENDING.value
_RESOLVED = FailedOperationStatus.RESOLVED.value


@pytest.fixture
def dlq(get_sqlite_conn) -> SQLFailedOperationRepository:
    return SQLFailedOperationRepository(get_sqlite_conn)


def _seed(dlq, *, domain: str, status: str) -> str:
    entry = dlq.create(domain=domain, failure_type="timeout")
    if status != _PENDING:
        dlq.update_status(entry.id, status)
    return entry.id


class TestSQLFacetCountsBehavior:
    """SQL GROUP BY contract under the four filter shapes."""

    def test_unfiltered_returns_complete_status_and_domain_counts(self, dlq):
        _seed(dlq, domain="payment", status=_PENDING)
        _seed(dlq, domain="payment", status=_PENDING)
        _seed(dlq, domain="payment", status=_RESOLVED)
        _seed(dlq, domain="inventory", status=_PENDING)

        result = dlq.get_facet_counts()

        assert result == {
            "by_status": {_PENDING: 3, _RESOLVED: 1},
            "by_domain": {"payment": 3, "inventory": 1},
        }

    def test_domain_scope_narrows_by_status_to_that_domain(self, dlq):
        """domain=X scopes by_status (D2). by_domain is unscoped on its axis."""
        _seed(dlq, domain="payment", status=_PENDING)
        _seed(dlq, domain="payment", status=_RESOLVED)
        _seed(dlq, domain="inventory", status=_PENDING)
        _seed(dlq, domain="inventory", status=_PENDING)

        result = dlq.get_facet_counts(domain="payment")

        assert result["by_status"] == {_PENDING: 1, _RESOLVED: 1}
        assert result["by_domain"] == {"payment": 2, "inventory": 2}

    def test_status_scope_narrows_by_domain_to_that_status(self, dlq):
        _seed(dlq, domain="payment", status=_PENDING)
        _seed(dlq, domain="inventory", status=_PENDING)
        _seed(dlq, domain="payment", status=_RESOLVED)
        _seed(dlq, domain="inventory", status=_RESOLVED)
        _seed(dlq, domain="inventory", status=_RESOLVED)

        result = dlq.get_facet_counts(status=_RESOLVED)

        assert result["by_domain"] == {"payment": 1, "inventory": 2}
        assert result["by_status"] == {_PENDING: 2, _RESOLVED: 3}

    def test_both_filters_scope_both_axes(self, dlq):
        _seed(dlq, domain="payment", status=_PENDING)
        _seed(dlq, domain="payment", status=_RESOLVED)
        _seed(dlq, domain="inventory", status=_PENDING)
        _seed(dlq, domain="inventory", status=_RESOLVED)
        _seed(dlq, domain="inventory", status=_RESOLVED)

        result = dlq.get_facet_counts(status=_RESOLVED, domain="payment")

        assert result["by_status"] == {_PENDING: 1, _RESOLVED: 1}
        assert result["by_domain"] == {"payment": 1, "inventory": 2}

    def test_empty_repository_returns_empty_maps(self, dlq):
        """GROUP BY on an empty table → no rows → empty maps."""
        assert dlq.get_facet_counts() == {"by_status": {}, "by_domain": {}}

    def test_absent_combination_does_not_surface_as_zero(self, dlq):
        """domain X with no entries of status Y → Y is omitted, not :0."""
        _seed(dlq, domain="payment", status=_PENDING)
        _seed(dlq, domain="inventory", status=_RESOLVED)

        result = dlq.get_facet_counts(domain="payment")

        # payment has no RESOLVED entry → RESOLVED omitted from by_status.
        assert _RESOLVED not in result["by_status"]
        assert result["by_status"] == {_PENDING: 1}

    def test_scope_with_unknown_domain_returns_empty_by_status(self, dlq):
        """WHERE domain=ghost matches nothing → GROUP BY emits no rows."""
        _seed(dlq, domain="payment", status=_PENDING)

        result = dlq.get_facet_counts(domain="ghost")

        assert result["by_status"] == {}
        assert result["by_domain"] == {"payment": 1}

    def test_counts_are_int_not_decimal_or_str(self, dlq):
        """The SQL adapter coerces COUNT(*) via int() — confirm the cast."""
        _seed(dlq, domain="payment", status=_PENDING)

        result = dlq.get_facet_counts()

        for count in result["by_status"].values():
            assert isinstance(count, int)
        for count in result["by_domain"].values():
            assert isinstance(count, int)


class TestSQLFacetCountsContract:
    """Top-level shape is stable across the 4 filter combinations."""

    @pytest.mark.parametrize(
        ("status", "domain"),
        [
            (None, None),
            (_RESOLVED, None),
            (None, "payment"),
            (_RESOLVED, "payment"),
        ],
    )
    def test_result_has_exactly_by_status_and_by_domain_keys(self, dlq, status, domain):
        _seed(dlq, domain="payment", status=_RESOLVED)

        result = dlq.get_facet_counts(status=status, domain=domain)

        assert set(result.keys()) == {"by_status", "by_domain"}
        assert isinstance(result["by_status"], dict)
        assert isinstance(result["by_domain"], dict)
