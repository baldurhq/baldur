"""Unit tests for ``RedisCacheAdapter.get_lock`` prefix isolation (#465).

Source: ``src/baldur/adapters/cache/redis_adapter.py``

Covers (#465 G1 / G5):

- ``RedisCacheAdapter.get_lock(name)`` resolves the full storage key once
  via ``self._make_key(name)`` and passes the result into the lock as
  ``full_key`` (D4).
- ``RedisDistributedLock`` writes the verbatim ``full_key`` to Redis on
  acquire / release / locked / owned / extend, with no in-class
  transformation (no ``f"lock:{name}"`` sentinel) — pins the post-#465
  contract for direct callers (audit / postmortem / integrity tasks).
- D1 snapshot invariant: the lock instance captures its full key at
  construction time. Toggling ``TestModeContext`` between construction
  and a subsequent operation does NOT shift the storage key.
- Tri-state ``key_prefix`` (``None`` / ``""`` / ``"static:"``) plus
  ``TestModeContext.start()`` plus ``BALDUR_NAMESPACE_REGION`` compose
  through to the lock-key shape — pinning the X-Test-Mode v1.0 PRO
  blocker fix at the unit level.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (mock ``redis.set`` / ``redis.eval`` /
  ``redis.exists`` / ``redis.get`` argument shape).
- §8.4 Side effects (lock-key string composition).
- §8.11 Time-dependency analogue (D1 snapshot invariant — ContextVar
  flip after construction must not affect the captured key).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from baldur.adapters.cache.redis_adapter import (
    RedisCacheAdapter,
    RedisDistributedLock,
)
from baldur.core.test_mode_context import TestModeContext
from baldur.settings.namespace import NamespaceSettings


@pytest.fixture
def mock_redis_client():
    """Mock Redis client with a connection_pool stand-in."""
    client = MagicMock()
    client.connection_pool = MagicMock()
    # SET NX PX returns truthy when acquired
    client.set.return_value = True
    # EVAL (release/extend) returns 1 (success)
    client.eval.return_value = 1
    # EXISTS / GET defaults
    client.exists.return_value = 1
    client.get.return_value = None
    return client


@pytest.fixture
def patched_namespace_settings(monkeypatch):
    """Replace ``get_namespace_settings()`` with a per-test stub."""

    def _install(*, enabled: bool = False, region: str | None = None):
        stub = NamespaceSettings(namespace_enabled=enabled, region=region)
        monkeypatch.setattr(
            "baldur.settings.namespace.get_namespace_settings",
            lambda: stub,
        )
        return stub

    return _install


# ---------------------------------------------------------------------------
# Adapter-level: get_lock() resolves the full key via _make_key once
# ---------------------------------------------------------------------------


class TestRedisLockPrefixBehavior:
    """``RedisCacheAdapter.get_lock`` routes the lock name through ``_make_key``."""

    def test_default_prefix_uses_baldur_segment(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``key_prefix=None``, no TestMode → lock writes ``baldur:foo``."""
        # Given
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)

        # When
        lock = adapter.get_lock("foo")
        lock.acquire(blocking=False)

        # Then
        # SET NX PX is called with the resolved full key.
        full_key_used = mock_redis_client.set.call_args[0][0]
        assert full_key_used == "baldur:foo"

    def test_test_mode_active_uses_xtest_segment(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``TestModeContext.start()`` → lock writes ``xtest:baldur:foo``.

        This is the v1.0 PRO blocker fix — synthetic lock keys are now
        distinct from real-mode lock keys.
        """
        # Given
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)

        # When
        with TestModeContext.start(session_id="xtest-blocker"):
            lock = adapter.get_lock("foo")
            lock.acquire(blocking=False)

        # Then
        full_key_used = mock_redis_client.set.call_args[0][0]
        assert full_key_used == "xtest:baldur:foo"

    def test_namespace_region_uses_region_segment(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``BALDUR_NAMESPACE_REGION=seoul`` → lock writes ``baldur:seoul:foo``.

        Multi-region isolation falls out of the same patch; #465 OOS
        pins the side-effect with this single assertion.
        """
        # Given
        patched_namespace_settings(enabled=True, region="seoul")
        adapter = RedisCacheAdapter(client=mock_redis_client)

        # When
        lock = adapter.get_lock("foo")
        lock.acquire(blocking=False)

        # Then
        full_key_used = mock_redis_client.set.call_args[0][0]
        assert full_key_used == "baldur:seoul:foo"

    def test_test_mode_and_namespace_compose(
        self, mock_redis_client, patched_namespace_settings
    ):
        """TestMode + namespace compose → ``xtest:baldur:seoul:foo``."""
        patched_namespace_settings(enabled=True, region="seoul")
        adapter = RedisCacheAdapter(client=mock_redis_client)

        with TestModeContext.start(session_id="x"):
            lock = adapter.get_lock("foo")
            lock.acquire(blocking=False)

        assert mock_redis_client.set.call_args[0][0] == "xtest:baldur:seoul:foo"

    def test_static_literal_prefix_ignores_test_mode(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``key_prefix="static:"`` → literal wins; TestMode does not flip it."""
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client, key_prefix="static:")

        with TestModeContext.start(session_id="x"):
            lock = adapter.get_lock("foo")
            lock.acquire(blocking=False)

        assert mock_redis_client.set.call_args[0][0] == "static:foo"

    def test_empty_composer_prefix_writes_raw_name(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``key_prefix=""`` → composer pattern; lock writes ``foo`` verbatim."""
        adapter = RedisCacheAdapter(client=mock_redis_client, key_prefix="")

        lock = adapter.get_lock("foo")
        lock.acquire(blocking=False)

        assert mock_redis_client.set.call_args[0][0] == "foo"

    def test_lock_key_no_double_lock_sentinel(
        self, mock_redis_client, patched_namespace_settings
    ):
        """The pre-#465 ``f"lock:{name}"`` sentinel is gone.

        Regression guard: if a future commit reintroduces ``lock:`` as
        a hardcoded segment inside ``RedisDistributedLock.__init__``,
        this test fails.
        """
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)

        lock = adapter.get_lock("idempotency:lock:order:abc")
        lock.acquire(blocking=False)

        # Single ``lock:`` segment from the caller; lock class adds nothing.
        assert (
            mock_redis_client.set.call_args[0][0] == "baldur:idempotency:lock:order:abc"
        )


# ---------------------------------------------------------------------------
# Lock-class contract: full_key is written verbatim, no transformation
# ---------------------------------------------------------------------------


class TestRedisDistributedLockContract:
    """Direct ``RedisDistributedLock(full_key=...)`` construction contract.

    Pins behavior for the 5 production direct callers (audit/integrity,
    audit/fallback, integrity_tasks, postmortem GENERATE/GROUP). They
    construct the prefixed key themselves and pass it as ``full_key``;
    the lock writes it verbatim with zero transformation.
    """

    def test_acquire_writes_verbatim_full_key(self, mock_redis_client):
        """SET NX PX uses the verbatim full_key — no extra prefix segment."""
        full_key = "baldur:audit:hash_chain:lock"

        lock = RedisDistributedLock(
            redis_client=mock_redis_client,
            full_key=full_key,
            timeout=timedelta(seconds=5),
        )
        lock.acquire(blocking=False)

        assert mock_redis_client.set.call_args[0][0] == full_key

    def test_release_lua_uses_verbatim_full_key(self, mock_redis_client):
        """Release Lua script's KEYS[1] is the verbatim full_key."""
        full_key = "baldur:audit:hash_chain:lock"

        lock = RedisDistributedLock(
            redis_client=mock_redis_client,
            full_key=full_key,
        )
        lock.acquire(blocking=False)
        lock.release()

        # eval(script, numkeys, *keys, *args) → keys are positional args
        # after numkeys=1.
        eval_args = mock_redis_client.eval.call_args[0]
        assert eval_args[1] == 1
        assert eval_args[2] == full_key

    def test_locked_uses_verbatim_full_key(self, mock_redis_client):
        """``locked()`` calls ``EXISTS`` on the verbatim full_key."""
        full_key = "postmortem:generate:incident-42"

        lock = RedisDistributedLock(
            redis_client=mock_redis_client,
            full_key=full_key,
        )
        lock.locked()

        mock_redis_client.exists.assert_called_once_with(full_key)

    def test_owned_uses_verbatim_full_key(self, mock_redis_client):
        """``owned()`` calls ``GET`` on the verbatim full_key."""
        full_key = "baldur:integrity:background_verify_lock"

        lock = RedisDistributedLock(
            redis_client=mock_redis_client,
            full_key=full_key,
        )
        lock.acquire(blocking=False)

        mock_redis_client.get.return_value = lock._owner_id.encode("utf-8")
        lock.owned()

        mock_redis_client.get.assert_called_with(full_key)

    def test_extend_lua_uses_verbatim_full_key(self, mock_redis_client):
        """Extend Lua script's KEYS[1] is the verbatim full_key."""
        full_key = "baldur:audit:hash_chain:lock"

        lock = RedisDistributedLock(
            redis_client=mock_redis_client,
            full_key=full_key,
        )
        lock.acquire(blocking=False)
        lock.extend(timedelta(seconds=30))

        # Last eval call is the extend script.
        last_eval_args = mock_redis_client.eval.call_args[0]
        assert last_eval_args[2] == full_key


# ---------------------------------------------------------------------------
# D1 snapshot invariant: full_key captured at construction time
# ---------------------------------------------------------------------------


class TestRedisLockSnapshotInvariant:
    """The lock snapshots its full key at construction (#465 D1).

    Toggling ``TestModeContext`` between construction and acquire MUST
    NOT shift the storage key — acquire/release symmetry depends on a
    single namespace per lock instance.
    """

    def test_construction_in_real_mode_acquire_in_test_mode_uses_real_key(
        self, mock_redis_client, patched_namespace_settings
    ):
        """Construct in real mode, acquire inside TestMode → real-mode key.

        D1 documented behavior: cross-context construction/acquire is
        safe (no orphan) but uses the construction-time namespace.
        """
        # Given
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)

        # When — construct outside, acquire inside
        lock = adapter.get_lock("foo")
        with TestModeContext.start(session_id="x"):
            lock.acquire(blocking=False)

        # Then — captured at construction (real mode)
        assert mock_redis_client.set.call_args[0][0] == "baldur:foo"

    def test_construction_in_test_mode_acquire_outside_uses_test_key(
        self, mock_redis_client, patched_namespace_settings
    ):
        """Construct in TestMode, acquire after exit → still TestMode key."""
        # Given
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)

        # When
        with TestModeContext.start(session_id="x"):
            lock = adapter.get_lock("foo")
        lock.acquire(blocking=False)

        # Then — captured at construction (test mode)
        assert mock_redis_client.set.call_args[0][0] == "xtest:baldur:foo"

    def test_acquire_release_use_identical_key_across_context_flip(
        self, mock_redis_client, patched_namespace_settings
    ):
        """Acquire and release write/read the SAME Redis key.

        Without snapshot, a TestMode flip between acquire and release
        would orphan the lock until TTL — D1 rationale.
        """
        # Given
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)
        lock = adapter.get_lock("foo")

        # When
        lock.acquire(blocking=False)
        acquire_key = mock_redis_client.set.call_args[0][0]

        with TestModeContext.start(session_id="x"):
            lock.release()
        release_key = mock_redis_client.eval.call_args[0][2]

        # Then
        assert acquire_key == release_key == "baldur:foo"
