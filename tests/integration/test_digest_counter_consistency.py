"""Mock-based integration tests for 712 — the digest's counters tell the truth.

Composition under test (pure OSS, no PRO tier involved):

  1. Producer  -> the on-recovery replay sweep, and the cleanup lane's tasks
  2. Recording -> `_record_item_resolved` / `record_cleanup_result`
                  -> DLQMetricEventHandler.on_item_resolved
                  -> DailyReportCollector.add_result()
  3. Aggregation -> aggregate_daily_results() over the shared per-day list
  4. Render     -> format_report_for_slack()

The counters under test exist only *after* aggregation — a unit test of any
single link can show that a push happened, but not that the sweep's
resolutions and the cleanup lane's counts land in the same report and render
together. The digest-consistency property is specifically cross-producer: a
recovery sweep must appear on the Auto-replay line *and* in the DLQ
auto-resolved count, which are produced by two different call sites sharing
one cache list.

Infrastructure: InMemoryCacheAdapter behind ProviderRegistry.get_cache.
No Docker required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.interfaces.governance import NoOpGovernanceChecker
from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationRepository,
)
from baldur.services.cleanup_service import CleanupResult, CleanupService
from baldur.services.daily_report import (
    DailyReportService,
    aggregate_daily_results,
    format_report_for_slack,
)
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import ReplayResult, _replay_handlers
from baldur.services.replay_service.handlers import ReplayHandler
from baldur.tasks import cleanup_tasks

SERVICE_NAME = "payment_api"
DOMAIN = "payment"
FAILURE_TYPE = "PG_TIMEOUT"
FAILURE_TYPE_MAP = {SERVICE_NAME: [FAILURE_TYPE]}


class _SuccessHandler(ReplayHandler):
    """Replays every entry successfully so the sweep resolves them."""

    @property
    def domain(self) -> str:
        return DOMAIN

    def can_replay(self, failed_op) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op) -> ReplayResult:
        return ReplayResult.succeeded(failed_op.id, "OK")


@pytest.fixture
def cache_provider():
    return InMemoryCacheAdapter()


@pytest.fixture(autouse=True)
def _wire_cache(cache_provider):
    """Back the collector with an in-memory cache for the whole flow.

    Both the collector's writes and `aggregate_daily_results`' read resolve
    the provider at call time, so one patch covers the round trip.
    """
    with patch(
        "baldur.factory.ProviderRegistry.get_cache",
        return_value=cache_provider,
    ):
        yield


@pytest.fixture(autouse=True)
def _handler_registry():
    _replay_handlers.clear()
    _replay_handlers[DOMAIN] = _SuccessHandler()
    yield
    _replay_handlers.clear()


def _entries(count: int) -> list[FailedOperationData]:
    return [
        FailedOperationData(
            id=f"dlq-{i}",
            domain=DOMAIN,
            failure_type=FAILURE_TYPE,
            status="pending",
            retry_count=0,
            max_retries=2,
        )
        for i in range(count)
    ]


def _sweep_service(entries: list[FailedOperationData]):
    """A ReplayService whose sweep finds `entries`, in pure-OSS posture.

    Governance is pinned to the OSS NoOp checker rather than resolved from
    the registry — in a PRO-present run an unpinned slot would silently
    exercise the PRO checker (the 709 false-pass lesson).
    """
    from baldur.services.replay_service import ReplayService

    repo = MagicMock(spec=FailedOperationRepository)
    repo.find_replayable.return_value = entries
    repo.try_acquire_for_replay.side_effect = list(entries)
    repo.get_by_id.return_value = None
    svc = ReplayService(repository=repo)
    svc._event_bus = MagicMock(spec=BaldurEventBus)
    svc._governance = NoOpGovernanceChecker()
    svc._governance_resolved = True
    return svc


def _todays_report():
    """Aggregate today's entries and populate the automated-actions summary."""
    report = aggregate_daily_results(date=datetime.now(UTC))
    DailyReportService()._collect_automated_actions_section(report)
    return report


def _stub_cleanup_service(monkeypatch, method: str, result: CleanupResult):
    service = MagicMock(spec=CleanupService)
    getattr(service, method).return_value = result
    monkeypatch.setattr(
        "baldur.services.cleanup_service.get_cleanup_service",
        lambda: service,
    )


# =============================================================================
# Digest consistency — the sweep on both lines it feeds
# =============================================================================


