"""
Tests for F14-C1: InMemory Pool adapters.

Source: src/baldur/adapters/pool/memory_stats.py, memory_recovery.py
"""

from __future__ import annotations


class TestInMemoryPoolStatsProviderBehavior:
    """InMemoryPoolStatsProvider returns and updates PoolStats."""

    def test_get_stats_returns_pool_stats_with_configured_values(self):
        """get_stats returns PoolStats matching constructor arguments."""
        from baldur.adapters.pool.memory_stats import InMemoryPoolStatsProvider

        provider = InMemoryPoolStatsProvider(
            pool_name="my_pool",
            max_connections=20,
            active_connections=5,
            available_connections=15,
            waiting_requests=2,
        )

        stats = provider.get_stats()

        assert stats.pool_name == "my_pool"
        assert stats.max_connections == 20
        assert stats.active_connections == 5
        assert stats.available_connections == 15
        assert stats.waiting_requests == 2

    def test_set_stats_updates_individual_fields(self):
        """set_stats updates only the specified fields."""
        from baldur.adapters.pool.memory_stats import InMemoryPoolStatsProvider

        provider = InMemoryPoolStatsProvider(
            pool_name="test",
            max_connections=10,
            active_connections=0,
            available_connections=10,
        )

        provider.set_stats(active_connections=7, available_connections=3)

        stats = provider.get_stats()
        assert stats.active_connections == 7
        assert stats.available_connections == 3
        assert stats.max_connections == 10
        assert stats.pool_name == "test"


class TestInMemoryPoolRecoveryHandlerBehavior:
    """InMemoryPoolRecoveryHandler records and clears recovery actions."""

    def test_expand_pool_records_action_and_returns_true(self):
        """expand_pool records action and returns True by default."""
        from baldur.adapters.pool.memory_recovery import (
            InMemoryPoolRecoveryHandler,
        )

        handler = InMemoryPoolRecoveryHandler()

        result = handler.expand_pool(additional_connections=5)

        assert result is True
        actions = handler.get_actions()
        assert len(actions) == 1
        assert actions[0]["action"] == "expand_pool"
        assert actions[0]["additional_connections"] == 5

    def test_get_actions_returns_list_of_recorded_actions(self):
        """get_actions returns all recorded actions in order."""
        from baldur.adapters.pool.memory_recovery import (
            InMemoryPoolRecoveryHandler,
        )

        handler = InMemoryPoolRecoveryHandler()
        handler.close_connection("conn-1")
        handler.expand_pool(3)
        handler.shrink_pool(5)

        actions = handler.get_actions()

        assert len(actions) == 3
        assert actions[0]["action"] == "close_connection"
        assert actions[1]["action"] == "expand_pool"
        assert actions[2]["action"] == "shrink_pool"

    def test_clear_actions_empties_the_list(self):
        """clear_actions removes all recorded actions."""
        from baldur.adapters.pool.memory_recovery import (
            InMemoryPoolRecoveryHandler,
        )

        handler = InMemoryPoolRecoveryHandler()
        handler.expand_pool(2)
        handler.close_connection("conn-x")
        assert len(handler.get_actions()) == 2

        handler.clear_actions()

        assert handler.get_actions() == []
