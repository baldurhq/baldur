"""Redis ``cas_dict_field`` Lua-semantics integration tests (491 D3).

The unit-test layer (``tests/unit/adapters/cache/test_redis_cas_dict_field.py``)
mocks ``LuaScriptRegistry.execute`` and only asserts the adapter's call
shape (script name, KEYS / ARGV layout, return-value mapping). The
production-correctness aspects below require a real Redis server:

A. ``cjson.decode`` roundtrip with orjson-encoded payloads (unicode,
   emoji, large blob, non-table primitive)
B. ``SET PX`` TTL applied atomically inside the same EVAL
C. ``EVALSHA`` + ``NOSCRIPT`` auto-recovery after ``SCRIPT FLUSH``
D. Concurrent CAS race — exactly-once semantics under contention

All tests require a running Redis instance (auto-skipped via
``pytestmark = pytest.mark.requires_redis``).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter

pytestmark = pytest.mark.requires_redis


_PREFIX = "test:cas:"


@pytest.fixture
def cache(redis_url) -> RedisCacheAdapter:
    """RedisCacheAdapter with a static prefix for cas_dict_field tests."""
    return RedisCacheAdapter(
        url=redis_url,
        key_prefix=_PREFIX,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
    )


# =============================================================================
# A. cjson.decode roundtrip — payload variants
# =============================================================================


class TestCasDictFieldCjsonRoundtrip:
    """Lua's ``cjson.decode`` must roundtrip orjson-serialized records."""

    def test_executing_to_completed_transition_succeeds(self, cache):
        """
        Purpose:
            Verify the production hot-path: pre-populated ``status="executing"``
            record CAS-transitions to ``status="completed"`` atomically.
        Expected:
            - Returns True (CAS won)
            - Final stored value equals the new dict byte-for-byte
        """
        cache.set("order:abc", {"status": "executing", "retry_count": 0})

        ok = cache.cas_dict_field(
            "order:abc",
            "status",
            "executing",
            {"status": "completed", "retry_count": 0, "result": {"ok": True}},
        )

        assert ok is True
        assert cache.get("order:abc") == {
            "status": "completed",
            "retry_count": 0,
            "result": {"ok": True},
        }

    def test_field_mismatch_returns_false_and_preserves_value(self, cache):
        """
        Purpose:
            CAS where ``expected`` doesn't match the current field value
            (Lua branch ``rec[ARGV[1]] ~= ARGV[2]``) → returns False and
            performs no SET.
        Expected:
            - Returns False
            - Stored value is unchanged
        """
        original = {"status": "completed", "retry_count": 1}
        cache.set("order:done", original)

        ok = cache.cas_dict_field(
            "order:done",
            "status",
            "executing",
            {"status": "failed"},
        )

        assert ok is False
        assert cache.get("order:done") == original

    def test_missing_key_returns_false_without_creating(self, cache):
        """
        Purpose:
            CAS on a non-existent key (Lua branch ``not raw``) → returns False
            and does NOT create the key (no fall-through to SET).
        Expected:
            - Returns False
            - Key still does not exist after the call
        """
        ok = cache.cas_dict_field(
            "order:missing",
            "status",
            "executing",
            {"status": "completed"},
        )

        assert ok is False
        assert cache.get("order:missing") is None

    def test_non_table_record_returns_false(self, cache):
        """
        Purpose:
            CAS where the stored value is a JSON primitive (string), not an
            object — Lua branch ``type(rec) ~= 'table'`` → returns False.
        Expected:
            - Returns False
            - Original primitive value preserved
        """
        cache.set("legacy:string", "raw_string_value")

        ok = cache.cas_dict_field(
            "legacy:string",
            "status",
            "executing",
            {"status": "completed"},
        )

        assert ok is False
        assert cache.get("legacy:string") == "raw_string_value"

    def test_unicode_field_value_roundtrips(self, cache):
        """
        Purpose:
            ``expected`` and ``new_value`` containing multi-byte UTF-8 (Korean)
            survive the orjson → cjson.decode → Lua string-compare → SET PX
            roundtrip. Lua compares byte sequences, so UTF-8 must be preserved
            end-to-end.
        Expected:
            - CAS succeeds with unicode ``expected`` matching
            - Final value preserves all unicode characters
        """
        cache.set("order:korean", {"status": "실행중", "메모": "한글 테스트"})

        ok = cache.cas_dict_field(
            "order:korean",
            "status",
            "실행중",
            {"status": "완료", "결과": "성공"},
        )

        assert ok is True
        assert cache.get("order:korean") == {"status": "완료", "결과": "성공"}

    def test_emoji_in_value_roundtrips(self, cache):
        """
        Purpose:
            4-byte UTF-8 (emoji) inside ``new_value`` roundtrips through
            cjson.decode + SET PX without corruption.
        Expected:
            - CAS succeeds, final value preserves emoji bytes exactly
        """
        cache.set("order:emoji", {"status": "executing", "tag": "🔥💥"})

        ok = cache.cas_dict_field(
            "order:emoji",
            "status",
            "executing",
            {"status": "completed", "result": "✅ done"},
        )

        assert ok is True
        assert cache.get("order:emoji") == {
            "status": "completed",
            "result": "✅ done",
        }

    def test_large_payload_roundtrips(self, cache):
        """
        Purpose:
            ~10 KB payload in ``new_value`` survives the EVAL boundary
            (ARGV size + cjson.decode CPU + SET PX).
        Expected:
            - CAS succeeds, final value byte-equal to source
        """
        big_blob = {"items": [{"i": i, "v": "x" * 100} for i in range(100)]}
        cache.set("order:large", {"status": "executing"})

        ok = cache.cas_dict_field(
            "order:large",
            "status",
            "executing",
            {"status": "completed", "result": big_blob},
        )

        assert ok is True
        assert cache.get("order:large") == {
            "status": "completed",
            "result": big_blob,
        }


