"""Unit tests for RedisDLQRepository.count_created_in_window (622 D3).

The Redis windowed inflow count is a ZCOUNT over the status-independent global
index ``dlq:all``, whose member score IS the created_at epoch. The repository
delegates to ``RedisDLQQuery.count_created_in_window``, which issues
``backend.zcount(ALL_KEY, start_ts, end_ts)`` — an O(log N) range read.

The ZCOUNT range semantics themselves are covered by the backend zcount tests
(``test_resilient_zcount``); here we verify the adapter delegates with the right
index key and epoch-second bounds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from baldur.adapters.redis.dlq import RedisDLQRepository

_START = datetime(2026, 6, 1, tzinfo=UTC)
_END = datetime(2026, 6, 12, tzinfo=UTC)


class TestCountCreatedInWindowRedis:
    """count_created_in_window() delegates to a ZCOUNT over the global index."""

    def test_returns_backend_zcount_result(self):
        """The repository returns the backend's ZCOUNT result verbatim."""
        backend = MagicMock()
        backend.zcount.return_value = 9
        repo = RedisDLQRepository(backend)

        assert repo.count_created_in_window(_START, _END) == 9

    def test_zcounts_global_index_with_epoch_bounds(self):
        """ZCOUNT targets ALL_KEY with the created_at epoch-second bounds."""
        # Given a repo over a mock backend.
        backend = MagicMock()
        backend.zcount.return_value = 0
        repo = RedisDLQRepository(backend)

        # When the windowed count runs.
        repo.count_created_in_window(_START, _END)

        # Then it issues exactly one ZCOUNT over the global index with epoch bounds.
        backend.zcount.assert_called_once_with(
            repo.ALL_KEY, _START.timestamp(), _END.timestamp()
        )

    def test_global_index_key_is_dlq_all(self):
        """ALL_KEY (the global created_at-scored index) is ``dlq:all``."""
        repo = RedisDLQRepository(MagicMock())

        assert repo.ALL_KEY == "dlq:all"
