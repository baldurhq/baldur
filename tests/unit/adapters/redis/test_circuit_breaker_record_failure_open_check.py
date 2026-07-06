"""656 D7 — RedisCircuitBreakerStateRepository.record_failure_with_open_check.

The override folds HMGET-state/opened_at + conditional HSET into a single
Lua ``EVAL``, so concurrent gunicorn workers / K8s replicas observing
HALF_OPEN see exactly one ``did_open=True`` per logical re-open — the
failure-side mirror of the #498 close-check, closing the HALF_OPEN->OPEN
multi-emit race the ABC's two-call default leaves open.

Unit tests here pin the Python wrapper's contract (return-array parsing
into ``CircuitBreakerOpenAttempt``, ``opened_at`` carry on the race-loser
branch, exception propagation, eval-call shape). Real-Redis multi-worker
atomicity is verified end-to-end by
``tests/integration/redis/test_cb_open_check_lua.py`` — fakeredis Lua
approximations cannot fully reproduce ``EVAL`` semantics under concurrency.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from baldur.adapters.redis.circuit_breaker import (
    _LUA_RECORD_FAILURE_WITH_OPEN_CHECK,
    RedisCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
)


def _make_repo(eval_return) -> tuple[RedisCircuitBreakerStateRepository, MagicMock]:
    """Construct a repo whose Redis ``eval`` returns the given Lua array.

    The Python wrapper reaches Redis via the backend's public
    ``raw_redis_client`` seam — exactly the entrypoint
    ``record_success_with_close_check`` uses.
    """
    backend = MagicMock()
    backend._get_full_key.side_effect = lambda key: f"baldur:{key}"
    backend.raw_redis_client = backend._redis._redis
    backend.raw_redis_client.eval.return_value = eval_return
    repo = RedisCircuitBreakerStateRepository(backend=backend)
    return repo, backend


# =============================================================================
# Contract — Lua return-array shape -> CircuitBreakerOpenAttempt mapping
# =============================================================================


class TestRedisRecordFailureWithOpenCheckContract:
    """Each Lua branch's return array maps to a specific attempt shape."""

    def test_half_open_open_branch_returns_did_open_true(self):
        # Lua re-open branch return: {1, 'open', now_iso}.
        repo, _backend = _make_repo([1, b"open", b"2026-06-22T10:00:00+00:00"])

        attempt = repo.record_failure_with_open_check("svc")

        assert isinstance(attempt, CircuitBreakerOpenAttempt)
        assert attempt.did_open is True
        assert attempt.state.state == "open"
        # did_open=True ⇒ opened_at is parsed (the writeback depends on it).
        assert attempt.state.opened_at is not None

    def test_open_race_loser_carries_existing_opened_at(self):
        # Lua race-loser branch: {0, 'open', <existing opened_at>}.
        repo, _backend = _make_repo([0, b"open", b"2026-06-22T09:00:00+00:00"])

        attempt = repo.record_failure_with_open_check("svc")

        # No re-open, but the existing OPEN-era opened_at is carried so the
        # Layered wrapper writes back L1=open without losing the timestamp.
        assert attempt.did_open is False
        assert attempt.state.state == "open"
        assert attempt.state.opened_at is not None

    def test_closed_state_returns_stale_sentinel(self):
        # Lua trust-L2 branch (a concurrent quorum closed): {0, 'closed', ''}.
        repo, _backend = _make_repo([0, b"closed", b""])

        attempt = repo.record_failure_with_open_check("svc")

        assert attempt.did_open is False
        assert attempt.state.state == "closed"
        assert attempt.state.opened_at is None

    def test_missing_hash_returns_stale_sentinel(self):
        # Lua else-branch when HMGET reports no hash: {0, 'missing', ''}.
        repo, _backend = _make_repo([0, b"missing", b""])

        attempt = repo.record_failure_with_open_check("svc")

        assert attempt.did_open is False
        assert attempt.state.state == "missing"
        assert attempt.state.opened_at is None

    def test_returned_state_data_uses_synthetic_defaults(self):
        # Per D7: auxiliary fields are synthesized rather than fetched in a
        # second RTT — callers read only did_open + state.state +
        # state.opened_at.
        repo, _backend = _make_repo([1, b"open", b"2026-06-22T10:00:00+00:00"])

        attempt = repo.record_failure_with_open_check("svc")

        assert isinstance(attempt.state, CircuitBreakerStateData)
        assert attempt.state.service_name == "svc"
        assert attempt.state.failure_count == 0
        assert attempt.state.success_count == 0
        assert attempt.state.half_open_request_count == 0
        assert attempt.state.manually_controlled is False
        assert attempt.state.metadata == {}


# =============================================================================
# Behavior — eval() dispatch shape + exception propagation
# =============================================================================


class TestRedisRecordFailureWithOpenCheckBehavior:
    """Wrapper-level behavior around the Lua eval call."""

    def test_eval_receives_lua_script_and_key(self):
        # Given: a repo whose Redis ``eval`` is captured.
        repo, backend = _make_repo([1, b"open", b"2026-06-22T10:00:00+00:00"])

        # When: the wrapper dispatches an open-check.
        repo.record_failure_with_open_check("payment")

        # Then: eval is called with the open-check Lua script, KEYS[1] = the
        # full-prefixed hash key, and ARGV[1] = now_iso.
        eval_mock = backend._redis._redis.eval
        eval_mock.assert_called_once()
        args = eval_mock.call_args.args
        assert args[0] is _LUA_RECORD_FAILURE_WITH_OPEN_CHECK
        assert args[1] == 1  # numkeys
        assert args[2] == "baldur:cb:payment"

    def test_eval_receives_now_iso_as_string(self):
        repo, backend = _make_repo([1, b"open", b"2026-06-22T10:00:00+00:00"])

        repo.record_failure_with_open_check("svc")

        now_iso = backend._redis._redis.eval.call_args.args[3]
        assert isinstance(now_iso, str)
        assert "T" in now_iso

    def test_eval_failure_propagates_after_warning_log(self):
        # The Lua override re-raises Redis errors so the Layered wrapper
        # (D7 step 5) can record degraded-mode and fall back to L1. The
        # warning event is the 1st-line operational hint — guard against
        # silent removal of the log call.
        repo, backend = _make_repo([1, b"open", b""])
        backend._redis._redis.eval.side_effect = ConnectionError("redis down")

        with capture_logs() as caplog:
            with pytest.raises(ConnectionError, match="redis down"):
                repo.record_failure_with_open_check("svc")

        assert any(
            entry.get("event") == "redis_cb_repo.record_failure_with_open_check_failed"
            and entry.get("log_level") == "warning"
            for entry in caplog
        )