# =============================================================================
# B. TTL semantics — SET PX applied atomically
# =============================================================================


class TestCasDictFieldTtl:
    """``SET PX`` runs inside the same EVAL — TTL is atomic with the CAS."""

    def test_ttl_applied_atomically_on_cas(self, cache):
        """
        Purpose:
            ``ttl=1s`` causes the key to expire after the CAS write — TTL is
            applied inside the same EVAL, not via a follow-up PEXPIRE round-trip.
        Expected:
            - Value present immediately after CAS
            - Key gone after TTL elapses (~1.5s wait)
        """
        cache.set("order:ttl", {"status": "executing"})

        ok = cache.cas_dict_field(
            "order:ttl",
            "status",
            "executing",
            {"status": "completed"},
            ttl=timedelta(seconds=1),
        )

        assert ok is True
        assert cache.get("order:ttl") == {"status": "completed"}

        time.sleep(1.5)
        assert cache.get("order:ttl") is None

    def test_no_ttl_leaves_key_persistent(self, cache, redis_test_client):
        """
        Purpose:
            ``ttl=None`` → Lua falls through to plain ``SET`` (no PX argument).
            The CAS write must NOT inherit any TTL from a prior set — the key
            remains persistent.
        Expected:
            - PTTL on the prefixed key returns -1 (key exists, no expiration)
        """
        cache.set("order:no_ttl", {"status": "executing"})

        ok = cache.cas_dict_field(
            "order:no_ttl",
            "status",
            "executing",
            {"status": "completed"},
            ttl=None,
        )

        assert ok is True
        # Redis PTTL: -1 = key exists with no TTL; -2 = key does not exist.
        assert redis_test_client.pttl(f"{_PREFIX}order:no_ttl") == -1


# =============================================================================
# C. EVALSHA + NOSCRIPT auto-reload
# =============================================================================


