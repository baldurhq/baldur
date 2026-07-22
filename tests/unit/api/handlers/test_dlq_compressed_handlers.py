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

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.dlq_compressed import (
    dlq_compressed_detail,
    dlq_compressed_list,
    dlq_compressed_migrate,
)
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


# =============================================================================
# has_more truthfulness (723 D2) + the operator migration handler (723 D4)
# =============================================================================


class _RecordingRepository:
    """Records what the handler forwarded and answers with a fixed population.

    The handler's whole job here is the ``limit + 1`` probe, so the forwarded
    limit is the assertion that matters — a handler that asked for ``limit``
    and compared ``len(entries) >= limit`` produces the same body on a full
    page and can only be told apart by what it asked for.
    """

    def __init__(self, available: int = 0, backfill_result: dict | None = None):
        self._entries = [
            _entry(f"compressed:payment:timeout:E001:{i:04d}") for i in range(available)
        ]
        self.calls: list[dict] = []
        self.backfill_calls: list[dict] = []
        self._backfill_result = backfill_result or {
            "complete": True,
            "mode": "full",
            "walked": 7,
            "added": 4,
            "skipped_unreadable": 0,
            "verified": True,
            "marker_set": True,
        }

    def get_compressed_entries(self, domain=None, status=None, limit=100):
        self.calls.append({"domain": domain, "status": status, "limit": limit})
        return self._entries[:limit]

    def backfill_compressed_status_index(self, *, operator_initiated=False):
        self.backfill_calls.append({"operator_initiated": operator_initiated})
        return self._backfill_result


@contextmanager
def _lock(acquired: bool):
    yield acquired


def _list_ctx(**query) -> RequestContext:
    return RequestContext(
        method=HttpMethod("GET"),
        path="/dlq-compressed",
        query_params={k: str(v) for k, v in query.items() if v is not None},
    )


def _migrate_ctx() -> RequestContext:
    return RequestContext(
        method=HttpMethod("POST"),
        path="/dlq/migrate-compressed/",
        json_body={},
    )


class TestDlqCompressedListHandler:
    """has_more is decided by one extra row, not by a post-filter count."""

    @pytest.mark.parametrize(
        ("available", "expected_count", "expected_has_more"),
        [(2, 2, False), (3, 3, False), (5, 3, True)],
        ids=["short_page", "exact_page", "more_available"],
    )
    @pytest.mark.parametrize(
        ("domain", "status"),
        [(None, None), (None, "archived"), ("payment", None), ("payment", "archived")],
        ids=["unfiltered", "status", "domain", "domain_status"],
    )
    def test_has_more_is_true_only_when_a_further_match_exists(
        self, domain, status, available, expected_count, expected_has_more
    ):
        repo = _RecordingRepository(available=available)

        with patch(_REPOSITORY, return_value=repo):
            resp = dlq_compressed_list(_list_ctx(limit=3, domain=domain, status=status))

        assert resp.status_code == 200
        assert resp.body["count"] == expected_count
        assert len(resp.body["results"]) == expected_count
        assert resp.body["has_more"] is expected_has_more

    def test_the_probe_row_is_requested_but_never_returned(self):
        """One row past the page is fetched to decide, then dropped."""
        repo = _RecordingRepository(available=10)

        with patch(_REPOSITORY, return_value=repo):
            resp = dlq_compressed_list(_list_ctx(limit=3, status="archived"))

        assert repo.calls == [{"domain": None, "status": "archived", "limit": 4}]
        assert len(resp.body["results"]) == 3

    def test_filters_are_forwarded_to_the_repository(self):
        repo = _RecordingRepository(available=1)

        with patch(_REPOSITORY, return_value=repo):
            dlq_compressed_list(_list_ctx(limit=50, domain="payment", status="stale"))

        assert repo.calls == [{"domain": "payment", "status": "stale", "limit": 51}]

    def test_an_unparseable_limit_falls_back_to_the_default_page(self):
        repo = _RecordingRepository(available=1)

        with patch(_REPOSITORY, return_value=repo):
            dlq_compressed_list(_list_ctx(limit="not-a-number"))

        assert repo.calls == [{"domain": None, "status": None, "limit": 101}]


class TestDlqCompressedMigrateHandler:
    """The operator migration runs under the sweep's lock and reports honestly."""

    def test_successful_run_is_operator_initiated_and_returns_its_counts(self):
        repo = _RecordingRepository()

        with (
            patch(_REPOSITORY, return_value=repo),
            patch(
                "baldur.dlq.helpers.compressed_lifecycle_lock", lambda s: _lock(True)
            ),
        ):
            resp = dlq_compressed_migrate(_migrate_ctx())

        assert resp.status_code == 200
        assert resp.body["status"] == "ok"
        assert resp.body["walked"] == 7
        assert resp.body["marker_set"] is True
        # Operator-initiated is what lets one pass conclude the migration.
        assert repo.backfill_calls == [{"operator_initiated": True}]

    def test_lock_held_by_the_sweep_is_reported_as_a_conflict(self):
        """A conflict must not also run the walk — retrying is the answer."""
        repo = _RecordingRepository()

        with (
            patch(_REPOSITORY, return_value=repo),
            patch(
                "baldur.dlq.helpers.compressed_lifecycle_lock", lambda s: _lock(False)
            ),
        ):
            resp = dlq_compressed_migrate(_migrate_ctx())

        assert resp.status_code == 409
        assert resp.body["error_code"] == "LOCK_NOT_ACQUIRED"
        assert repo.backfill_calls == []

    def test_unverified_walk_is_a_conflict_carrying_its_own_report(self):
        """An unverified walk did reconcile idempotently — it just cannot be
        trusted to have covered anything, so the operator re-runs it."""
        repo = _RecordingRepository(
            backfill_result={
                "complete": False,
                "mode": "full",
                "walked": 3,
                "added": 0,
                "skipped_unreadable": 0,
                "verified": False,
                "marker_set": False,
            }
        )

        with (
            patch(_REPOSITORY, return_value=repo),
            patch(
                "baldur.dlq.helpers.compressed_lifecycle_lock", lambda s: _lock(True)
            ),
        ):
            resp = dlq_compressed_migrate(_migrate_ctx())

        assert resp.status_code == 409
        assert resp.body["error_code"] == "BACKFILL_UNVERIFIED"
        assert resp.body["details"]["verified"] is False

    def test_absent_pro_repository_raises_for_the_caller_to_map_to_a_500(self):
        """Compressed entries are a PRO surface; an OSS install has no
        repository to migrate, and that is a server-side gap, not a retry."""
        from baldur.api.handlers.dlq_compressed import _repository

        with patch("baldur.factory.registry.ProviderRegistry") as registry:
            registry.dlq_repository.safe_get.return_value = None
            with pytest.raises(RuntimeError, match="baldur_pro"):
                _repository()
