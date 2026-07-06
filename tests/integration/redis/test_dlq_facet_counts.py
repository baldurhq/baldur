"""Real-Redis integration tests for ``RedisDLQRepository.get_facet_counts``
(542 D4).

What this tests that the unit tests cannot:

The unit tests in ``tests/unit/adapters/redis/test_dlq_facet_counts.py``
cover correctness against a hand-rolled fake-redis. This module exercises the
*contract* under test — server-side ``ZINTERCARD`` cardinality over the two
existing per-dimension ZSETs (``by_domain:*`` and the per-status indexes), the
keyspace ``SCAN`` cursor loop, ``ZCARD`` round-trips, and the ``<7.0``
fallback path — against a real Redis 7+ server.

Coverage axes (Test Assessment 542):
- Adapter parity — unfiltered + scoped facets equal the memory adapter run
  against the same seeded set (`G3 success criterion`).
- ZINTERCARD vs blob-bucket fallback drift (same seeded set → same counts).
- D5 `get_cleanup_stats().by_status` end-to-end with a real Redis: all
  present indexed statuses surfaced; zero-count statuses absent.
- Empty-set boundary — ``ZINTERCARD`` on a missing/empty key returns 0
  (drop), not an error.

Auto-skips when Redis is unavailable via the conftest-installed
``requires_redis`` marker autoskip hook.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.requires_redis


from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationStatus

_PENDING = FailedOperationStatus.PENDING.value
_RESOLVED = FailedOperationStatus.RESOLVED.value


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    """Reset the runtime-scoped Redis negative cache so backend can init Redis.

    Mirrors the helper used by ``test_dlq_lua_acquire.py`` (the Redis
    integration tests reuse this pattern).
    """
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


def _seed(repo, *, domain: str, status: str) -> str:
    """Create one entry in ``repo`` and (if needed) move it to ``status``."""
    entry = repo.create(domain=domain, failure_type="timeout")
    if status != _PENDING:
        repo.update_status(entry.id, status)
    return entry.id


def _seed_set(repo) -> None:
    """Seed a fixed mix of statuses × domains used across parity assertions.

    Mirrors the unit-test set so the parity assertion checks the same shape.
    """
    _seed(repo, domain="payment", status=_PENDING)
    _seed(repo, domain="payment", status=_PENDING)
    _seed(repo, domain="payment", status=_RESOLVED)
    _seed(repo, domain="inventory", status=_PENDING)
    _seed(repo, domain="inventory", status=_RESOLVED)
    _seed(repo, domain="inventory", status=_RESOLVED)


# =============================================================================
# Adapter parity — Redis vs memory adapter run against the same seeded set
# =============================================================================


class TestRedisFacetCountsAdapterParity:
    """G3 success criterion — Redis matches memory for the same seeded set."""

    def test_unfiltered_facets_match_memory_adapter(self, redis_dlq_repository):
        _seed_set(redis_dlq_repository)

        memory = InMemoryFailedOperationRepository()
        _seed_set(memory)

        assert redis_dlq_repository.get_facet_counts() == memory.get_facet_counts()

    def test_domain_scoped_facets_match_memory_adapter(self, redis_dlq_repository):
        _seed_set(redis_dlq_repository)
        memory = InMemoryFailedOperationRepository()
        _seed_set(memory)

        for d in ("payment", "inventory"):
            assert redis_dlq_repository.get_facet_counts(
                domain=d
            ) == memory.get_facet_counts(domain=d)

    def test_status_scoped_facets_match_memory_adapter(self, redis_dlq_repository):
        _seed_set(redis_dlq_repository)
        memory = InMemoryFailedOperationRepository()
        _seed_set(memory)

        for s in (_PENDING, _RESOLVED):
            assert redis_dlq_repository.get_facet_counts(
                status=s
            ) == memory.get_facet_counts(status=s)

    def test_both_scopes_match_memory_adapter(self, redis_dlq_repository):
        _seed_set(redis_dlq_repository)
        memory = InMemoryFailedOperationRepository()
        _seed_set(memory)

        result_redis = redis_dlq_repository.get_facet_counts(
            status=_RESOLVED, domain="payment"
        )
        result_memory = memory.get_facet_counts(status=_RESOLVED, domain="payment")
        assert result_redis == result_memory


# =============================================================================
# ZINTERCARD vs blob-bucket fallback drift
# =============================================================================


class TestRedisFacetCountsZintercardFallbackDrift:
    """Forcing the <7.0 fallback path must yield identical counts to the
    ZINTERCARD path on the same seeded set (D4 — drift assertion).
    """

    def test_fallback_path_matches_zintercard_path(self, redis_dlq_repository):
        _seed_set(redis_dlq_repository)

        # Snapshot the ZINTERCARD result first.
        zintercard_result = redis_dlq_repository.get_facet_counts(domain="payment")

        # Flip the capability cache to False so the next call MUST use
        # the bounded blob-bucket scan fallback.
        redis_dlq_repository.query._zintercard_supported = False

        fallback_result = redis_dlq_repository.get_facet_counts(domain="payment")

        # Same seeded set → identical counts across both code paths.
        assert fallback_result == zintercard_result

    def test_status_scoped_fallback_matches_zintercard(self, redis_dlq_repository):
        _seed_set(redis_dlq_repository)

        zintercard_result = redis_dlq_repository.get_facet_counts(status=_RESOLVED)
        redis_dlq_repository.query._zintercard_supported = False
        fallback_result = redis_dlq_repository.get_facet_counts(status=_RESOLVED)

        assert fallback_result == zintercard_result


# =============================================================================
# Empty-set boundary — ZINTERCARD on missing keys must drop, not error
# =============================================================================


class TestRedisFacetCountsEmptyBoundary:
    """ZINTERCARD on a missing/empty key returns 0 — the read-side facet
    must drop it instead of surfacing ``:0`` (D4 zero-drop)."""

    def test_facets_on_empty_repository_returns_empty_maps(self, redis_dlq_repository):
        """No entries → both facet maps are empty (no zero-keys lingering)."""
        result = redis_dlq_repository.get_facet_counts()
        assert result == {"by_status": {}, "by_domain": {}}

    def test_scoped_facet_on_unknown_domain_returns_empty_by_status(
        self, redis_dlq_repository
    ):
        """ZINTERCARD with a missing left key (``by_domain:ghost``) returns 0
        for every status — the result drops every bucket (no ``:0`` keys)."""
        _seed(redis_dlq_repository, domain="payment", status=_PENDING)

        result = redis_dlq_repository.get_facet_counts(domain="ghost")

        assert result["by_status"] == {}
        # by_domain is unscoped on its own axis, so payment is still listed.
        assert result["by_domain"] == {"payment": 1}


# =============================================================================
# D5 — get_cleanup_stats().by_status end-to-end
# =============================================================================


class TestRedisCleanupStatsCompleteByStatusIntegration:
    """End-to-end D5: all present indexed statuses surface via real ZCARD;
    zero-count statuses are omitted (not ``:0``)."""

    def test_pending_and_resolved_present_when_seeded(self, redis_dlq_repository):
        _seed(redis_dlq_repository, domain="payment", status=_PENDING)
        _seed(redis_dlq_repository, domain="payment", status=_RESOLVED)
        _seed(redis_dlq_repository, domain="inventory", status=_RESOLVED)

        stats = redis_dlq_repository.get_cleanup_stats()

        assert stats["by_status"][_PENDING] == 1
        assert stats["by_status"][_RESOLVED] == 2

    def test_zero_count_statuses_are_absent_from_by_status(self, redis_dlq_repository):
        """A status that has never had an entry must NOT appear as ``:0`` —
        matches memory/SQL adapters (D5 parity)."""
        _seed(redis_dlq_repository, domain="payment", status=_PENDING)

        stats = redis_dlq_repository.get_cleanup_stats()

        # Indexed-but-empty statuses (REQUIRES_REVIEW, REJECTED, ARCHIVED,
        # EXPIRED, etc.) must be omitted, not surfaced as ``:0``.
        assert all(count > 0 for count in stats["by_status"].values())
        for absent in (
            FailedOperationStatus.REQUIRES_REVIEW.value,
            FailedOperationStatus.REJECTED.value,
            FailedOperationStatus.ARCHIVED.value,
            FailedOperationStatus.EXPIRED.value,
        ):
            assert absent not in stats["by_status"]
