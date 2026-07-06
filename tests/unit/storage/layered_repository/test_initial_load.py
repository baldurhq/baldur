"""Initial-load hydration regression pin for 477 (Cat 6.3 F1).

Gates against re-introduction of the `get_all` typo in
``L2LoadMixin._load_from_l2_with_timeout`` and the silent re-addition of the
``get_all`` backward-compatibility alias on
``InMemoryCircuitBreakerStateRepository``.

The existing ``test_cold_start.py::test_l2_load_attempted_on_init`` only
asserts that the L2 method was called — it does NOT assert hydration into L1.
This file adds the hydration assertion (5-field per-state equality) plus a
contract test that pins the alias removal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock


class TestLayeredRepoInitialLoadBehavior:
    """LayeredCircuitBreakerStateRepository.__init__ hydrates L1 from L2."""

    def test_initial_load_with_populated_l2_hydrates_l1(self):
        """Populated L2 → L1 contains all states with 5-field equality.

        Uses a spec-bounded mock so a future re-introduction of the typo
        (``self._l2.get_all``) fails at test time rather than being swallowed
        by the broad ``except Exception`` in ``_load_from_l2_with_timeout``.
        """
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.interfaces.repositories import CircuitBreakerStateData

        # Given: spec-bounded mock L2 exposing only the canonical surface
        opened_at_a = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
        opened_at_b = datetime(2026, 5, 6, 11, 0, 0, tzinfo=UTC)
        l2_states = [
            CircuitBreakerStateData(
                service_name="payment-service",
                state="open",
                failure_count=7,
                success_count=2,
                opened_at=opened_at_a,
            ),
            CircuitBreakerStateData(
                service_name="catalog-service",
                state="half_open",
                failure_count=3,
                success_count=5,
                opened_at=opened_at_b,
            ),
        ]
        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = l2_states

        # When: LayeredRepository __init__ triggers initial load
        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)

        # Then: L1 actually hydrated (not silently empty due to swallowed error)
        l1_states = repo._l1.get_all_states()
        assert len(l1_states) == len(l2_states)

        # Per-state equality on the 5 fields propagated by update_state:
        # service_name, state, failure_count, success_count, opened_at.
        l1_by_service = {s.service_name: s for s in l1_states}
        for expected in l2_states:
            actual = l1_by_service[expected.service_name]
            assert actual.state == expected.state
            assert actual.failure_count == expected.failure_count
            assert actual.success_count == expected.success_count
            assert actual.opened_at == expected.opened_at

        mock_l2.get_all_states.assert_called_once()

    def test_initial_load_with_empty_l2_does_not_raise(self):
        """Empty L2 → no AttributeError, L1 stays empty, get_all_states called once."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        # Given: L2 is reachable but holds no entries
        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = []

        # When
        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)

        # Then: empty L1 without any swallowed exception
        assert repo._l1.get_all_states() == []
        mock_l2.get_all_states.assert_called_once()


class TestInMemoryCircuitBreakerStateRepositoryContract:
    """InMemoryCircuitBreakerStateRepository surface is canonical."""

    def test_get_all_alias_is_removed_from_in_memory_adapter(self):
        """No ``get_all`` attribute on the in-memory adapter.

        Pairs with
        ``tests/unit/interfaces/test_interface_contract_integrity.py::``
        ``TestGetAllStatesAbstractBehavior::test_get_all_is_removed``,
        which gates the ABC. This extends the gate to the concrete in-memory
        adapter so the backward-compat alias cannot be silently re-added.
        """
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )

        assert not hasattr(InMemoryCircuitBreakerStateRepository, "get_all")
