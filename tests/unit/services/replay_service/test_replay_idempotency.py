"""
Tests for DLQ Replay idempotency integration (395 A2).

Covers:
- IdempotencyKey.for_dlq_replay() key format
- ReplayResult.skipped_result() factory
- ReplayResult.skipped field default
- Batch aggregation 3-way branching
- 566 D6/G4: a cache-backed singleton gate makes a same-key replay SKIP
"""

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

from baldur.services.idempotency.models import IdempotencyKey
from baldur.services.replay_service.models import (
    BatchReplayResult,
    ReplayResult,
)

# =============================================================================
# IdempotencyKey.for_dlq_replay — Contract
# =============================================================================


class TestIdempotencyKeyForDlqReplayContract:
    """for_dlq_replay() 키 생성 계약 검증."""

    def test_key_contains_dlq_replay_action_type(self):
        """생성된 키에 'dlq_replay' action_type이 포함된다."""
        key = IdempotencyKey.for_dlq_replay(dlq_id=42, domain="payment", retry_count=1)
        assert "dlq_replay" in key.cache_key

    def test_key_contains_domain_and_dlq_id(self):
        """생성된 키에 domain과 dlq_id가 포함된다."""
        key = IdempotencyKey.for_dlq_replay(dlq_id=42, domain="payment", retry_count=1)
        assert "payment:42" in key.cache_key

    def test_different_retry_counts_produce_different_keys(self):
        """다른 retry_count는 다른 키를 생성한다."""
        key1 = IdempotencyKey.for_dlq_replay(dlq_id=42, domain="payment", retry_count=1)
        key2 = IdempotencyKey.for_dlq_replay(dlq_id=42, domain="payment", retry_count=2)
        assert key1.cache_key != key2.cache_key

    def test_same_params_produce_same_key(self):
        """동일 파라미터는 동일 키를 생성한다 (결정론적)."""
        key1 = IdempotencyKey.for_dlq_replay(dlq_id=42, domain="payment", retry_count=1)
        key2 = IdempotencyKey.for_dlq_replay(dlq_id=42, domain="payment", retry_count=1)
        assert key1.cache_key == key2.cache_key


# =============================================================================
# ReplayResult.skipped_result — Contract
# =============================================================================


class TestReplayResultSkippedContract:
    """ReplayResult.skipped_result() 팩토리 계약 검증."""

    def test_skipped_result_success_is_true(self):
        """skipped_result()의 success는 True이다."""
        result = ReplayResult.skipped_result(dlq_id=1, reason="duplicate")
        assert result.success is True

    def test_skipped_result_skipped_is_true(self):
        """skipped_result()의 skipped는 True이다."""
        result = ReplayResult.skipped_result(dlq_id=1, reason="duplicate")
        assert result.skipped is True

    def test_skipped_result_contains_reason_in_message(self):
        """skipped_result()의 message에 reason이 포함된다."""
        result = ReplayResult.skipped_result(dlq_id=1, reason="duplicate")
        assert "duplicate" in result.message

    def test_skipped_result_data_contains_skip_reason(self):
        """skipped_result()의 data에 skip_reason이 포함된다."""
        result = ReplayResult.skipped_result(dlq_id=1, reason="in_progress")
        assert result.data["skip_reason"] == "in_progress"

    def test_default_skipped_field_is_false(self):
        """ReplayResult의 skipped 기본값은 False이다."""
        result = ReplayResult(success=True, dlq_id=1)
        assert result.skipped is False

    def test_succeeded_result_skipped_is_false(self):
        """succeeded() 팩토리의 skipped는 False이다."""
        result = ReplayResult.succeeded(dlq_id=1)
        assert result.skipped is False

    def test_failed_result_skipped_is_false(self):
        """failed() 팩토리의 skipped는 False이다."""
        result = ReplayResult.failed(dlq_id=1, error="err")
        assert result.skipped is False


# =============================================================================
# Batch aggregation — 3-way branching (§8.4 Side Effects)
# =============================================================================


