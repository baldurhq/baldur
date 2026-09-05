"""Score-anchored replayable selection on the Redis DLQ store.

This is the adapter where the old selector could not reach a domain at all: it
fetched from the *global* pending index and post-filtered, so a domain whose
entries sit behind another domain's prefix was never selected — the drain
looked like it had nothing to do while the backlog grew. The walk now drives
off the index that owns the domain, and this file pins which index that is in
each of the three postures the store can be in (warm composite / cold pair /
degraded), because two of them carry entries of every status and therefore
need a status filter the third must not apply.

The fake backend preserves real score ordering and real ``ZRANGEBYSCORE``
offset/count semantics, and the blobs are decoded through the adapter's own
codec — a MagicMock would return whatever the test handed it and pass against
either index.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.redis.dlq_query import RedisDLQQuery
from baldur.interfaces.repositories import (
    REPLAY_SELECTION_MAX_SCAN,
    FailedOperationStatus,
    decode_replay_cursor,
    encode_replay_cursor,
)
from baldur.models.dlq import POLICY_CHAIN_CAPTURE_SOURCE

OPEN_CIRCUIT = "CIRCUIT_BREAKER_OPEN"
DOMAIN = "payment_api"
PENDING = FailedOperationStatus.PENDING.value
BASE = datetime(2026, 9, 5, 10, 0, 0, tzinfo=UTC)


def _warm_stub(result: bool) -> MagicMock:
    """Composite-warming accessor stand-in that records whether it was consulted.

    Wrapping a callable rather than handing back a bare mock keeps the stub
    answerable to a signature: a call shape the real accessor would reject is
    a call shape this stub rejects too.
    """
    return MagicMock(wraps=lambda *_args, **_kwargs: result)


def _make_repo(backend):
    """A repository whose read path is real and whose store is in-process."""
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
    repo.query = RedisDLQQuery(repo)
    backend.is_degraded = False
    return repo


def _seed(
    repo,
    entry_id,
    *,
    offset_seconds=0,
    domain=DOMAIN,
    failure_type=OPEN_CIRCUIT,
    source=POLICY_CHAIN_CAPTURE_SOURCE,
    status=PENDING,
    retry_count=0,
    created_at_present=True,
):
    """Write one entry blob and file it in every index the walk may drive off."""
    created_at = BASE + timedelta(seconds=offset_seconds)
    payload = {
        "id": entry_id,
        "domain": domain,
        "failure_type": failure_type,
        "status": status,
        "retry_count": retry_count,
        "max_retries": 5,
        "metadata": {} if source is None else {"source": source},
    }
    if created_at_present:
        payload["created_at"] = created_at.isoformat()
    repo._backend.set_blob(f"dlq:entry:{entry_id}", json.dumps(payload).encode("utf-8"))
    score = created_at.timestamp()
    if status == PENDING:
        repo._backend.zadd("dlq:pending", {entry_id: score})
        repo._backend.zadd(f"dlq:status_domain:pending:{domain}", {entry_id: score})
    repo._backend.zadd(f"dlq:by_domain:{domain}", {entry_id: score})
    return entry_id, created_at


@pytest.fixture
def repo(backend):
    return _make_repo(backend)


@pytest.fixture
def warm_repo(repo):
    """The normal posture: the (pending, domain) composite pair is warm."""
    repo.query._warm_composite_if_needed = _warm_stub(True)
    return repo


class TestRedisReplayableIndexRoutingBehavior:
    """Which index the walk drives off, and whether it still filters status."""

    def test_domain_scoped_walk_uses_the_warm_composite(self, warm_repo):
        key, needs_status_filter = warm_repo.query._replayable_index_key(DOMAIN)

        assert key == f"dlq:status_domain:pending:{DOMAIN}"
        assert needs_status_filter is False

    def test_undomained_walk_uses_the_global_pending_index(self, warm_repo):
        key, needs_status_filter = warm_repo.query._replayable_index_key(None)

        assert key == "dlq:pending"
        # The global pending index already IS the status scope.
        assert needs_status_filter is False

    def test_cold_composite_falls_back_to_the_legacy_domain_index(self, repo):
        repo.query._warm_composite_if_needed = _warm_stub(False)

        key, needs_status_filter = repo.query._replayable_index_key(DOMAIN)

        assert key == f"dlq:by_domain:{DOMAIN}"
        # The legacy ZSET carries every status, so the walk must re-check it.
        assert needs_status_filter is True

    def test_degraded_backend_falls_back_without_attempting_a_warmup(self, repo):
        """Warming the composite needs the raw client, which degraded mode
        does not have — asking for it would raise inside the selector."""
        repo._backend.is_degraded = True
        repo.query._warm_composite_if_needed = _warm_stub(True)

        key, needs_status_filter = repo.query._replayable_index_key(DOMAIN)

        assert key == f"dlq:by_domain:{DOMAIN}"
        assert needs_status_filter is True
        repo.query._warm_composite_if_needed.assert_not_called()

    def test_legacy_fallback_excludes_entries_of_another_status(self, repo):
        """The by_domain ZSET is status-blind; a REPLAYING entry selected from
        it would be handed to a second replayer."""
        repo.query._warm_composite_if_needed = _warm_stub(False)
        _seed(repo, "pending-1", offset_seconds=1)
        _seed(
            repo,
            "replaying-1",
            offset_seconds=2,
            status=FailedOperationStatus.REPLAYING.value,
        )

        page = repo.query.find_replayable_page(max_retries=5, domain=DOMAIN)

        assert [e.id for e in page.entries] == ["pending-1"]


class TestRedisReplayablePageBehavior:
    """Eligibility, the source residual, and the score-anchored cursor."""

    def test_page_returns_pending_entries_oldest_first(self, warm_repo):
        _seed(warm_repo, "c", offset_seconds=3)
        _seed(warm_repo, "a", offset_seconds=1)
        _seed(warm_repo, "b", offset_seconds=2)

        page = warm_repo.query.find_replayable_page(max_retries=5, domain=DOMAIN)

        assert [e.id for e in page.entries] == ["a", "b", "c"]

    def test_entry_at_or_past_max_retries_is_not_selectable(self, warm_repo):
        _seed(warm_repo, "eligible", offset_seconds=1, retry_count=2)
        _seed(warm_repo, "spent", offset_seconds=2, retry_count=3)

        page = warm_repo.query.find_replayable_page(max_retries=3, domain=DOMAIN)

        assert [e.id for e in page.entries] == ["eligible"]

    def test_failure_type_filter_excludes_other_types(self, warm_repo):
        _seed(warm_repo, "wanted", offset_seconds=1, failure_type="TIMEOUT")
        _seed(warm_repo, "other", offset_seconds=2, failure_type=OPEN_CIRCUIT)

        page = warm_repo.query.find_replayable_page(
            max_retries=5, domain=DOMAIN, failure_type="TIMEOUT"
        )

        assert [e.id for e in page.entries] == ["wanted"]

    def test_source_match_selects_only_that_capture_layer(self, warm_repo):
        _seed(warm_repo, "policy-chain", offset_seconds=1)
        _seed(warm_repo, "middleware", offset_seconds=2, source="middleware")

        page = warm_repo.query.find_replayable_page(
            max_retries=5, domain=DOMAIN, source=POLICY_CHAIN_CAPTURE_SOURCE
        )

        assert [e.id for e in page.entries] == ["policy-chain"]

    def test_entry_without_a_source_key_is_excluded_by_a_source_filter(self, warm_repo):
        _seed(warm_repo, "unsourced", offset_seconds=1, source=None)

        page = warm_repo.query.find_replayable_page(
            max_retries=5, domain=DOMAIN, source=POLICY_CHAIN_CAPTURE_SOURCE
        )

        assert page.entries == []

    def test_entry_with_no_created_at_is_skipped_rather_than_returned(self, warm_repo):
        """The cursor is built from that field, so returning the entry would
        advance the walk past a position it cannot encode."""
        _seed(warm_repo, "positionless", offset_seconds=1, created_at_present=False)
        _seed(warm_repo, "positioned", offset_seconds=2)

        page = warm_repo.query.find_replayable_page(max_retries=5, domain=DOMAIN)

        assert [e.id for e in page.entries] == ["positioned"]

    def test_limit_truncates_to_the_oldest_matches(self, warm_repo):
        _seed(warm_repo, "a", offset_seconds=1)
        _seed(warm_repo, "b", offset_seconds=2)
        _seed(warm_repo, "c", offset_seconds=3)

        page = warm_repo.query.find_replayable_page(
            max_retries=5, domain=DOMAIN, limit=2
        )

        assert [e.id for e in page.entries] == ["a", "b"]

    def test_cursor_resumes_strictly_after_the_position_it_names(self, warm_repo):
        _seed(warm_repo, "a", offset_seconds=1)
        _seed(warm_repo, "b", offset_seconds=2)
        _seed(warm_repo, "c", offset_seconds=3)

        first = warm_repo.query.find_replayable_page(
            max_retries=5, domain=DOMAIN, limit=1
        )
        second = warm_repo.query.find_replayable_page(
            max_retries=5, domain=DOMAIN, limit=10, cursor=first.next_cursor
        )

        assert [e.id for e in first.entries] == ["a"]
        assert [e.id for e in second.entries] == ["b", "c"]

    def test_same_score_entries_are_separated_by_the_id_half(self, warm_repo):
        """A score floor cannot express the id half, so the floor is inclusive
        and the exact position comparison happens on the decoded blob."""
        _seed(warm_repo, "a", offset_seconds=1)
        _seed(warm_repo, "b", offset_seconds=1)
        _seed(warm_repo, "c", offset_seconds=1)

        first = warm_repo.query.find_replayable_page(
            max_retries=5, domain=DOMAIN, limit=1
        )
        second = warm_repo.query.find_replayable_page(
            max_retries=5, domain=DOMAIN, limit=10, cursor=first.next_cursor
        )

        assert [e.id for e in first.entries] == ["a"]
        assert [e.id for e in second.entries] == ["b", "c"]

    def test_cursor_past_the_end_returns_nothing_and_advances_no_further(
        self, warm_repo
    ):
        _, last_at = _seed(warm_repo, "a", offset_seconds=1)

        past_end = warm_repo.query.find_replayable_page(
            max_retries=5,
            domain=DOMAIN,
            cursor=encode_replay_cursor(last_at, "a"),
        )

        assert past_end.entries == []
        assert past_end.next_cursor is None
        assert past_end.scan_exhausted is False

    def test_unusable_cursor_selects_from_the_beginning(self, warm_repo):
        _seed(warm_repo, "a", offset_seconds=1)

        page = warm_repo.query.find_replayable_page(
            max_retries=5, domain=DOMAIN, cursor="not-a-cursor"
        )

        assert [e.id for e in page.entries] == ["a"]

    def test_walking_a_backlog_page_by_page_reaches_every_entry_once(self, warm_repo):
        for i in range(1, 8):
            _seed(warm_repo, f"e{i}", offset_seconds=i)

        collected: list[str] = []
        cursor: str | None = None
        for _ in range(8):
            page = warm_repo.query.find_replayable_page(
                max_retries=5, domain=DOMAIN, limit=2, cursor=cursor
            )
            if not page.entries:
                break
            collected.extend(e.id for e in page.entries)
            cursor = page.next_cursor

        assert collected == [f"e{i}" for i in range(1, 8)]


class TestRedisReplayableDomainReachBehavior:
    """The starvation shape this selector exists to remove."""

    def test_domain_rows_far_past_the_limit_are_still_reached(self, warm_repo):
        """The old selector fetched a small multiple of ``limit`` and
        post-filtered, so matches behind a long prefix of non-matches were
        never returned however many times the lane ran."""
        for i in range(1, 401):
            _seed(warm_repo, f"noise-{i:04d}", offset_seconds=i, source="middleware")
        for i in range(1, 6):
            _seed(warm_repo, f"wanted-{i}", offset_seconds=500 + i)

        page = warm_repo.query.find_replayable_page(
            max_retries=5,
            domain=DOMAIN,
            source=POLICY_CHAIN_CAPTURE_SOURCE,
            limit=10,
        )

        assert [e.id for e in page.entries] == [f"wanted-{i}" for i in range(1, 6)]
        assert page.scan_exhausted is False

    def test_walk_stops_on_its_scan_bound_and_says_so(self, warm_repo):
        """One window can legitimately hold no match, so an empty page means
        neither "drained" nor "give up" on its own."""
        for i in range(1, REPLAY_SELECTION_MAX_SCAN + 2):
            _seed(warm_repo, f"noise-{i:06d}", offset_seconds=i, source="middleware")

        page = warm_repo.query.find_replayable_page(
            max_retries=5,
            domain=DOMAIN,
            source=POLICY_CHAIN_CAPTURE_SOURCE,
            limit=10,
        )

        assert page.entries == []
        assert page.scan_exhausted is True
        position = decode_replay_cursor(page.next_cursor)
        assert position is not None
        assert position[1] == f"noise-{REPLAY_SELECTION_MAX_SCAN:06d}"
