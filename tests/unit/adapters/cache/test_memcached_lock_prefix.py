"""Unit tests for ``MemcachedCacheAdapter.get_lock`` prefix isolation (#465).

Source: ``src/baldur/adapters/cache/memcached_adapter.py``

Covers (#465 G2 / G5):

- ``MemcachedCacheAdapter.get_lock(name)`` resolves the full storage
  key once via ``self._make_key(name)`` and passes it as ``full_key``
  to ``MemcachedDistributedLock`` (D4).
- The configured static ``key_prefix`` (``"baldur:"`` default,
  ``"custom:"``, etc.) is honored on the lock path — pre-#465 the
  lock silently dropped the prefix and wrote ``lock:foo``.
- 250-byte truncation is applied at the adapter's ``_make_key``
  boundary, before the lock sees the key — D4 rationale.
- ``MemcachedDistributedLock`` writes ``full_key`` verbatim on
  acquire (ADD) / release (verify+delete) / locked (GET).
- Per #465 D3, Memcached does NOT honor ``TestModeContext`` /
  ``NamespaceSettings`` (Redis-only per #463 D9). The fix here is
  contract restoration, not synthetic isolation.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (mock client ``add`` / ``get`` /
  ``delete`` argument shape).
- §8.4 Side effects (lock-key string composition).
- §8.2 Boundary analysis (250-byte truncation).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from baldur.adapters.cache.memcached_adapter import (
    MemcachedCacheAdapter,
    MemcachedDistributedLock,
)


@pytest.fixture
def mock_memcached_client():
    """Mock pymemcache HashClient with default-success responses."""
    client = MagicMock()
    client.add.return_value = True
    client.get.return_value = None
    client.delete.return_value = True
    return client


@pytest.fixture
def adapter_with_mock_client(mock_memcached_client):
    """Build a MemcachedCacheAdapter with the mock client preinjected.

    The adapter lazily resolves ``self.client`` via the ``client``
    property; preinjecting ``self._client`` skips pymemcache import.
    """

    def _build(*, key_prefix: str = "baldur:") -> MemcachedCacheAdapter:
        adapter = MemcachedCacheAdapter(
            servers=["localhost:11211"],
            key_prefix=key_prefix,
        )
        adapter._client = mock_memcached_client
        return adapter

    return _build


# ---------------------------------------------------------------------------
# Adapter-level: get_lock() routes name through _make_key()
# ---------------------------------------------------------------------------


class TestMemcachedLockPrefixBehavior:
    """``MemcachedCacheAdapter.get_lock`` honors the configured ``key_prefix``."""

    def test_default_prefix_uses_baldur_segment(
        self, adapter_with_mock_client, mock_memcached_client
    ):
        """``key_prefix="baldur:"`` (default) → lock writes ``baldur:foo``."""
        adapter = adapter_with_mock_client()

        lock = adapter.get_lock("foo")
        lock.acquire(blocking=False)

        # client.add(key, value, expire=...) — first positional is the key.
        full_key_used = mock_memcached_client.add.call_args[0][0]
        assert full_key_used == "baldur:foo"

    def test_custom_prefix_propagates_to_lock(
        self, adapter_with_mock_client, mock_memcached_client
    ):
        """``key_prefix="custom:"`` → lock writes ``custom:foo``.

        Pre-#465 the lock wrote ``lock:foo``, silently dropping the
        adapter's configured prefix — the contract violation that G2
        fixes.
        """
        adapter = adapter_with_mock_client(key_prefix="custom:")

        lock = adapter.get_lock("foo")
        lock.acquire(blocking=False)

        assert mock_memcached_client.add.call_args[0][0] == "custom:foo"

    def test_long_name_truncated_at_adapter_boundary(
        self, adapter_with_mock_client, mock_memcached_client
    ):
        """``_make_key`` truncates to 250 bytes BEFORE the lock sees the key.

        D4 rationale — Memcached's 250-byte limit is enforced at the
        adapter boundary. The lock is unaware of the limit and writes
        whatever ``full_key`` it receives.
        """
        adapter = adapter_with_mock_client(key_prefix="baldur:")
        long_name = "x" * 300  # full_key would be 307 chars before truncation

        lock = adapter.get_lock(long_name)
        lock.acquire(blocking=False)

        full_key_used = mock_memcached_client.add.call_args[0][0]
        assert len(full_key_used) == 250
        assert full_key_used.startswith("baldur:")

    def test_whitespace_replaced_at_adapter_boundary(
        self, adapter_with_mock_client, mock_memcached_client
    ):
        """``_make_key`` replaces whitespace with ``_`` BEFORE the lock sees it.

        Memcached protocol forbids whitespace in keys; the adapter's
        ``_make_key`` cleans the key before constructing the lock.
        """
        adapter = adapter_with_mock_client(key_prefix="baldur:")

        lock = adapter.get_lock("foo bar")
        lock.acquire(blocking=False)

        assert mock_memcached_client.add.call_args[0][0] == "baldur:foo_bar"


# ---------------------------------------------------------------------------
# Lock-class contract: full_key is written verbatim, no transformation
# ---------------------------------------------------------------------------


class TestMemcachedDistributedLockContract:
    """Direct ``MemcachedDistributedLock(full_key=...)`` contract.

    Acquire / release / locked all hit the verbatim full_key with no
    in-class transformation. No ``f"lock:{name}"`` sentinel is added.
    """

    def test_acquire_uses_verbatim_full_key(self, mock_memcached_client):
        """ADD is called with the verbatim full_key (no extra prefix)."""
        full_key = "custom:order:42"

        lock = MemcachedDistributedLock(
            client=mock_memcached_client,
            full_key=full_key,
            timeout=timedelta(seconds=5),
        )
        lock.acquire(blocking=False)

        assert mock_memcached_client.add.call_args[0][0] == full_key

    def test_release_verifies_and_deletes_verbatim_full_key(
        self, mock_memcached_client
    ):
        """Release reads ``GET`` and ``DELETE`` on the verbatim full_key."""
        full_key = "custom:order:42"
        lock = MemcachedDistributedLock(
            client=mock_memcached_client,
            full_key=full_key,
        )
        lock.acquire(blocking=False)
        # Make GET return our token so DELETE path runs.
        mock_memcached_client.get.return_value = lock._token

        lock.release()

        mock_memcached_client.get.assert_called_with(full_key)
        mock_memcached_client.delete.assert_called_with(full_key)

    def test_locked_uses_verbatim_full_key(self, mock_memcached_client):
        """``locked()`` calls ``GET`` on the verbatim full_key."""
        full_key = "baldur:postmortem:lock"

        lock = MemcachedDistributedLock(
            client=mock_memcached_client,
            full_key=full_key,
        )
        lock.locked()

        mock_memcached_client.get.assert_called_once_with(full_key)

    def test_owned_uses_verbatim_full_key(self, mock_memcached_client):
        """``owned()`` calls ``GET`` on the verbatim full_key.

        Regression guard for the b90828ac drive-by fix that added
        ``MemcachedDistributedLock.owned()`` to satisfy the ABC. Mirrors
        the contract pinned by ``test_release_verifies_and_deletes_verbatim_full_key``.
        """
        full_key = "baldur:postmortem:lock"

        lock = MemcachedDistributedLock(
            client=mock_memcached_client,
            full_key=full_key,
        )
        lock.acquire(blocking=False)
        mock_memcached_client.get.return_value = lock._token

        assert lock.owned() is True
        mock_memcached_client.get.assert_called_with(full_key)
