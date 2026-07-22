"""Compressed-DLQ detail handler tests (721 D2/D3).

``dlq_compressed_detail`` used to fetch ``get_compressed_entries(limit=1000)``
and linear-scan the result, so a compressed entry outside the newest 1000
returned 404 *despite existing* (G3). It now issues a direct by-id read, and
the by-id read guards reserved structural key names so a crafted ``entry_id``
cannot flip the resilient backend into degraded mode via a ``WRONGTYPE`` on a
sorted set (D2).

The handler is driven with a repository whose ``get_compressed_entry`` is the
real Redis implementation over an in-process blob backend, so the assertions
exercise the production read path rather than a stubbed return.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.dlq_compressed import dlq_compressed_detail
from baldur.interfaces.repositories import DLQCompressedEntry
from baldur.interfaces.web_framework import HttpMethod, RequestContext

_REPOSITORY = "baldur.api.handlers.dlq_compressed._repository"


class _BlobBackend:
    """In-process backend that stores entry blobs and dispatches batch ops.

    The by-id read is index-free (a direct ``get_blob`` on
    ``dlq:compressed:{id}``), so only the blob store is load-bearing here; the
    ``zadd`` / ``zrem`` index ops the store emits are accepted and dropped.
    """

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def set_blob(self, key: str, value: bytes) -> None:
        self.blobs[key] = value

    def get_blob(self, key: str) -> bytes | None:
        return self.blobs.get(key)

    def zadd(self, key: str, mapping: dict) -> None:
        pass

    def zrem(self, key: str, members) -> None:
        pass

    def batch_write_ops(self, ops: list[tuple]) -> None:
        for op in ops:
            getattr(self, op[0])(*op[1:])


def _make_repo(backend):
    """Build a RedisDLQRepository around ``backend`` (real by-id read path)."""
    from baldur.adapters.redis.dlq import RedisDLQRepository
    from baldur.adapters.redis.dlq_compression import RedisDLQCompression

    with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
        repo = RedisDLQRepository.__new__(RedisDLQRepository)
    repo._backend = backend
    repo.compression = RedisDLQCompression(repo)
    return repo


def _entry(entry_id: str, *, seconds_old: int = 0) -> DLQCompressedEntry:
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
    return DLQCompressedEntry(
        id=entry_id,
        domain="payment",
        failure_type="timeout",
        error_code="E001",
        count=3,
        first_seen=now - timedelta(days=7),
        last_seen=now,
        sample_error_message="Connection timeout",
        sample_context={"endpoint": "/api/pay"},
        status="active",
        compressed_at=now - timedelta(seconds=seconds_old),
    )


def _detail_ctx(entry_id: str) -> RequestContext:
    return RequestContext(
        method=HttpMethod("GET"),
        path=f"/dlq-compressed/{entry_id}",
        path_params={"entry_id": entry_id},
    )


class TestDlqCompressedDetailHandler:
    """dlq_compressed_detail: by-id read (G3) + reserved-name guard (D2)."""

    def test_entry_outside_the_newest_1000_is_returned_not_404(self):
        """An entry older than the newest 1000 is served by its detail endpoint.

        Regression for G3: the old handler linear-scanned the newest 1000
        entries, so the 1001st-oldest returned 404 despite existing. The by-id
        read is index-free, so window position is irrelevant.
        """
        # Given -- 1001 stored entries; the oldest sits outside any 1000-window.
        backend = _BlobBackend()
        repo = _make_repo(backend)
        oldest_id = "compressed:payment:timeout:E001:0000"
        repo.store_compressed_entry(_entry(oldest_id, seconds_old=1001))
        for i in range(1, 1001):
            repo.store_compressed_entry(
                _entry(f"compressed:payment:timeout:E001:{i:04d}", seconds_old=1001 - i)
            )

        # When -- the detail handler is asked for the oldest id.
        with patch(_REPOSITORY, return_value=repo):
            resp = dlq_compressed_detail(_detail_ctx(oldest_id))

        # Then -- 200 with the entry's payload, not a 404.
        assert resp.status_code == 200
        assert resp.body["id"] == oldest_id
        assert resp.body["domain"] == "payment"

    def test_missing_entry_returns_404(self):
        """An id with no stored blob maps to a 404."""
        backend = _BlobBackend()
        repo = _make_repo(backend)

        with patch(_REPOSITORY, return_value=repo):
            resp = dlq_compressed_detail(_detail_ctx("compressed:payment:absent:1"))

        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "reserved_id",
        [
            "index",
            "by_domain:payment",
            "status:active",
            "status_domain:active:payment",
        ],
        ids=["index", "by_domain", "status", "status_domain"],
    )
    def test_reserved_key_name_returns_404_without_touching_the_backend(
        self, reserved_id
    ):
        """A reserved structural key name as ``entry_id`` returns 404 and never
        reads the backend, so it cannot induce a WRONGTYPE-driven degrade.

        The guard short-circuits before ``get_blob`` -- asserting the read is
        never issued pins the guard as the proximate cause (§8.13), which is
        what keeps ``_switch_to_degraded`` from firing on a viewer request.
        """
        backend = MagicMock()
        repo = _make_repo(backend)

        with patch(_REPOSITORY, return_value=repo):
            resp = dlq_compressed_detail(_detail_ctx(reserved_id))

        assert resp.status_code == 404
        backend.get_blob.assert_not_called()
        backend._switch_to_degraded.assert_not_called()