class TestSweepDigestConsistency:
    """One recovery sweep must be reported consistently across the digest."""

    def test_sweep_appears_on_the_auto_replay_line_and_in_the_resolved_count(self):
        """A day whose only activity is one sweep reports it twice, agreeing.

        The Auto-replay line comes from the sweep's own batch emitter; the
        DLQ auto-resolved count comes from the per-entry resolution
        recording. Before 712 the second producer did not exist, so the same
        sweep showed up as N recovered on one line and 0 auto-resolved on the
        other — the digest contradicting itself.
        """
        # Given: three eligible entries, all replaying successfully
        svc = _sweep_service(_entries(3))

        # When
        batch = svc.replay_on_circuit_close(
            service_name=SERVICE_NAME,
            max_items=3,
            service_failure_type_map=FAILURE_TYPE_MAP,
        )
        report = _todays_report()

        # Then
        assert batch.success_count == 3
        assert report.automated_actions_summary.auto_replay_batches == 1
        assert report.automated_actions_summary.auto_replay_recovered == 3
        assert report.dlq_resolved_count >= batch.success_count

    def test_rendered_digest_shows_both_the_auto_replay_and_dlq_lines(self):
        """Both counters reach the operator, not just the aggregate object."""
        # Given
        svc = _sweep_service(_entries(2))

        # When
        svc.replay_on_circuit_close(
            service_name=SERVICE_NAME,
            max_items=2,
            service_failure_type_map=FAILURE_TYPE_MAP,
        )
        message = format_report_for_slack(_todays_report())

        # Then
        assert "• Auto-replay: 1 batches, 2 recovered / 0 failed" in message
        assert "2 auto-resolved" in message

    def test_idle_recovery_adds_nothing_to_either_counter(self):
        """A circuit close with an empty backlog leaves the digest untouched.

        Anchors the tests above to the sweep's actual work rather than to the
        recovery event: a recording moved outside the per-entry path would
        still produce counts here.
        """
        svc = _sweep_service([])

        svc.replay_on_circuit_close(
            service_name=SERVICE_NAME,
            service_failure_type_map=FAILURE_TYPE_MAP,
        )
        report = _todays_report()

        assert report.automated_actions_summary is None
        assert report.dlq_resolved_count == 0


# =============================================================================
# Cleanup lane — producer to rendered Auto-Processing line
# =============================================================================


class TestCleanupLaneDigestFlow:
    """The cleanup lane's counters survive aggregation and render."""

    def test_cleanup_run_renders_the_auto_processing_line(self, monkeypatch):
        """An expired-config cleanup reaches the operator's digest.

        Before 712 nothing in the cleanup lane pushed to the collector, so
        this section rendered 0 for Expired on every install — and a
        cleanup-only day suppressed the section entirely.
        """
        # Given
        _stub_cleanup_service(
            monkeypatch,
            "cleanup_expired_config",
            CleanupResult(success=True, operation="expired", count=8),
        )

        # When
        cleanup_tasks.cleanup_expired_config(older_than_hours=24)
        report = _todays_report()
        message = format_report_for_slack(report)

        # Then
        assert report.expired_count == 8
        assert "*📊 Auto-Processing Summary*" in message
        assert "Expired: 8" in message

    def test_cleanup_only_day_no_longer_suppresses_the_section(self, monkeypatch):
        """The section renders on cleanup work alone, with no replay activity."""
        _stub_cleanup_service(
            monkeypatch,
            "cleanup_expired_config",
            CleanupResult(success=True, operation="expired", count=1),
        )

        cleanup_tasks.cleanup_expired_config(older_than_hours=24)
        message = format_report_for_slack(_todays_report())

        assert "*📊 Auto-Processing Summary*" in message
        assert "*🤖 Automated Actions*" not in message

    def test_oss_failure_shaped_cleanup_run_reports_no_failure(self, monkeypatch):
        """A tier-gated task finding no backing service is not a daily failure.

        This is the shape an OSS install produces every single day, so a push
        here would render a permanent "Task failures: 1" in the digest.
        """
        # Given: the PRO-backed archive path returns its failure result
        _stub_cleanup_service(
            monkeypatch,
            "archive_old_dlq_entries",
            CleanupResult(
                success=False,
                operation="archived",
                error="baldur_pro DLQService not registered",
            ),
        )

        # When
        cleanup_tasks.archive_old_dlq_entries(older_than_days=30)
        report = _todays_report()

        # Then
        assert report.task_failures == 0
        assert report.archived_count == 0
        assert "*📊 Auto-Processing Summary*" not in format_report_for_slack(report)

    def test_cleanup_and_sweep_counts_coexist_in_one_report(self, monkeypatch):
        """Two independent producers share the day's list without clobbering.

        They write to the same per-day cache key, so this pins that
        aggregation sums across producers rather than the later one
        overwriting the earlier.
        """
        # Given: a cleanup run and a recovery sweep on the same day
        _stub_cleanup_service(
            monkeypatch,
            "cleanup_expired_config",
            CleanupResult(success=True, operation="expired", count=5),
        )
        cleanup_tasks.cleanup_expired_config(older_than_hours=24)
        svc = _sweep_service(_entries(2))

        # When
        svc.replay_on_circuit_close(
            service_name=SERVICE_NAME,
            max_items=2,
            service_failure_type_map=FAILURE_TYPE_MAP,
        )
        report = _todays_report()

        # Then
        assert report.expired_count == 5
        assert report.dlq_resolved_count == 2
        assert report.automated_actions_summary.auto_replay_recovered == 2
