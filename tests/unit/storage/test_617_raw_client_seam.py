"""Unit tests for 617 D7 — public raw-client / ensure-redis seams.

D7 replaced the double-private ``_backend._redis._redis`` reach-through with
three public seams:

- ``RedisCacheAdapter.raw_client`` — returns the underlying redis client.
- ``ResilientStorageBackend.raw_redis_client`` — the adapter's raw client, or
  None when no live Redis adapter exists.
- ``ResilientStorageBackend.ensure_redis()`` — thin public wrapper over the
  internal lazy-init.

The composed Redis DLQ repository now routes through those seams via
``_raw_redis_client`` (with a ``getattr`` default for mock-backend tolerance)
and ``_ensure_redis_available``. These are pure accessors / single-hop
delegation, so the assertions are correspondingly trivial.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.adapters.cache import RedisCacheAdapter
from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.resilient.backend import ResilientStorageBackend


def _bare_backend() -> ResilientStorageBackend:
    """A backend instance with no __init__ side effects (pure-accessor probe)."""
    return object.__new__(ResilientStorageBackend)


class _BackendWithoutRawClient:
    """A stand-in backend that does NOT expose ``raw_redis_client``.

    Exercises the ``getattr(..., None)`` default in
    ``RedisDLQRepository._raw_redis_client`` that tolerates mock backends.
    """


class TestRawClientSeam:
    """RedisCacheAdapter / ResilientStorageBackend / RedisDLQRepository seams."""

    # -- RedisCacheAdapter.raw_client -------------------------------------

    def test_redis_cache_adapter_raw_client_returns_underlying_client(self):
        """``raw_client`` returns the injected redis client verbatim."""
        sentinel = object()
        adapter = RedisCacheAdapter(client=sentinel)

        assert adapter.raw_client is sentinel

    # -- ResilientStorageBackend.raw_redis_client -------------------------

    def test_backend_raw_redis_client_returns_none_when_no_redis(self):
        """``raw_redis_client`` is None when the backend has no redis adapter."""
        backend = _bare_backend()
        backend._redis = None

        assert backend.raw_redis_client is None

    def test_backend_raw_redis_client_delegates_to_adapter_raw_client(self):
        """``raw_redis_client`` returns the adapter's ``raw_client``."""
        sentinel = object()
        backend = _bare_backend()
        backend._redis = MagicMock()
        backend._redis.raw_client = sentinel

        assert backend.raw_redis_client is sentinel

    # -- ResilientStorageBackend.ensure_redis ------------------------------

    def test_backend_ensure_redis_delegates_to_internal_ensure(self):
        """``ensure_redis()`` is a thin wrapper over ``_ensure_redis()``."""
        backend = _bare_backend()

        with patch.object(backend, "_ensure_redis", return_value=True) as mock_ensure:
            result = backend.ensure_redis()

        assert result is True
        mock_ensure.assert_called_once_with()

    # -- RedisDLQRepository._raw_redis_client ------------------------------

    def test_repo_raw_redis_client_returns_backend_seam_value(self):
        """The repo's ``_raw_redis_client`` forwards the backend seam value."""
        sentinel = object()
        backend = MagicMock()
        backend.raw_redis_client = sentinel
        repo = RedisDLQRepository(backend)

        assert repo._raw_redis_client is sentinel

    def test_repo_raw_redis_client_tolerates_backend_without_seam(self):
        """A backend lacking ``raw_redis_client`` yields None (mock tolerance)."""
        repo = RedisDLQRepository(_BackendWithoutRawClient())

        assert repo._raw_redis_client is None

    # -- RedisDLQRepository._ensure_redis_available ------------------------

    def test_repo_ensure_redis_available_delegates_to_backend(self):
        """``_ensure_redis_available`` forwards to ``backend.ensure_redis()``."""
        backend = MagicMock()
        backend.ensure_redis.return_value = True
        repo = RedisDLQRepository(backend)

        result = repo._ensure_redis_available()

        assert result is True
        backend.ensure_redis.assert_called_once_with()
