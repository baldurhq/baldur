"""
Unit tests for InMemoryFailedOperationRepository.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from baldur.interfaces.repositories import FailedOperationStatus
from tests.factories.time_helpers import freeze_time

FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


class TestInMemoryFailedOperationRepository:
    """Tests for InMemoryFailedOperationRepository."""

    @pytest.fixture
    def repo(self):
        """Create a fresh repository for each test."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        return InMemoryFailedOperationRepository()

    def test_create_failed_operation(self, repo):
        """Test creating a new failed operation."""

        entry = repo.create(
            domain="payment",
            failure_type="gateway_timeout",
            error_message="Connection timeout to payment gateway",
            error_code="TIMEOUT_001",
            entity_type="order",
            entity_id="12345",
            entity_refs={"order_id": 12345, "payment_id": 67890},
            user_id=100,
        )

        assert entry.id == "1"
        assert entry.domain == "payment"
        assert entry.failure_type == "gateway_timeout"
        assert entry.error_message == "Connection timeout to payment gateway"
        assert entry.error_code == "TIMEOUT_001"
        assert entry.entity_type == "order"
        assert entry.entity_id == "12345"
        assert entry.entity_refs.get("order_id") == 12345
        assert entry.entity_refs.get("payment_id") == 67890
        assert entry.user_id == 100
        assert entry.status == FailedOperationStatus.PENDING.value
        assert entry.created_at is not None
        assert entry.retry_count == 0

    def test_get_by_id(self, repo):
        """Test retrieving a failed operation by ID."""
        created = repo.create(
            domain="payment",
            failure_type="validation_error",
            error_message="Invalid card number",
        )

        retrieved = repo.get_by_id(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.domain == "payment"
        assert retrieved.failure_type == "validation_error"

    def test_get_by_id_not_found(self, repo):
        """Test retrieving a non-existent failed operation."""
        result = repo.get_by_id(99999)
        assert result is None

    def test_get_pending_by_domain(self, repo):
        """Test filtering pending operations by domain."""
        repo.create(domain="payment", failure_type="error1", error_message="err")
        repo.create(domain="payment", failure_type="error2", error_message="err")
        repo.create(domain="webhook", failure_type="error3", error_message="err")

        payment_entries = repo.get_pending_by_domain("payment")
        assert len(payment_entries) == 2

        webhook_entries = repo.get_pending_by_domain("webhook")
        assert len(webhook_entries) == 1

    def test_update_status(self, repo):
        """Test updating the status of a failed operation."""

        entry = repo.create(
            domain="payment",
            failure_type="timeout",
            error_message="Request timeout",
        )

        result = repo.update_status(
            entry.id,
            FailedOperationStatus.RESOLVED.value,
            resolution_type="manual",
            resolution_note="Fixed by admin",
        )

        assert result is True

        updated = repo.get_by_id(entry.id)
        assert updated.status == FailedOperationStatus.RESOLVED.value
        assert updated.resolution_type == "manual"
        assert updated.resolution_note == "Fixed by admin"
        assert updated.resolved_at is not None

    def test_update_status_with_recommended_action(self, repo):
        """update_status() persists recommended_action (G3 escalation)."""

        entry = repo.create(
            domain="payment",
            failure_type="timeout",
            error_message="Request timeout",
        )

        result = repo.update_status(
            entry.id,
            FailedOperationStatus.REQUIRES_REVIEW.value,
            resolution_note="Replay failed",
            recommended_action="escalate",
        )

        assert result is True
        updated = repo.get_by_id(entry.id)
        assert updated.status == FailedOperationStatus.REQUIRES_REVIEW.value
        assert updated.recommended_action == "escalate"

    def test_update_status_empty_recommended_action_preserves_existing(self, repo):
        """Empty recommended_action does not overwrite existing value."""

        entry = repo.create(
            domain="payment",
            failure_type="timeout",
            error_message="Request timeout",
            recommended_action="manual_check",
        )

        repo.update_status(
            entry.id,
            FailedOperationStatus.PENDING.value,
            resolution_note="retry queued",
        )

        updated = repo.get_by_id(entry.id)
        assert updated.recommended_action == "manual_check"

    def test_increment_retry_count(self, repo):
        """Test incrementing the retry count."""
        entry = repo.create(
            domain="payment",
            failure_type="network_error",
            error_message="Connection refused",
        )

        assert entry.retry_count == 0

        result = repo.increment_retry_count(entry.id)
        assert result is True

        updated = repo.get_by_id(entry.id)
        assert updated.retry_count == 1
        assert updated.last_retry_at is not None

        repo.increment_retry_count(entry.id)
        updated = repo.get_by_id(entry.id)
        assert updated.retry_count == 2

    def test_thread_safety(self, repo):
        """Test thread safety with concurrent operations."""
        results = []
        errors = []

        def create_operation(n):
            try:
                entry = repo.create(
                    domain="payment",
                    failure_type=f"error_{n}",
                    error_message=f"Error message {n}",
                )
                results.append(entry.id)
            except Exception as e:
                errors.append(str(e))

        threads = []
        for i in range(50):
            t = threading.Thread(target=create_operation, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(set(results)) == 50
        # 538 D1: ids are opaque strings ("1".."50"); compare as a set.
        assert set(results) == {str(i) for i in range(1, 51)}


class TestInMemoryCreateExpiresAtBehavior:
    """Behavior: create() accepts and stores expires_at field."""

    @pytest.fixture
    def repo(self):
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        return InMemoryFailedOperationRepository()

    def test_create_with_expires_at_sets_field(self, repo):
        """expires_at value is stored on the created entry."""
        from datetime import datetime

        expires = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
        entry = repo.create(
            domain="payment",
            failure_type="timeout",
            expires_at=expires,
        )

        assert entry.expires_at == expires

    def test_create_without_expires_at_defaults_to_none(self, repo):
        """Omitting expires_at leaves it as None."""
        entry = repo.create(
            domain="payment",
            failure_type="timeout",
        )

        assert entry.expires_at is None

    def test_create_with_expires_at_persisted_on_get(self, repo):
        """expires_at is retrievable via get_by_id."""
        from datetime import datetime

        expires = datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC)
        created = repo.create(
            domain="payment",
            failure_type="timeout",
            expires_at=expires,
        )

        retrieved = repo.get_by_id(created.id)
        assert retrieved.expires_at == expires


class TestInMemoryCountArchivedOlderThanBehavior:
    """Behavior: count_archived_older_than filters by status and resolved_at."""

    @pytest.fixture(autouse=True)
    def _freeze_now(self):
        from unittest.mock import patch as _patch

        with (
            _patch("baldur.adapters.memory.base._now", return_value=FIXED_NOW),
            _patch(
                "baldur.adapters.memory.failed_operation._now", return_value=FIXED_NOW
            ),
        ):
            yield

    @pytest.fixture
    def repo(self):
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        return InMemoryFailedOperationRepository()

    def _create_archived_entry(self, repo, resolved_days_ago: int) -> None:
        from dataclasses import replace

        entry = repo.create(domain="payment", failure_type="timeout")
        repo.update_status(entry.id, FailedOperationStatus.RESOLVED.value)
        repo.update_status(entry.id, FailedOperationStatus.ARCHIVED.value)

        stored = repo._storage[entry.id]
        repo._storage[entry.id] = replace(
            stored, resolved_at=FIXED_NOW - timedelta(days=resolved_days_ago)
        )

    def test_count_zero_when_no_archived_entries(self, repo):
        """Empty repository returns 0."""
        assert repo.count_archived_older_than(30) == 0

    def test_count_excludes_recent_archived_entries(self, repo):
        """Archived entries resolved recently are not counted."""
        self._create_archived_entry(repo, resolved_days_ago=10)

        assert repo.count_archived_older_than(30) == 0

    def test_count_includes_old_archived_entries(self, repo):
        """Archived entries resolved long ago are counted."""
        self._create_archived_entry(repo, resolved_days_ago=60)

        assert repo.count_archived_older_than(30) == 1

    def test_count_boundary_exact_day_not_counted(self, repo):
        """Entry at exactly the boundary is not older than N days."""
        self._create_archived_entry(repo, resolved_days_ago=30)

        assert repo.count_archived_older_than(30) == 0

    def test_count_boundary_one_day_beyond_is_counted(self, repo):
        """Entry one day beyond the boundary is counted."""
        self._create_archived_entry(repo, resolved_days_ago=31)

        assert repo.count_archived_older_than(30) == 1

    def test_count_ignores_non_archived_status(self, repo):
        """Non-ARCHIVED entries are never counted."""

        entry = repo.create(domain="payment", failure_type="timeout")
        repo.update_status(entry.id, FailedOperationStatus.RESOLVED.value)

        assert repo.count_archived_older_than(0) == 0


# =============================================================================
# 778 D10 — the population overflow eviction draws its candidates from
# =============================================================================

# Twin of ``TestSQLDlqEvictionPopulationBehavior`` in
# tests/unit/adapters/sql/test_failed_operation.py — same
# fixture shape, same assertions. Both adapters had the same divergence and
# both were narrowed to the population the Redis adapter gets for free, so
# the two suites are deliberately readable side by side.


def _mixed_status_population(repo, domain="payment"):
    """Seed one entry per interesting status, oldest first.

    The order matters: the two entries eviction must never spend its batch
    on — an archived one and an in-flight one — are the two *oldest*, which
    is exactly the shape that used to neutralize the cap.
    """
    created = {}
    plan = [
        ("archived", "2026-04-14 10:00:00", FailedOperationStatus.ARCHIVED.value),
        ("replaying", "2026-04-14 10:01:00", FailedOperationStatus.REPLAYING.value),
        ("resolved", "2026-04-14 10:02:00", FailedOperationStatus.RESOLVED.value),
        ("reviewing", "2026-04-14 10:03:00", FailedOperationStatus.REVIEWING.value),
        ("rejected", "2026-04-14 10:04:00", FailedOperationStatus.REJECTED.value),
        ("pending_old", "2026-04-14 10:05:00", None),
        ("pending_new", "2026-04-14 10:06:00", None),
    ]
    for key, at, status in plan:
        with freeze_time(at):
            entry = repo.create(domain=domain, failure_type="timeout")
            if status is not None:
                repo.update_status(entry.id, status)
        created[key] = entry.id
    return created


class TestInMemoryDlqEvictionPopulationBehavior:
    """778 D10 — ``count_by_domain`` and ``get_oldest_ids`` now describe the
    same queue the size cap counts.

    ``count_all`` always excluded finished entries, but the candidate query
    ordered the whole store and the per-domain count had no status filter at
    all. On a store carrying enough old archived rows, eviction spent every
    batch on rows the count never included: the counted queue grew without
    bound while the cap reported itself enforced.
    """

    @pytest.fixture
    def repo(self):
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        return InMemoryFailedOperationRepository()

    def test_count_by_domain_excludes_finished_entries(self, repo):
        """The per-domain cap must not trip on entries resolved days ago."""
        _mixed_status_population(repo)

        # 7 entries seeded; the archived, resolved and rejected ones are
        # finished, leaving the 4 the cap is about.
        assert repo.count_by_domain("payment") == 4
        assert repo.count_by_domain("payment") == repo.count_all()

    def test_count_by_domain_is_scoped_to_its_domain(self, repo):
        """The narrowing did not cost the filter the method exists for."""
        _mixed_status_population(repo, domain="payment")
        repo.create(domain="notification", failure_type="smtp")

        assert repo.count_by_domain("payment") == 4
        assert repo.count_by_domain("notification") == 1

    def test_get_oldest_ids_excludes_finished_entries(self, repo):
        """Archived and resolved entries are not eviction candidates.

        Retention and purge own their removal; deleting one here would
        report an eviction that shrank nothing.
        """
        ids = _mixed_status_population(repo)

        candidates = repo.get_oldest_ids(count=10)

        assert ids["archived"] not in candidates
        assert ids["resolved"] not in candidates
        assert ids["rejected"] not in candidates

    def test_get_oldest_ids_excludes_in_flight_entries(self, repo):
        """A replaying or reviewing head must not crowd out the candidates.

        With candidates merely non-terminal, a batch window whose head was
        all REPLAYING returned zero evictions while evictable entries sat
        right behind it — and the caller reads zero as "the whole queue is
        in flight".
        """
        ids = _mixed_status_population(repo)

        candidates = repo.get_oldest_ids(count=10)

        assert ids["replaying"] not in candidates
        assert ids["reviewing"] not in candidates

    def test_get_oldest_ids_returns_the_evictable_oldest_first(self, repo):
        """Ordering still holds inside the narrowed population."""
        ids = _mixed_status_population(repo)

        assert repo.get_oldest_ids(count=10) == [
            ids["pending_old"],
            ids["pending_new"],
        ]

    def test_eviction_candidates_survive_a_protected_and_terminal_head(self, repo):
        """The single-candidate case, which is where the old shape failed.

        The five oldest entries are all non-evictable, so a batch of one used
        to come back holding an archived entry even though two evictable
        entries were waiting.
        """
        ids = _mixed_status_population(repo)

        assert repo.get_oldest_ids(count=1) == [ids["pending_old"]]

    def test_get_oldest_ids_applies_the_same_filter_per_domain(self, repo):
        """Domain-scoped candidates come from the domain-scoped population."""
        payment = _mixed_status_population(repo, domain="payment")
        _mixed_status_population(repo, domain="notification")

        candidates = repo.get_oldest_ids(count=10, domain="payment")

        assert candidates == [payment["pending_old"], payment["pending_new"]]

    def test_eviction_shrinks_the_number_the_cap_is_enforced_against(self, repo):
        """The property the whole narrowing exists to restore."""
        ids = _mixed_status_population(repo)
        counted_before = repo.count_all()

        evicted = repo.evict_oldest(count=10)

        assert evicted == 2
        assert repo.count_all() == counted_before - evicted
        assert repo.get_by_id(ids["pending_old"]) is None
        assert repo.get_by_id(ids["pending_new"]) is None
        # Finished and in-flight entries are all still there.
        for key in ("archived", "resolved", "rejected", "replaying", "reviewing"):
            assert repo.get_by_id(ids[key]) is not None

    def test_eviction_on_an_entirely_non_evictable_population_is_a_no_op(self, repo):
        """Nothing to evict is a legitimate answer, not a bug to route around.

        The soft-cap accept branch upstream reads zero as "the population is
        genuinely all protected or finished" — which is now true when it
        happens, because the candidate query already dropped everything else.
        """
        with freeze_time("2026-04-14 10:00:00"):
            archived = repo.create(domain="payment", failure_type="timeout")
            repo.update_status(archived.id, FailedOperationStatus.ARCHIVED.value)
        with freeze_time("2026-04-14 10:01:00"):
            replaying = repo.create(domain="payment", failure_type="timeout")
            repo.update_status(replaying.id, FailedOperationStatus.REPLAYING.value)

        assert repo.get_oldest_ids(count=10) == []
        assert repo.evict_oldest(count=10) == 0
        assert repo.get_by_id(archived.id) is not None
        assert repo.get_by_id(replaying.id) is not None
