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

import threading
import types
from unittest.mock import MagicMock, patch

from baldur.adapters.cache import RedisCacheAdapter
from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.resilient.backend import (
    ResilientStorageBackend,
    ResilientStorageMode,
)


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


def _blobs_backend(
    *, mode: ResilientStorageMode, redis=None, memory: dict | None = None
) -> ResilientStorageBackend:
    """A backend with only the attributes ``get_blobs`` touches wired up.

    ``_get_full_key`` is driven by a static-prefix config namespace so full
    keys equal short keys, keeping the ``mget`` argument assertions readable.
    """
    backend = object.__new__(ResilientStorageBackend)
    backend._mode = mode
    backend._redis = redis
    backend._redis_initialized = True
    backend._blob_memory = dict(memory or {})
    backend._lock = threading.RLock()
    backend.config = types.SimpleNamespace(use_dynamic_prefix=False, key_prefix="")
    return backend


class TestGetBlobsBehavior:
    """ResilientStorageBackend.get_blobs — the D5 batched raw-bytes read."""

    def test_get_blobs_runs_ensure_redis_before_the_mode_check(self):
        """``_ensure_redis`` runs first, so a fresh DEGRADED backend promoted by
        it serves real bytes rather than all-None from memory."""
        # Given -- constructed DEGRADED (as the real backend is) with no redis.
        backend = _blobs_backend(mode=ResilientStorageMode.DEGRADED)
        sentinel = [b"blob-a", b"blob-b"]

        def _promote():
            backend._mode = ResilientStorageMode.REDIS
            backend._redis = MagicMock()
            backend._redis.raw_client.mget.return_value = sentinel
            return True

        # When -- get_blobs is called; _ensure_redis promotes to REDIS first.
        with patch.object(backend, "_ensure_redis", side_effect=_promote):
            result = backend.get_blobs(["k1", "k2"])

        # Then -- real bytes. A mode-check-first read would still see DEGRADED
        # and return [None, None] from the empty blob store.
        assert result == sentinel

    def test_get_blobs_empty_list_returns_empty_without_touching_client(self):
        """An empty key list short-circuits before any Redis command."""
        redis = MagicMock()
        backend = _blobs_backend(mode=ResilientStorageMode.REDIS, redis=redis)

        with patch.object(backend, "_ensure_redis", return_value=True):
            result = backend.get_blobs([])

        assert result == []
        redis.raw_client.mget.assert_not_called()

    def test_get_blobs_normal_mode_issues_one_mget_over_full_keys(self):
        """Normal mode issues a single ``mget`` over the full keys."""
        redis = MagicMock()
        redis.raw_client.mget.return_value = [b"a", b"b"]
        backend = _blobs_backend(mode=ResilientStorageMode.REDIS, redis=redis)

        with patch.object(backend, "_ensure_redis", return_value=True):
            result = backend.get_blobs(["k1", "k2"])

        assert result == [b"a", b"b"]
        redis.raw_client.mget.assert_called_once_with(["k1", "k2"])

    def test_get_blobs_redis_error_degrades_and_serves_memory(self):
        """A Redis error degrades the backend and serves every key from memory."""
        redis = MagicMock()
        redis.raw_client.mget.side_effect = ConnectionError("down")
        backend = _blobs_backend(
            mode=ResilientStorageMode.REDIS, redis=redis, memory={"k1": b"m1"}
        )

        with patch.object(backend, "_ensure_redis", return_value=True):
            result = backend.get_blobs(["k1", "k2"])

        assert result == [b"m1", None]
        assert backend.is_degraded is True

    def test_get_blobs_degraded_mode_reads_memory_directly(self):
        """Degraded mode reads the bounded blob store without any client call."""
        backend = _blobs_backend(
            mode=ResilientStorageMode.DEGRADED, memory={"k1": b"m1", "k2": b"m2"}
        )

        with patch.object(backend, "_ensure_redis", return_value=False):
            result = backend.get_blobs(["k1", "k2", "k3"])

        assert result == [b"m1", b"m2", None]
