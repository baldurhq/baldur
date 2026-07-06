"""
Unit tests for 428 — RedisDLQQuery.get_statistics() pending breakdown (D9).

Test target:
  - baldur.adapters.redis.dlq_query.RedisDLQQuery.get_statistics() — extended
    with pending_by_domain and pending_by_domain_and_failure_type.
  - RedisDLQQuery._collect_pending_breakdown() — chunked ZRANGE + hgetall loop
    in normal Redis path; in-memory iteration in degraded mode.

Fail-open behavior: breakdown computation wrapped in try/except. On exception
the keys are absent but baseline counts (pending/resolved/archived/etc.) are
preserved.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from baldur.adapters.redis.dlq_query import RedisDLQQuery


def _blob(data: dict) -> bytes:
    """Encode dict as the orjson-style JSON bytes a real entry blob would hold."""
    return json.dumps(data).encode("utf-8")


def _make_repo_with_query(backend: MagicMock) -> tuple[MagicMock, RedisDLQQuery]:
    """Construct RedisDLQRepository-like stub that RedisDLQQuery uses."""
    from baldur.adapters.redis.dlq import RedisDLQRepository

    with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
        repo = RedisDLQRepository.__new__(RedisDLQRepository)
    repo._backend = backend
    repo._key_prefix = "dlq:"
    repo._pending_key = "dlq:pending"
    repo._entry_prefix = "dlq:entry:"
    repo._by_domain_prefix = "dlq:by_domain:"
    repo._status_prefix = "dlq:status:"
    repo._status_domain_prefix = "dlq:status_domain:"
    repo._all_key = "dlq:all"
    repo._domains_key = "dlq:domains"
    repo._known_domains = set()
    repo._to_data = MagicMock()
    query = RedisDLQQuery(repo)
    return repo, query


class TestGetStatisticsPendingBreakdownBehavior:
    """D9 pending breakdown in Redis adapter."""

    def test_breakdown_fields_returned_alongside_baseline_counts(self):
        """Normal path: pending_by_domain + pending_by_domain_and_failure_type present."""
        backend = MagicMock()
        # ZCARD baseline counts
        backend.zcard.return_value = 3
        backend.is_degraded = False
        backend.zrange.return_value = [b"1", b"2", b"3"]

        repo, query = _make_repo_with_query(backend)
        repo._load_blob = MagicMock(
            side_effect=[
                _blob({"domain": "payment", "failure_type": "TIMEOUT"}),
                _blob({"domain": "payment", "failure_type": "AUTH_ERROR"}),
                _blob({"domain": "inventory", "failure_type": "TIMEOUT"}),
            ]
        )
        repo.count_pending = MagicMock(return_value=3)
        query.count_by_status = MagicMock(return_value=0)
        repo._make_key = MagicMock(side_effect=lambda i: f"dlq:{i}")

        stats = query.get_statistics()

        assert stats["pending_by_domain"] == {"payment": 2, "inventory": 1}
        assert stats["pending_by_domain_and_failure_type"] == {
            "payment": {"TIMEOUT": 1, "AUTH_ERROR": 1},
            "inventory": {"TIMEOUT": 1},
        }

    def test_breakdown_failure_open_preserves_baseline_counts(self):
        """Exception inside breakdown collection: baseline keys still returned, breakdown keys absent."""
        backend = MagicMock()
        backend.is_degraded = False

        repo, query = _make_repo_with_query(backend)
        repo.count_pending = MagicMock(return_value=5)
        query.count_by_status = MagicMock(return_value=0)
        # Force exception inside _collect_pending_breakdown
        query._collect_pending_breakdown = MagicMock(side_effect=RuntimeError("boom"))

        stats = query.get_statistics()

        assert stats["pending"] == 5
        assert "pending_by_domain" not in stats
        assert "pending_by_domain_and_failure_type" not in stats

    def test_empty_pending_backlog_returns_empty_breakdown(self):
        """No pending entries -> empty maps, no errors."""
        backend = MagicMock()
        backend.zcard.return_value = 0
        backend.is_degraded = False
        backend.zrange.return_value = []

        repo, query = _make_repo_with_query(backend)
        repo.count_pending = MagicMock(return_value=0)
        query.count_by_status = MagicMock(return_value=0)
        repo._make_key = MagicMock(side_effect=lambda i: f"dlq:{i}")

        stats = query.get_statistics()

        assert stats["pending_by_domain"] == {}
        assert stats["pending_by_domain_and_failure_type"] == {}

    def test_degraded_mode_iterates_memory_map(self):
        """backend.is_degraded -> in-memory fallback aggregation."""
        from baldur.interfaces.repositories import FailedOperationStatus

        backend = MagicMock()
        backend.is_degraded = True
        backend._memory = {
            "dlq:1": _blob(
                {
                    "status": FailedOperationStatus.PENDING.value,
                    "domain": "payment",
                    "failure_type": "TIMEOUT",
                }
            ),
            "dlq:2": _blob(
                {
                    "status": FailedOperationStatus.PENDING.value,
                    "domain": "inventory",
                    "failure_type": "TIMEOUT",
                }
            ),
            "dlq:3": _blob(
                {
                    "status": FailedOperationStatus.RESOLVED.value,  # Excluded
                    "domain": "payment",
                    "failure_type": "AUTH_ERROR",
                }
            ),
            "dlq:pending": "special_key",  # Special key - excluded
        }

        repo, query = _make_repo_with_query(backend)
        repo._is_valid_entry_key = MagicMock(
            side_effect=lambda k: k.startswith("dlq:") and k.split(":")[1].isdigit()
        )

        by_domain, by_ft = query._collect_pending_breakdown()

        assert by_domain == {"payment": 1, "inventory": 1}
        assert by_ft == {
            "payment": {"TIMEOUT": 1},
            "inventory": {"TIMEOUT": 1},
        }

    def test_chunked_iteration_uses_given_batch_size(self):
        """ZRANGE called in batches determined by batch_size parameter."""
        backend = MagicMock()
        backend.is_degraded = False
        backend.zcard.return_value = 5

        # 5 total entries; with batch_size=2, expect ZRANGE calls at [0,1], [2,3], [4,5]
        def zrange_side_effect(key, start, stop):
            if start == 0:
                return [b"1", b"2"]
            if start == 2:
                return [b"3", b"4"]
            if start == 4:
                return [b"5"]
            return []

        backend.zrange.side_effect = zrange_side_effect

        repo, query = _make_repo_with_query(backend)
        repo._load_blob = MagicMock(
            return_value=_blob({"domain": "payment", "failure_type": "TIMEOUT"})
        )
        repo._make_key = MagicMock(side_effect=lambda i: f"dlq:{i}")

        by_domain, _ = query._collect_pending_breakdown(batch_size=2)

        # 5 pending entries, all payment/TIMEOUT
        assert by_domain == {"payment": 5}
        # Three zrange calls for chunks of 2
        assert backend.zrange.call_count == 3
