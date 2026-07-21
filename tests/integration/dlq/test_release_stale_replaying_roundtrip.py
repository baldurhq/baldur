"""Integration: stranded-REPLAYING entries are recoverable without PRO.

The lifecycle spans two components that must reach the *same* repository
instance: an entry is acquired into REPLAYING by the replay path, the worker
dies before completing, and the maintenance task later releases it back to
PENDING so a subsequent replay can acquire it again.

Before the tier re-point this was unreachable on an install without the PRO
distribution — the task read a PRO-only registry slot and raised before doing
any work, so an entry stranded by a crashed worker stayed stuck forever. These
tests drive the real resolution chain with both DLQ registry slots empty.

Mock-based (no infra): the in-memory repository is injected through the capture
service's constructor DI seam, and the clock is driven through the adapter's
time seam rather than by sleeping.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.settings.dlq import DLQSettings
from baldur.utils.time import utc_now

_STALE_THRESHOLD_MINUTES = 30


@pytest.fixture
def repository():
    """A fresh in-memory DLQ repository — the single instance both paths share."""
    return InMemoryFailedOperationRepository()


@pytest.fixture
def oss_backing(monkeypatch, repository):
    """Wire the repository behind the canonical chain with both slots empty.

    Simulates a pure-OSS install: ``resolve_dlq_backing()`` misses the PRO
    ``dlq_service`` slot and falls through to the OSS capture singleton, which
    is replaced here by one holding the test repository.
    """
    from baldur.factory.registry import ProviderRegistry
    from baldur.services.dlq_capture import service as capture_module
    from baldur.services.dlq_capture.service import (
        DLQCaptureService,
        reset_dlq_capture_service,
    )

    monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)
    monkeypatch.setattr(ProviderRegistry.dlq_repository, "safe_get", lambda: None)
    monkeypatch.setattr(
        capture_module,
        "_capture_service",
        DLQCaptureService(repository=repository),
    )
    yield repository
    reset_dlq_capture_service()


@pytest.fixture
def stale_settings():
    """Real DLQ settings pinning the staleness threshold the task reads.

    A real settings object, not a mock: it is a cheap Pydantic model, so
    constructing one keeps the threshold subject to the field's own validation
    instead of accepting whatever a mock would hand back.
    """
    return DLQSettings(stale_replaying_timeout_minutes=_STALE_THRESHOLD_MINUTES)


def _run_release_task(stale_settings, *, now):
    """Execute the maintenance task body at a controlled wall-clock instant."""
    from baldur.celery_tasks.dlq_tasks import release_stale_replaying

    with (
        patch("baldur.settings.dlq.get_dlq_settings", return_value=stale_settings),
        patch("baldur.adapters.memory.failed_operation._now", return_value=now),
    ):
        return release_stale_replaying()


def _acquire_into_replaying(repository, entry_id, *, at):
    """Acquire an entry for replay, stamping the acquisition at a fixed instant."""
    with patch("baldur.adapters.memory.failed_operation._now", return_value=at):
        return repository.try_acquire_for_replay(entry_id, max_retries=3)


@pytest.fixture
def stranded_entry(oss_backing):
    """An entry acquired into REPLAYING whose worker never came back."""
    repository = oss_backing
    acquired_at = utc_now()
    entry = repository.create(domain="payment", failure_type="timeout")

    assert _acquire_into_replaying(repository, entry.id, at=acquired_at) is not None
    assert (
        repository.get_by_id(entry.id).status == FailedOperationStatus.REPLAYING.value
    )

    return entry, acquired_at


class TestReleaseStaleReplayingRoundtrip:
    """The acquire → strand → release lifecycle on a pure-OSS install."""

    def test_stranded_entry_returns_to_pending(
        self, oss_backing, stale_settings, stranded_entry
    ):
        """An entry stranded past the threshold is released back to PENDING."""
        repository = oss_backing
        entry, acquired_at = stranded_entry

        result = _run_release_task(
            stale_settings,
            now=acquired_at + timedelta(minutes=_STALE_THRESHOLD_MINUTES + 1),
        )

        assert result["success"] is True
        assert result["released_count"] == 1
        assert (
            repository.get_by_id(entry.id).status == FailedOperationStatus.PENDING.value
        )

    def test_released_entry_is_acquirable_again(
        self, oss_backing, stale_settings, stranded_entry
    ):
        """Recovery is real: the released entry can be replayed again.

        The point of the release is not the status field but that the entry
        re-enters the replay pipeline — a released entry that stayed
        unacquirable would be no better than stranded.
        """
        repository = oss_backing
        entry, acquired_at = stranded_entry
        released_at = acquired_at + timedelta(minutes=_STALE_THRESHOLD_MINUTES + 1)

        _run_release_task(stale_settings, now=released_at)
        reacquired = _acquire_into_replaying(repository, entry.id, at=released_at)

        assert reacquired is not None
        assert reacquired.status == FailedOperationStatus.REPLAYING.value

    def test_entry_within_the_threshold_is_left_alone(
        self, oss_backing, stale_settings, stranded_entry
    ):
        """A replay still inside its window is in-flight, not stranded.

        Boundary guard: releasing it would hand the same entry to a second
        worker while the first is still running it.
        """
        repository = oss_backing
        entry, acquired_at = stranded_entry

        result = _run_release_task(
            stale_settings,
            now=acquired_at + timedelta(minutes=_STALE_THRESHOLD_MINUTES - 1),
        )

        assert result["released_count"] == 0
        assert (
            repository.get_by_id(entry.id).status
            == FailedOperationStatus.REPLAYING.value
        )

    def test_release_is_idempotent_across_repeated_runs(
        self, oss_backing, stale_settings, stranded_entry
    ):
        """The task runs every 15 minutes — a second pass must find nothing.

        A non-idempotent release would keep reporting the same entry, and the
        WARNING it emits (which reads as "a worker crashed") would fire forever.
        """
        _entry, acquired_at = stranded_entry
        past_threshold = acquired_at + timedelta(minutes=_STALE_THRESHOLD_MINUTES + 1)

        first = _run_release_task(stale_settings, now=past_threshold)
        second = _run_release_task(stale_settings, now=past_threshold)

        assert first["released_count"] == 1
        assert second["released_count"] == 0

    def test_healthy_pending_entries_are_untouched(
        self, oss_backing, stale_settings, stranded_entry
    ):
        """Only REPLAYING entries are candidates — a PENDING one is not swept up."""
        repository = oss_backing
        _entry, acquired_at = stranded_entry
        untouched = repository.create(domain="orders", failure_type="timeout")

        result = _run_release_task(
            stale_settings,
            now=acquired_at + timedelta(minutes=_STALE_THRESHOLD_MINUTES + 1),
        )

        assert result["released_count"] == 1
        assert (
            repository.get_by_id(untouched.id).status
            == FailedOperationStatus.PENDING.value
        )
