"""Unit tests for the composite-aware atomic CAS replay path (544 D4).

``RedisDLQLifecycle._try_acquire_atomic`` is the sole writer for the
PENDING -> REPLAYING transition (it bypasses ``RedisDLQRepository._update``'s
index maintenance, per the invariant comment at ``dlq_lifecycle.py:158-160``).
544 D4 extends the existing WATCH/MULTI block so the composite
``(status, domain)`` ZSET maintenance is atomic alongside the per-status
PENDING -> REPLAYING transition:

    pipe.multi()
    pipe.zrem(full_pending_key, str(id))
    pipe.set(full_entry_key, new_blob)
    pipe.zadd(full_replaying_key, {str(id): replaying_score})
    pipe.zrem(full_composite_pending, str(id))
    pipe.zadd(full_composite_replaying, {str(id): replaying_score})
    pipe.execute()

The score on both composite ops is the ``created_at`` epoch (matching the
541 D6 cross-status convention), NOT the transition time, so per-status
find stays "recently created" rather than "recently transitioned".

Test classes:
    TestAtomicAcquireCompositeBehavior -- composite zrem / zadd are issued
        inside the MULTI block alongside the existing PENDING / REPLAYING /
        SET ops; full keys are derived from the entry's domain.
    TestAtomicAcquireScoreConsistency -- the composite REPLAYING zadd uses
        the same created_at-epoch score as the per-status REPLAYING zadd
        (no transition-time leak).
    TestAtomicAcquireDomainHandling -- missing domain skips composite ops
        without breaking the per-status transition (degenerate-entry safety).
    TestAtomicAcquireWatchRetry -- WATCH conflict (EXEC returns None)
        re-applies all five composite-aware ops on the retry pass.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.redis.dlq_lifecycle import RedisDLQLifecycle
from baldur.interfaces.repositories import FailedOperationStatus

# =============================================================================
# Helpers
# =============================================================================


_PENDING = FailedOperationStatus.PENDING.value
_REPLAYING = FailedOperationStatus.REPLAYING.value


def _sample_entry(**overrides):
    base = {
        "id": "e1",
        "domain": "payment",
        "failure_type": "PG_TIMEOUT",
        "error_message": "boom",
        "status": _PENDING,
        "retry_count": 0,
        "max_retries": 3,
        "created_at": "2026-01-01T10:00:00+00:00",
        "updated_at": "2026-01-01T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _encode_entry(entry: dict) -> bytes:
    """Encode an entry as plain JSON bytes -- matches the in-tree codec
    for compression-off path used by other lifecycle tests."""
    return json.dumps(entry).encode("utf-8")


def _make_lifecycle(
    *,
    blob_after_watch: bytes | None,
    exec_result=("zrem-ok", "set-ok", 1, 1, 1),
):
    """Wire a RedisDLQLifecycle with a mock repo + mock pipeline that
    records the calls made inside the MULTI block.

    The exec_result tuple length matches the new 5-op MULTI (zrem PENDING,
    set blob, zadd REPLAYING, zrem composite_pending, zadd composite_replaying).
    """
    repo = MagicMock()
    repo._key_prefix = "dlq:"
    repo._pending_key = "dlq:pending"
    repo.PENDING_KEY = "dlq:pending"
    repo._make_key = MagicMock(side_effect=lambda eid: f"dlq:entry:{eid}")
    repo._status_key = MagicMock(side_effect=lambda s: f"dlq:status:{s}")
    repo._status_domain_key = MagicMock(
        side_effect=lambda s, d: f"dlq:status_domain:{s}:{d}"
    )
    repo._backend._get_full_key = MagicMock(side_effect=lambda k: k)
    repo._ensure_redis_available = MagicMock(return_value=True)
    # Use the real codec for the WATCH-loop decode path so the lifecycle
    # exercises the actual blob format.
    repo._compression_enabled = MagicMock(return_value=False)
    repo._encode_entry = RedisDLQRepository._encode_entry.__get__(repo)
    repo._decode_entry = RedisDLQRepository._decode_entry.__get__(repo)
    repo._to_data = MagicMock(side_effect=lambda d: d)

    pipe = MagicMock()
    pipe.get.return_value = blob_after_watch
    pipe.execute.return_value = exec_result

    pipeline_ctx = MagicMock()
    pipeline_ctx.__enter__.return_value = pipe
    pipeline_ctx.__exit__.return_value = False

    raw_client = MagicMock()
    raw_client.pipeline.return_value = pipeline_ctx
    repo._raw_redis_client = raw_client

    lifecycle = RedisDLQLifecycle(repo)
    return lifecycle, repo, pipe


# =============================================================================
# Composite zrem / zadd inside MULTI block
# =============================================================================


class TestAtomicAcquireCompositeBehavior:
    """Composite ops are issued inside the WATCH/MULTI block alongside the
    existing PENDING / REPLAYING / SET ops (D4 atomicity contract)."""

    def test_acquire_issues_composite_zrem_pending_and_zadd_replaying(self):
        entry = _sample_entry()
        blob = _encode_entry(entry)
        lifecycle, _, pipe = _make_lifecycle(blob_after_watch=blob)

        result = lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[])

        assert result is not None
        # Composite zrem on PENDING + zadd on REPLAYING, with the entry's domain.
        pipe.zrem.assert_any_call("dlq:status_domain:pending:payment", "10")
        composite_zadd_calls = [
            c
            for c in pipe.zadd.call_args_list
            if c.args[0] == "dlq:status_domain:replaying:payment"
        ]
        assert len(composite_zadd_calls) == 1

    def test_composite_ops_buffered_before_execute(self):
        """The five MULTI ops (zrem PENDING + set + zadd REPLAYING + zrem
        composite_pending + zadd composite_replaying) all buffer before the
        single .execute() call -- proves atomicity inside one MULTI block."""
        entry = _sample_entry()
        lifecycle, _, pipe = _make_lifecycle(blob_after_watch=_encode_entry(entry))

        lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[])

        # multi() called once.
        assert pipe.multi.call_count == 1
        # zrem called twice (PENDING + composite_pending).
        assert pipe.zrem.call_count == 2
        # set called once (blob).
        assert pipe.set.call_count == 1
        # zadd called twice (REPLAYING + composite_replaying).
        assert pipe.zadd.call_count == 2
        # execute called exactly once.
        assert pipe.execute.call_count == 1

    def test_composite_ops_target_correct_full_keys(self):
        """Each composite op targets the full prefixed key derived via
        backend._get_full_key + repo._status_domain_key."""
        entry = _sample_entry(domain="auth")
        lifecycle, _, pipe = _make_lifecycle(blob_after_watch=_encode_entry(entry))

        lifecycle._try_acquire_atomic(42, max_retries=3, domain_out=[])

        # composite_pending zrem key.
        pipe.zrem.assert_any_call("dlq:status_domain:pending:auth", "42")
        # composite_replaying zadd key.
        composite_zadd_calls = [
            c
            for c in pipe.zadd.call_args_list
            if c.args[0] == "dlq:status_domain:replaying:auth"
        ]
        assert len(composite_zadd_calls) == 1


# =============================================================================
# Score consistency -- created_at epoch, not transition time
# =============================================================================


class TestAtomicAcquireScoreConsistencyBehavior:
    """Composite REPLAYING zadd score equals the per-status REPLAYING zadd
    score (both come from the created_at epoch, 541 D6 convention)."""

    def test_composite_zadd_score_equals_created_at_epoch(self):
        created_iso = "2026-01-01T10:00:00+00:00"
        expected_score = datetime.fromisoformat(created_iso).timestamp()
        entry = _sample_entry(created_at=created_iso)
        lifecycle, _, pipe = _make_lifecycle(blob_after_watch=_encode_entry(entry))

        lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[])

        # Find the composite_replaying zadd call.
        composite_zadd_calls = [
            c
            for c in pipe.zadd.call_args_list
            if c.args[0] == "dlq:status_domain:replaying:payment"
        ]
        assert len(composite_zadd_calls) == 1
        mapping = composite_zadd_calls[0].args[1]
        assert mapping == {"10": expected_score}

    def test_composite_and_per_status_zadds_share_the_same_score(self):
        """Per-status REPLAYING zadd and composite REPLAYING zadd carry
        identical scores -- a downstream zrevrange over either index
        produces identical ordering."""
        created_iso = "2026-01-01T10:00:00+00:00"
        entry = _sample_entry(created_at=created_iso)
        lifecycle, _, pipe = _make_lifecycle(blob_after_watch=_encode_entry(entry))

        lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[])

        per_status_zadd = next(
            c for c in pipe.zadd.call_args_list if c.args[0] == "dlq:status:replaying"
        )
        composite_zadd = next(
            c
            for c in pipe.zadd.call_args_list
            if c.args[0] == "dlq:status_domain:replaying:payment"
        )
        assert per_status_zadd.args[1] == composite_zadd.args[1]


# =============================================================================
# Domain-missing safety
# =============================================================================


class TestAtomicAcquireDomainHandlingBehavior:
    """Entries with an empty domain skip composite ops to avoid creating
    a ``dlq:status_domain:pending:`` registry-like key. The per-status
    PENDING -> REPLAYING transition still happens."""

    def test_empty_domain_skips_composite_ops(self):
        entry = _sample_entry(domain="")
        lifecycle, _, pipe = _make_lifecycle(blob_after_watch=_encode_entry(entry))

        result = lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[])

        assert result is not None
        # No composite zrem/zadd calls because domain is empty.
        zrem_keys = [c.args[0] for c in pipe.zrem.call_args_list]
        assert "dlq:status_domain:pending:" not in zrem_keys
        assert not any("status_domain" in k for k in zrem_keys)
        zadd_keys = [c.args[0] for c in pipe.zadd.call_args_list]
        assert not any("status_domain" in k for k in zadd_keys)
        # Per-status transition still happens (zrem PENDING + zadd REPLAYING).
        pipe.zrem.assert_any_call("dlq:pending", "10")
        zadd_keys = {c.args[0] for c in pipe.zadd.call_args_list}
        assert "dlq:status:replaying" in zadd_keys


# =============================================================================
# WATCH-conflict retry
# =============================================================================


class TestAtomicAcquireWatchRetryBehavior:
    """A WATCH conflict (EXEC returns None) re-applies all five composite-
    aware ops on the retry pass -- composite atomicity holds across retries."""

    def test_watch_conflict_retries_with_all_composite_ops(self):
        entry = _sample_entry()
        lifecycle, _, pipe = _make_lifecycle(blob_after_watch=_encode_entry(entry))
        # First execute() returns None (WATCH conflict); second returns the
        # 5-tuple success result.
        pipe.execute.side_effect = [None, ("ok",) * 5]

        result = lifecycle._try_acquire_atomic(10, max_retries=3, domain_out=[])

        assert result is not None
        assert pipe.execute.call_count == 2
        # Across the two attempts the composite zrem and composite zadd
        # were invoked twice (once per WATCH pass).
        composite_zrems = [
            c
            for c in pipe.zrem.call_args_list
            if c.args[0] == "dlq:status_domain:pending:payment"
        ]
        composite_zadds = [
            c
            for c in pipe.zadd.call_args_list
            if c.args[0] == "dlq:status_domain:replaying:payment"
        ]
        assert len(composite_zrems) == 2
        assert len(composite_zadds) == 2
