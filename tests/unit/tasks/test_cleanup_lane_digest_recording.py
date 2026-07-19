"""Tests for the cleanup lane's digest recording call sites.

Targets the six in-scope cleanup task bodies that call
``record_cleanup_result``:

    - ``baldur.tasks.cleanup_tasks``: archive_old_dlq_entries,
      cleanup_expired_config, purge_archived_dlq_entries
    - ``baldur.tasks.config_apply``: cleanup_expired_config_changes
    - ``baldur.celery_tasks.dlq_tasks``: cleanup_resolved_dlq_entries
    - ``baldur.adapters.celery.tasks.dlq_replay``: cleanup_resolved_dlq_entries

The last two are *different* tasks that happen to share a function name —
different registered task names, bodies, and backings (a PRO DLQ service vs
the OSS statistics adapter) — so both are covered separately here. The
adapter-lane one is the only OSS-live producer of the digest's Archived
count, so losing its call site would silently return that counter to 0 on
every OSS install.

The real helper runs in every test (only the collector is patched), so these
cover the call site *and* the policy applied to each task's own result shape.

Posture: every PRO-backed slot is explicitly emptied via monkeypatch rather
than left to resolve. In a monorepo run with `baldur_pro` importable the slot
may already be registered, and an OSS-shape assertion would then silently
exercise the PRO path instead — the 709 false-pass lesson.

Covers:
- TestCleanupLaneDigestRecordingBehavior: the six call sites, their per-task
  gating, and the deliberately excluded approval-expiry lane.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.celery.tasks import dlq_replay as adapter_dlq_tasks
from baldur.celery_tasks import dlq_tasks as celery_dlq_tasks
from baldur.factory import ProviderRegistry
from baldur.interfaces.dlq import DLQService
from baldur.interfaces.statistics import StatisticsRepositoryInterface
from baldur.services.cleanup_service import CleanupResult, CleanupService
from baldur.services.daily_report import DailyReportCollector
from baldur.services.pending_config import PendingConfigService
from baldur.tasks import cleanup_tasks, config_apply

# =============================================================================
# Task drivers — each stubs its own backing and returns the task's result
# =============================================================================


def _stub_cleanup_service(monkeypatch, method: str, result: CleanupResult):
    """Pin `get_cleanup_service` to a service returning `result`."""
    service = MagicMock(spec=CleanupService)
    getattr(service, method).return_value = result
    monkeypatch.setattr(
        "baldur.services.cleanup_service.get_cleanup_service",
        lambda: service,
    )
    return service


def _drive_archive(monkeypatch, count: int) -> dict:
    _stub_cleanup_service(
        monkeypatch,
        "archive_old_dlq_entries",
        CleanupResult(
            success=True,
            operation="archived",
            count=count,
            details={"older_than_days": 30},
        ),
    )
    return cleanup_tasks.archive_old_dlq_entries(older_than_days=30)


def _drive_expired_config(monkeypatch, count: int) -> dict:
    _stub_cleanup_service(
        monkeypatch,
        "cleanup_expired_config",
        CleanupResult(
            success=True,
            operation="expired",
            count=count,
            details={"older_than_hours": 24},
        ),
    )
    return cleanup_tasks.cleanup_expired_config(older_than_hours=24)


def _drive_purge(monkeypatch, count: int) -> dict:
    _stub_cleanup_service(
        monkeypatch,
        "purge_archived_dlq_entries",
        CleanupResult(
            success=True,
            operation="purged",
            count=count,
            details={"older_than_days": 90, "dry_run": False},
        ),
    )
    return cleanup_tasks.purge_archived_dlq_entries(older_than_days=90)


def _drive_config_changes(monkeypatch, count: int) -> dict:
    service = MagicMock(spec=PendingConfigService)
    service.cleanup_expired.return_value = count
    monkeypatch.setattr(
        "baldur.services.pending_config.get_pending_config_service",
        lambda: service,
    )
    return config_apply.cleanup_expired_config_changes(max_age_hours=24)


def _drive_celery_dlq_cleanup(monkeypatch, count: int) -> dict:
    dlq_service = MagicMock(spec=DLQService)
    dlq_service.cleanup_old_entries.return_value = {
        "expired_count": count,
        "archived_count": count,
    }
    monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: dlq_service)
    return celery_dlq_tasks.cleanup_resolved_dlq_entries.apply(
        kwargs={"days_old": 30}, task_id="t"
    ).get()


def _drive_adapter_dlq_cleanup(monkeypatch, count: int) -> dict:
    stats_repo = MagicMock(spec=StatisticsRepositoryInterface)
    stats_repo.archive_old_entries.return_value = count
    monkeypatch.setattr(ProviderRegistry, "has_statistics_adapter", lambda: True)
    monkeypatch.setattr(ProviderRegistry, "get_statistics_repo", lambda: stats_repo)
    return adapter_dlq_tasks.cleanup_resolved_dlq_entries.apply(
        kwargs={"days_old": 30}, task_id="t"
    ).get()


# (registered task name, driver, the counter the task feeds)
_PRODUCERS = [
    pytest.param(
        "baldur.archive_old_dlq_entries",
        _drive_archive,
        "archived_count",
        id="archive_old_dlq_entries",
    ),
    pytest.param(
        "baldur.cleanup_expired_config",
        _drive_expired_config,
        "expired_count",
        id="cleanup_expired_config",
    ),
    pytest.param(
        "baldur.purge_archived_dlq_entries",
        _drive_purge,
        "purged_count",
        id="purge_archived_dlq_entries",
    ),
    pytest.param(
        "baldur.cleanup_expired_config_changes",
        _drive_config_changes,
        "expired_count",
        id="cleanup_expired_config_changes",
    ),
    pytest.param(
        "baldur.celery_tasks.cleanup_resolved_dlq_entries",
        _drive_celery_dlq_cleanup,
        "archived_count",
        id="celery_tasks_cleanup_resolved",
    ),
    pytest.param(
        "baldur.adapters.celery.tasks.cleanup_resolved_dlq_entries",
        _drive_adapter_dlq_cleanup,
        "archived_count",
        id="adapter_cleanup_resolved",
    ),
]


@pytest.fixture
def collector():
    """Patch the collector the shared helper resolves; yield the mock."""
    mock = MagicMock(spec=DailyReportCollector)
    with patch(
        "baldur.services.daily_report.aggregator.get_daily_report_collector",
        return_value=mock,
    ):
        yield mock


# =============================================================================
# Call sites — Behavior
# =============================================================================


class TestCleanupLaneDigestRecordingBehavior:
    """Each in-scope task reports its own work under its own task name."""

    @pytest.mark.parametrize(("task_name", "driver", "ingest_key"), _PRODUCERS)
    def test_task_records_its_result_when_work_was_done(
        self, collector, monkeypatch, task_name, driver, ingest_key
    ):
        """A run that processed entries pushes its result under its task name.

        The task name is asserted as a literal because it is the entry
        identity the report renders and the two same-named cleanup tasks are
        distinguishable only by it.
        """
        # When
        result = driver(monkeypatch, 4)

        # Then
        collector.add_result.assert_called_once_with(task_name=task_name, result=result)
        assert result[ingest_key] == 4

    @pytest.mark.parametrize(("task_name", "driver", "ingest_key"), _PRODUCERS)
    def test_task_with_nothing_to_clean_records_nothing(
        self, collector, monkeypatch, task_name, driver, ingest_key
    ):
        """An idle run adds no entry — the digest counts work, not runs.

        Without the helper's count gate every install would push an entry per
        cleanup task per schedule tick, evicting real entries from the day's
        bounded list.
        """
        result = driver(monkeypatch, 0)

        assert result[ingest_key] == 0
        collector.add_result.assert_not_called()

    # --- tier gating -------------------------------------------------------

    @pytest.mark.parametrize(
        ("task", "kwargs"),
        [
            pytest.param(
                cleanup_tasks.archive_old_dlq_entries,
                {"older_than_days": 30},
                id="archive",
            ),
            pytest.param(
                cleanup_tasks.purge_archived_dlq_entries,
                {"older_than_days": 90},
                id="purge",
            ),
        ],
    )
    def test_pro_gated_wrapper_on_oss_records_nothing(
        self, collector, monkeypatch, task, kwargs
    ):
        """With the PRO DLQ slot empty the task fails and must stay silent.

        On an OSS install these two run on every schedule tick and return a
        failure result. Recording it would add a `task_failures` bump to the
        digest every single day — the opposite of reporting cleanup.
        """
        # Given: the PRO slot is genuinely empty, not merely unregistered here
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)

        # When
        result = task(**kwargs)

        # Then
        assert result["success"] is False
        collector.add_result.assert_not_called()

    def test_pro_gated_celery_cleanup_on_oss_records_nothing(
        self, collector, monkeypatch
    ):
        """The PRO-backed celery cleanup task is silent on OSS too."""
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)

        result = celery_dlq_tasks.cleanup_resolved_dlq_entries.apply(
            kwargs={"days_old": 30}, task_id="t"
        ).get()

        assert result["success"] is False
        collector.add_result.assert_not_called()

    def test_adapter_cleanup_without_statistics_adapter_records_nothing(
        self, collector, monkeypatch
    ):
        """The `skipped` shape is a success with no counter — no entry.

        This shape is why the helper gates on a counter being present rather
        than on success alone: the task legitimately succeeds while doing
        nothing at all.
        """
        monkeypatch.setattr(ProviderRegistry, "has_statistics_adapter", lambda: False)

        result = adapter_dlq_tasks.cleanup_resolved_dlq_entries.apply(
            kwargs={"days_old": 30}, task_id="t"
        ).get()

        assert result["skipped"] is True
        collector.add_result.assert_not_called()

    def test_adapter_cleanup_is_the_oss_live_archived_producer(
        self, collector, monkeypatch
    ):
        """With PRO slots empty but a statistics adapter present, Archived is live.

        This is the per-tier claim: OSS renders a real Archived count only
        through this lane, since both PRO-backed archive paths are inert
        without a DLQ service.
        """
        # Given: pure-OSS registry posture, statistics adapter registered
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)
        stats_repo = MagicMock(spec=StatisticsRepositoryInterface)
        stats_repo.archive_old_entries.return_value = 12
        monkeypatch.setattr(ProviderRegistry, "has_statistics_adapter", lambda: True)
        monkeypatch.setattr(ProviderRegistry, "get_statistics_repo", lambda: stats_repo)

        # When
        result = adapter_dlq_tasks.cleanup_resolved_dlq_entries.apply(
            kwargs={"days_old": 30}, task_id="t"
        ).get()

        # Then
        assert result["archived_count"] == 12
        collector.add_result.assert_called_once_with(
            task_name="baldur.adapters.celery.tasks.cleanup_resolved_dlq_entries",
            result=result,
        )

    # --- dry run -----------------------------------------------------------

    def test_purge_dry_run_records_nothing(self, collector, monkeypatch):
        """A dry-run purge deleted nothing, so the digest reports nothing.

        The dry-run result is success-shaped and carries a non-zero
        `purged_count` (the would-purge count), so only the dry-run gate
        keeps it out.
        """
        _stub_cleanup_service(
            monkeypatch,
            "purge_archived_dlq_entries",
            CleanupResult(
                success=True,
                operation="purged",
                count=17,
                details={"older_than_days": 90, "dry_run": True},
            ),
        )

        result = cleanup_tasks.purge_archived_dlq_entries(
            older_than_days=90, dry_run=True
        )

        assert result["purged_count"] == 17
        collector.add_result.assert_not_called()

    # --- deliberate exclusion ----------------------------------------------

    def test_approval_expiry_is_not_recorded(self, collector, monkeypatch):
        """Approval expiry stays out of the digest's Expired counter.

        It produces an `expired_count` like the config lanes do, so wiring it
        would silently fold approval-request expiry into a counter that
        reports configuration cleanup.
        """
        _stub_cleanup_service(
            monkeypatch,
            "expire_approval_requests",
            CleanupResult(
                success=True,
                operation="expired",
                count=6,
                details={"older_than_hours": 72},
            ),
        )

        result = cleanup_tasks.expire_approval_requests(older_than_hours=72)

        assert result["expired_count"] == 6
        collector.add_result.assert_not_called()

    # --- fail-open ---------------------------------------------------------

    def test_collector_failure_does_not_fail_the_cleanup_task(self, monkeypatch):
        """A digest push failure never turns a successful cleanup into a failure."""
        broken = MagicMock(spec=DailyReportCollector)
        broken.add_result.side_effect = RuntimeError("collector down")

        with patch(
            "baldur.services.daily_report.aggregator.get_daily_report_collector",
            return_value=broken,
        ):
            result = _drive_expired_config(monkeypatch, 3)

        assert result["success"] is True
        assert result["expired_count"] == 3