class TestCasDictFieldEvalshaRecovery:
    """``LuaScriptRegistry`` MUST recover from ``SCRIPT FLUSH`` transparently."""

    def test_first_call_loads_script_and_caches_sha(self, cache):
        """
        Purpose:
            First ``cas_dict_field`` call lazy-inits the registry, performs
            SCRIPT LOAD + EVALSHA, and caches the returned SHA1.
        Expected:
            - Call succeeds
            - Registry instance exists with the script's SHA cached
        """
        cache.set("order:first", {"status": "executing"})

        ok = cache.cas_dict_field(
            "order:first",
            "status",
            "executing",
            {"status": "completed"},
        )

        assert ok is True
        registry = cache._lua_registry
        assert registry is not None
        assert "idempotency_cas_dict_field" in registry._sha_cache

    def test_recovers_from_script_flush_via_noscript(self, cache, redis_test_client):
        """
        Purpose:
            After server-side ``SCRIPT FLUSH`` invalidates the cached SHA,
            the next ``cas_dict_field`` call must succeed transparently —
            the registry catches NOSCRIPT, re-loads the script body, and
            retries via fresh EVALSHA.
        Expected:
            - First CAS succeeds and warms the SHA cache
            - SCRIPT FLUSH wipes the server-side script cache
            - Second CAS still succeeds (recovery is transparent to caller)
            - Final state reflects the second CAS write
        """
        # Warm the SHA cache.
        cache.set("order:flush_warm", {"status": "executing"})
        assert (
            cache.cas_dict_field(
                "order:flush_warm",
                "status",
                "executing",
                {"status": "completed"},
            )
            is True
        )

        # Wipe the server-side script cache. The adapter's local SHA cache
        # is now stale — next EVALSHA will hit NOSCRIPT.
        redis_test_client.execute_command("SCRIPT", "FLUSH")

        # Second CAS — registry catches NOSCRIPT, reloads, and retries.
        cache.set("order:flush_test", {"status": "executing"})
        ok = cache.cas_dict_field(
            "order:flush_test",
            "status",
            "executing",
            {"status": "completed"},
        )

        assert ok is True
        assert cache.get("order:flush_test") == {"status": "completed"}


# =============================================================================
# D. Concurrent CAS race — exactly-once semantics
# =============================================================================


class TestCasDictFieldConcurrentRace:
    """Concurrent threads racing for the same CAS — exactly one wins."""

    def test_concurrent_cas_has_exactly_one_winner(self, cache):
        """
        Purpose:
            20 threads simultaneously CAS the same ``executing`` record to a
            distinct ``completed`` payload. Redis serializes EVAL execution,
            so exactly one CAS observes ``status == "executing"`` and wins;
            the other 19 observe ``status == "completed"`` and return False.
            This is the atomicity contract that motivates D8 (without it,
            ``mark_completed`` could not collapse from 2 RTT to 1 RTT).
        Expected:
            - Exactly 1 True, 19 False across all 20 calls
            - Final stored value matches the winning thread's payload
        """
        cache.set("order:race", {"status": "executing"})

        thread_count = 20
        results: list[bool] = []
        results_lock = threading.Lock()
        start_barrier = threading.Barrier(thread_count)

        def attempt(thread_id: int) -> bool:
            # Release all threads at once for maximum contention.
            start_barrier.wait()
            won = cache.cas_dict_field(
                "order:race",
                "status",
                "executing",
                {"status": "completed", "winner": thread_id},
            )
            with results_lock:
                results.append(won)
            return won

        with ThreadPoolExecutor(max_workers=thread_count) as pool:
            futures = [pool.submit(attempt, i) for i in range(thread_count)]
            for f in futures:
                f.result()

        winners = sum(1 for r in results if r)
        assert winners == 1, f"expected exactly 1 winner, got {winners}"

        final = cache.get("order:race")
        assert final["status"] == "completed"
        assert "winner" in final
        assert 0 <= final["winner"] < thread_count
