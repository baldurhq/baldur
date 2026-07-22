"""Shared fixtures for the compressed-DLQ adapter suites.

Both compressed-entry suites in this directory drive the production read paths
through ``FakeSortedSetBackend`` (kept in ``tests.factories.redis`` — the stub
class outgrew the conftest size limit): the defects those paths exist to fix
are ordering and routing ones, and a MagicMock returns whatever the test hands
it, so it would pass against either ordering (§6.4).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.interfaces.repositories import DLQCompressedEntry
from baldur.utils.time import utc_now
from tests.factories.redis import FakeSortedSetBackend

__all__ = ["FakeSortedSetBackend"]


def _build_repo(backend: FakeSortedSetBackend) -> RedisDLQRepository:
    """Build a RedisDLQRepository around the fake backend."""
    from unittest.mock import patch

    from baldur.adapters.redis.dlq_compression import RedisDLQCompression

    with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
        repo = RedisDLQRepository.__new__(RedisDLQRepository)
    repo._backend = backend
    repo.compression = RedisDLQCompression(repo)
    return repo


@pytest.fixture
def backend() -> FakeSortedSetBackend:
    return FakeSortedSetBackend()


@pytest.fixture
def compression(backend):
    """The repository, not the compression mixin it delegates to.

    The lifecycle sweep holds a repository, so the delegating wrapper is
    part of the call path under test. Driving the mixin directly would let
    a wrapper whose signature has drifted from the mixin's pass here and
    fail in production.
    """
    return _build_repo(backend)


@pytest.fixture
def store_compressed(compression):
    """Store a compressed entry through the real adapter write path."""

    def _store(suffix, *, days_ago, status="active", domain="payment"):
        now = utc_now()
        entry = DLQCompressedEntry(
            id=f"compressed:{domain}:timeout:E_X:{suffix}",
            domain=domain,
            failure_type="timeout",
            error_code="E_X",
            count=1,
            first_seen=now,
            last_seen=now,
            sample_error_message="x",
            status=status,
            compressed_at=now - timedelta(days=days_ago),
        )
        compression.store_compressed_entry(entry)
        return entry

    return _store


@pytest.fixture
def rewrite_blob_status(backend):
    """Apply only the payload write of a transition (its first op).

    A transition writes the payload first, then relocates the index
    membership, so a crash between the two leaves exactly this state: a
    payload naming the new status while the indexes still name the old one.
    """

    def _rewrite(entry, new_status: str):
        from baldur.utils.serialization import fast_dumps, fast_loads

        key = f"dlq:compressed:{entry.id}"
        data = fast_loads(backend.blobs[key])
        data["status"] = new_status
        backend.set_blob(key, fast_dumps(data))

    return _rewrite


@pytest.fixture
def mark_index_ready(backend):
    """Stamp the migration-completion marker the status routes are gated on."""

    def _mark():
        from baldur.adapters.redis.dlq_compression import _COMPRESSED_MARKER_KEY
        from baldur.utils.serialization import fast_dumps

        backend.set_blob(
            _COMPRESSED_MARKER_KEY,
            fast_dumps({"stamped_at": utc_now().isoformat(), "source": "test"}),
        )

    return _mark
