"""
Contract tests for the FailedOperationRepository paginated find/count primitive
(541 D1).

Verifies the ABC declares ``find`` and ``count`` as abstract methods with the
sibling-repository keyword-only signature, and that the stale
``DjangoFailedOperationRepository`` docstring reference is gone.
"""

from __future__ import annotations

import inspect

from baldur.interfaces.repositories import FailedOperationRepository


class TestFailedOperationRepositoryContract:
    """find/count are part of the abstract contract (541 D1)."""

    def test_find_and_count_are_abstract_methods(self):
        """Both find and count are required by the ABC."""
        assert {"find", "count"} <= FailedOperationRepository.__abstractmethods__

    def test_find_signature_is_keyword_only_with_offset_limit(self):
        """find mirrors the sibling repos: keyword-only filters + offset/limit."""
        sig = inspect.signature(FailedOperationRepository.find)
        params = sig.parameters

        # status/domain/failure_type/offset/limit are all keyword-only (after *)
        for name in ("status", "domain", "failure_type", "offset", "limit"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY

        assert params["offset"].default == 0
        assert params["limit"].default == 100
        assert params["status"].default is None
        assert params["domain"].default is None
        assert params["failure_type"].default is None

    def test_count_signature_is_keyword_only_filters_only(self):
        """count takes the same filters but no offset/limit."""
        sig = inspect.signature(FailedOperationRepository.count)
        params = sig.parameters

        for name in ("status", "domain", "failure_type"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert params[name].default is None

        assert "offset" not in params
        assert "limit" not in params

    def test_find_by_status_remains_a_distinct_positional_contract(self):
        """find_by_status keeps its positional status + no-offset shape (541 D1)."""
        sig = inspect.signature(FailedOperationRepository.find_by_status)
        params = sig.parameters

        # status is positional-or-keyword (required, no default), unlike find's
        # keyword-only optional status.
        assert params["status"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert params["status"].default is inspect.Parameter.empty
        assert "offset" not in params

    def test_abc_docstring_drops_stale_django_repository_reference(self):
        """The non-existent DjangoFailedOperationRepository reference is removed."""
        doc = FailedOperationRepository.__doc__ or ""
        assert "DjangoFailedOperationRepository" not in doc
        # The three real concrete impls are named instead.
        assert "InMemoryFailedOperationRepository" in doc
        assert "SQLFailedOperationRepository" in doc
        assert "RedisDLQRepository" in doc


class TestFailedOperationRepositoryFacetCountsContract:
    """get_facet_counts is part of the abstract contract (542 D3)."""

    def test_get_facet_counts_is_abstract_method(self):
        """The faceted count primitive is required by the ABC (542 D3)."""
        assert "get_facet_counts" in FailedOperationRepository.__abstractmethods__

    def test_get_facet_counts_signature_is_keyword_only(self):
        """Both filters are keyword-only — positional calls reject."""
        sig = inspect.signature(FailedOperationRepository.get_facet_counts)
        params = sig.parameters

        for name in ("status", "domain"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert params[name].default is None

        # No ``offset``/``limit`` — facets are a full-aggregate primitive,
        # never paginated.
        assert "offset" not in params
        assert "limit" not in params

    def test_get_facet_counts_concrete_adapters_implement_it(self):
        """The three concrete adapters (memory, SQL, Redis) all implement
        ``get_facet_counts`` — i.e. it is NOT in their ``__abstractmethods__``
        sets, so they can be instantiated (542 D3)."""
        from baldur.adapters.memory.failed_operation import (
            InMemoryFailedOperationRepository,
        )
        from baldur.adapters.redis.dlq import RedisDLQRepository
        from baldur.adapters.sql.failed_operation import (
            SQLFailedOperationRepository,
        )

        for cls in (
            InMemoryFailedOperationRepository,
            SQLFailedOperationRepository,
            RedisDLQRepository,
        ):
            assert "get_facet_counts" not in cls.__abstractmethods__, (
                f"{cls.__name__} must implement get_facet_counts"
            )