class TestBatchReplayThreeWayBranchBehavior:
    """Batch replay 결과 집계 3-way 분기 동작 검증."""

    def test_skipped_results_increment_skipped_count(self):
        """skipped 결과는 skipped_count를 증가시킨다."""
        batch = BatchReplayResult(total=3, results=[])

        results = [
            ReplayResult.succeeded(dlq_id=1),
            ReplayResult.skipped_result(dlq_id=2, reason="duplicate"),
            ReplayResult.failed(dlq_id=3, error="err"),
        ]

        for r in results:
            if r.skipped:
                batch.skipped_count += 1
            elif r.success:
                batch.success_count += 1
            else:
                batch.failed_count += 1

        assert batch.success_count == 1
        assert batch.skipped_count == 1
        assert batch.failed_count == 1


# =============================================================================
# 566 D6/G4 — real singleton gate makes a same-key replay SKIP
# =============================================================================


@dataclass
class _FakeOp:
    """Minimal FailedOperationData stand-in for the replay path."""

    id: int = 7
    domain: str = "payment"
    retry_count: int = 0
    status: str = "pending"
    request_data: dict = field(default_factory=dict)


class TestReplayDuplicateSkipBehavior:
    """Two replays with the same ``for_dlq_replay`` key dedup against a real cache.

    Pre-566 the singleton gate is ``cache=None`` (always CONTINUE), so a
    genuinely-duplicate DLQ replay always re-runs the handler. With the gate
    cache-backed (post-566) the first replay runs the handler and marks the key
    completed; the second carries the same ``for_dlq_replay(dlq_id, domain,
    retry_count)`` key, so the gate returns SKIP and the service returns
    ``skipped_result(reason="duplicate")`` without re-running side effects.
    """

    def setup_method(self):
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
        from baldur.core.idempotency_gate import (
            IdempotencyGate,
            configure_idempotency_gate,
        )
        from baldur.services.replay_service import _replay_handlers

        _replay_handlers.clear()
        # Configure the runtime-scoped singleton with a real in-process cache —
        # a unit-test stand-in for the init()-installed gate (Testability Notes).
        configure_idempotency_gate(
            IdempotencyGate(cache=InMemoryCacheAdapter(key_prefix="replay_dup_test:"))
        )

    def teardown_method(self):
        from baldur.core.idempotency_gate import reset_idempotency_gate
        from baldur.services.replay_service import _replay_handlers

        reset_idempotency_gate()
        _replay_handlers.clear()

    def test_second_same_key_replay_returns_duplicate_skip(self):
        """Same-key second replay → ``skipped_result(reason="duplicate")`` (D6/G4)."""
        from baldur.services.replay_service import (
            ReplayHandler,
            ReplayService,
            register_replay_handler,
        )

        # Given — a repository that always re-acquires the same entry (same
        # dlq_id + retry_count → same idempotency key) and a counting handler.
        op = _FakeOp(id=7, domain="payment", retry_count=0)
        repo = MagicMock()
        repo.try_acquire_for_replay.return_value = op
        repo.get_by_id.return_value = op
        repo.complete_replay.return_value = None

        replay_calls = {"n": 0}

        class _CountingHandler(ReplayHandler):
            @property
            def domain(self) -> str:
                return "payment"

            def can_replay(self, failed_op):
                return True, ""

            def replay(self, failed_op):
                replay_calls["n"] += 1
                return ReplayResult.succeeded(failed_op.id, "Replayed OK")

        register_replay_handler(_CountingHandler())
        service = ReplayService(repository=repo)

        # When — two replays of the same entry at the same retry_count.
        with patch("baldur.services.replay_service.service.log_dlq_replay_audit"):
            first = service._execute_replay(7)
            second = service._execute_replay(7)

        # Then — first runs the handler and succeeds; second is deduped to SKIP.
        assert first.skipped is False
        assert first.success is True
        assert second.skipped is True
        assert second.success is True
        assert "duplicate" in second.message
        # The handler ran exactly once — the duplicate never re-executed.
        assert replay_calls["n"] == 1
        # SKIP path records a duplicate_skip resolution on the repository.
        assert any(
            call.kwargs.get("resolution_type") == "duplicate_skip"
            or "duplicate_skip" in call.args
            for call in repo.complete_replay.call_args_list
        )
