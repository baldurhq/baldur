"""498 D1 — RedisCircuitBreakerStateRepository.record_success_with_close_check.

The override folds HMGET-state/success_count + conditional HSET into a
single Lua ``EVAL``, so concurrent gunicorn workers / K8s replicas
observing HALF_OPEN see exactly one ``did_close=True`` per logical
recovery — closing the cross-process race the ABC's two-call default
leaves open (see #498 G1).

Unit tests here pin the Python wrapper's contract (return-array parsing
into ``CircuitBreakerCloseAttempt``, exception propagation, eval-call
shape). Real-Redis multi-worker atomicity is verified end-to-end by
``tests/integration/baldur/test_cb_half_open_lua_cas.py`` — fakeredis
Lua approximations cannot fully reproduce ``EVAL`` semantics under
concurrency (per #498 Test Assessment).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from baldur.adapters.redis.circuit_breaker import (
    _LUA_RECORD_SUCCESS_WITH_CLOSE_CHECK,
    RedisCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerCloseAttempt,
    CircuitBreakerStateData,
)


def _make_repo(eval_return) -> tuple[RedisCircuitBreakerStateRepository, MagicMock]:
    """Construct a repo whose Redis ``eval`` returns the given Lua array.

    The Python wrapper reaches Redis via the backend's public
    ``raw_redis_client`` seam — exactly the entrypoint
    ``try_acquire_half_open_slot`` uses.
    """
    backend = MagicMock()
    backend._get_full_key.side_effect = lambda key: f"baldur:{key}"
    # Point the public seam at the same mock the tests assert on.
    backend.raw_redis_client = backend._redis._redis
    backend.raw_redis_client.eval.return_value = eval_return
    repo = RedisCircuitBreakerStateRepository(backend=backend)
    return repo, backend


# =============================================================================
# Contract — Lua return-array shape -> CircuitBreakerCloseAttempt mapping
# =============================================================================


class TestRedisRecordSuccessWithCloseCheckContract:
    """Each Lua branch's return array maps to a specific attempt shape."""

    def test_half_open_close_branch_returns_did_close_true(self):
        # Lua close-branch return: {1, 'closed', 0}.
        repo, _backend = _make_repo([1, b"closed", 0])

        attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        assert isinstance(attempt, CircuitBreakerCloseAttempt)
        assert attempt.did_close is True
        assert attempt.state.state == "closed"
        assert attempt.state.success_count == 0

    def test_half_open_increment_branch_returns_did_close_false(self):
        # Lua increment-branch return: {0, 'half_open', new_count}.
        repo, _backend = _make_repo([0, b"half_open", 1])

        attempt = repo.record_success_with_close_check("svc", success_threshold=3)

        assert attempt.did_close is False
        assert attempt.state.state == "half_open"
        assert attempt.state.success_count == 1

    def test_closed_state_returns_race_loser_sentinel(self):
        # Lua race-loser / post-crash-convergence branch: {0, 'closed', 0}.
        repo, _backend = _make_repo([0, b"closed", 0])

        attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        assert attempt.did_close is False
        assert attempt.state.state == "closed"
        assert attempt.state.success_count == 0

    def test_open_state_returns_stale_sentinel(self):
        # Lua else-branch (stale state ∉ {half_open, closed}): {0, 'open', 0}.
        repo, _backend = _make_repo([0, b"open", 0])

        attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        assert attempt.did_close is False
        assert attempt.state.state == "open"
        assert attempt.state.success_count == 0

    def test_missing_hash_returns_stale_sentinel(self):
        # Lua else-branch when HMGET reports no hash: {0, 'missing', 0}.
        repo, _backend = _make_repo([0, b"missing", 0])

        attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        assert attempt.did_close is False
        assert attempt.state.state == "missing"

    def test_returned_state_data_uses_synthetic_defaults(self):
        # Per D2: auxiliary fields are synthesized rather than fetched in a
        # second RTT — callers read only did_close + state.state +
        # state.success_count.
        repo, _backend = _make_repo([1, b"closed", 0])

        attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        assert isinstance(attempt.state, CircuitBreakerStateData)
        assert attempt.state.service_name == "svc"
        assert attempt.state.failure_count == 0
        assert attempt.state.opened_at is None
        assert attempt.state.half_open_request_count == 0
        assert attempt.state.manually_controlled is False
        assert attempt.state.metadata == {}


# =============================================================================
# Behavior — eval() dispatch shape + exception propagation
# =============================================================================


class TestRedisRecordSuccessWithCloseCheckBehavior:
    """Wrapper-level behavior around the Lua eval call."""

    def test_eval_receives_lua_script_and_threshold(self):
        # Given: a repo whose Redis ``eval`` is captured.
        repo, backend = _make_repo([0, b"half_open", 1])

        # When: the wrapper dispatches a close-check.
        repo.record_success_with_close_check("payment", success_threshold=5)

        # Then: eval is called with the close-check Lua script, KEYS[1] = the
        # full-prefixed hash key, and ARGV[1] = the threshold (now_iso is
        # ARGV[2] — checked separately).
        eval_mock = backend._redis._redis.eval
        eval_mock.assert_called_once()
        args = eval_mock.call_args.args
        assert args[0] is _LUA_RECORD_SUCCESS_WITH_CLOSE_CHECK
        assert args[1] == 1  # numkeys
        assert args[2] == "baldur:cb:payment"
        assert args[3] == 5

    def test_eval_receives_now_iso_as_string(self):
        repo, backend = _make_repo([0, b"half_open", 1])

        repo.record_success_with_close_check("svc", success_threshold=2)

        now_iso = backend._redis._redis.eval.call_args.args[4]
        assert isinstance(now_iso, str)
        # ISO-8601 UTC: looks like "2026-05-12T..." — guard against accidental
        # change to e.g. unix-seconds.
        assert "T" in now_iso

    def test_eval_failure_propagates_after_warning_log(self):
        # The race-unsafe ABC default would mask Redis errors as a successful
        # increment+update_state pair; the Lua override re-raises so the
        # Layered wrapper (D6 step 5) can record degraded-mode. The warning
        # event is the 1st-line operational hint for the D7 degraded-mode
        # counter, so guard against silent removal of the log call.
        repo, backend = _make_repo([0, b"half_open", 1])
        backend._redis._redis.eval.side_effect = ConnectionError("redis down")

        with capture_logs() as caplog:
            with pytest.raises(ConnectionError, match="redis down"):
                repo.record_success_with_close_check("svc", success_threshold=2)

        assert any(
            entry.get("event") == "redis_cb_repo.record_success_with_close_check_failed"
            and entry.get("log_level") == "warning"
            for entry in caplog
        )

    def test_state_decoded_when_returned_as_string(self):
        # Some Redis client builds (newer redis-py with decode_responses=True)
        # return str rather than bytes. The wrapper must handle both.
        repo, _backend = _make_repo([1, "closed", 0])

        attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        assert attempt.did_close is True
        assert attempt.state.state == "closed"

    def test_did_close_zero_returns_false_boolean(self):
        # Lua arrays of integers — must not leak a Python truthy int.
        repo, _backend = _make_repo([0, b"half_open", 2])

        attempt = repo.record_success_with_close_check("svc", success_threshold=5)

        assert attempt.did_close is False
        assert isinstance(attempt.did_close, bool)
