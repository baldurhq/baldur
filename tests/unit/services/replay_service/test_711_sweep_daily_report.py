"""
Tests for the on-recovery sweep's daily-report emission (711 D3).

The circuit-close sweep records an ``auto_replay_batch`` entry so the
digest's "Auto-replay" line can render without PRO. Emission lives in the
service layer (not the Celery task), so direct callers are covered too, and
operator-initiated ``replay_batch`` calls stay out of the automatic count.

Covers:
- TestSweepEntryPayloadContract: the exact entry payload — task name, key
  set, count mapping, `success_rate` rounding, and the deliberate absence of
  `success`/`error` keys (which would bump `task_failures` on ingest).
- TestRecordSweepInDailyReportBehavior: the `total > 0` gate and the
  fail-open posture when the collector is unavailable.
- TestCircuitCloseSweepDailyReportBehavior: which sweep exits emit and which
  do not, driven through the public `replay_on_circuit_close` entry point.

Posture: every service here pins the OSS NoOp governance checker instead of
letting `_get_governance()` resolve `ProviderRegistry.governance`. In a
PRO-present monorepo run an unpinned slot silently exercises the PRO checker
— the 709 false-pass lesson — so pure-OSS reachability would go unverified.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.interfaces.governance import NoOpGovernanceChecker
from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationRepository,
)
from baldur.models.governance import GovernanceCheckResult
from baldur.services.daily_report import DailyReportCollector
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.models import BatchReplayResult, ReplayResult
from baldur.services.replay_service.service import _replay_inflight_lock_name
from baldur.utils.time import utc_now

SERVICE_NAME = "payment_api"
FAILURE_TYPE_MAP = {SERVICE_NAME: ["TIMEOUT"]}

# =============================================================================
# Helpers
# =============================================================================


def _make_service(cache=None, entries: list | None = None) -> ReplayService:
    """Build a ReplayService in pure-OSS posture with a stubbed repository.

    `entries` seeds `find_replayable` so the sweep's batch size is
    controlled without a real repository; `_execute_replay` is stubbed by
    the callers that need a specific success/failure split.
    """
    repo = MagicMock(spec=FailedOperationRepository)
    repo.find_replayable.return_value = entries if entries else []
    svc = ReplayService(repository=repo, cache=cache)
    svc._event_bus = MagicMock(spec=BaldurEventBus)
    # See module docstring — pin OSS governance, never resolve the registry.
    svc._governance = NoOpGovernanceChecker()
    svc._governance_resolved = True
    return svc


def _dlq_entries(count: int) -> list:
    """Stub DLQ entries — only `.id` is read by the sweep body."""
    return [
        MagicMock(spec=FailedOperationData, id=f"dlq-{i}", status="pending")
        for i in range(count)
    ]


@contextmanager
def _patched_collector():
    """Patch the collector singleton the emission resolves at call time.

    `_record_sweep_in_daily_report` imports `get_daily_report_collector`
    from the package barrel inside the call, so patching the barrel
    attribute intercepts it.
    """
    collector = MagicMock(spec=DailyReportCollector)
    with patch(
        "baldur.services.daily_report.get_daily_report_collector",
        return_value=collector,
    ):
        yield collector


def _sole_entry_payload(collector: MagicMock) -> dict:
    """Extract the payload of the one entry the sweep pushed."""
    collector.add_result.assert_called_once()
    return collector.add_result.call_args.kwargs["result"]


# =============================================================================
# Entry payload — Contract (D3 key mapping, hardcoded)
# =============================================================================


class TestSweepEntryPayloadContract:
    """The `auto_replay_batch` entry shape is a cross-module contract.

    The daily-report ingest map reads these exact keys, so they are pinned
    as literals here rather than derived from the source.
    """

    def _emit(self, batch_result: BatchReplayResult) -> dict:
        svc = _make_service()
        with _patched_collector() as collector:
            svc._record_sweep_in_daily_report(SERVICE_NAME, batch_result)
        return _sole_entry_payload(collector)

    def test_entry_task_name_is_auto_replay_batch(self):
        """The task name is the ingest key `_collect_automated_actions_section` counts."""
        svc = _make_service()
        batch_result = BatchReplayResult(total=1, success_count=1)

        with _patched_collector() as collector:
            svc._record_sweep_in_daily_report(SERVICE_NAME, batch_result)

        assert collector.add_result.call_args.kwargs["task_name"] == "auto_replay_batch"

    def test_entry_payload_key_set_is_exact(self):
        """Payload carries exactly the five D3-mapped keys."""
        payload = self._emit(
            BatchReplayResult(total=4, success_count=3, failed_count=1)
        )

        assert set(payload) == {
            "recovered_count",
            "failed_count",
            "processed_count",
            "success_rate",
            "service_name",
        }

    def test_entry_maps_batch_counts_to_report_keys(self):
        """success_count→recovered_count, failed_count→failed_count, total→processed_count."""
        payload = self._emit(
            BatchReplayResult(total=4, success_count=3, failed_count=1)
        )

        assert payload["recovered_count"] == 3
        assert payload["failed_count"] == 1
        assert payload["processed_count"] == 4

    def test_entry_carries_recovered_service_name(self):
        """The recovered service is entry context for the detail API view."""
        payload = self._emit(BatchReplayResult(total=1, success_count=1))

        assert payload["service_name"] == SERVICE_NAME

    def test_success_rate_rounded_to_four_decimal_places(self):
        """A non-terminating ratio is rounded, not stored at full precision."""
        payload = self._emit(BatchReplayResult(total=3, success_count=1))

        assert payload["success_rate"] == 0.3333

    def test_success_rate_is_one_when_every_entry_replayed(self):
        """A fully successful sweep reports a 1.0 rate."""
        payload = self._emit(BatchReplayResult(total=2, success_count=2))

        assert payload["success_rate"] == 1.0

    def test_success_rate_is_zero_when_no_entry_replayed(self):
        """An all-failed sweep reports a 0.0 rate, not a division error."""
        payload = self._emit(BatchReplayResult(total=2, failed_count=2))

        assert payload["success_rate"] == 0.0

    def test_entry_omits_success_and_error_keys(self):
        """`success`/`error` would bump `task_failures` on ingest — must be absent.

        The daily-report field mapping treats an entry carrying `error`, or
        `success is False`, as a task failure. A sweep that replayed some
        entries unsuccessfully is not a failed task, so neither key is sent.
        """
        payload = self._emit(
            BatchReplayResult(total=2, success_count=1, failed_count=1)
        )

        assert "success" not in payload
        assert "error" not in payload


# =============================================================================
# _record_sweep_in_daily_report — Behavior
# =============================================================================


class TestRecordSweepInDailyReportBehavior:
    """The emission gate and its fail-open posture."""

    @pytest.mark.parametrize(
        ("total", "should_emit"),
        [
            pytest.param(0, False, id="empty_sweep_silent"),
            pytest.param(1, True, id="single_entry_emits"),
            pytest.param(50, True, id="full_batch_emits"),
        ],
    )
    def test_emits_only_when_sweep_processed_entries(self, total, should_emit):
        """The `total > 0` boundary — an empty sweep records nothing.

        Without the gate, every CB recovery on an idle service would add a
        zero-count "Auto-replay" batch to the digest.

        The empty case also asserts silence rather than a swallowed error:
        with the gate removed, `success_rate` divides by zero and the
        fail-open `except` hides it, so a call-count assertion alone would
        still pass on a gate-less implementation.
        """
        svc = _make_service()
        batch_result = BatchReplayResult(total=total, success_count=total)

        with _patched_collector() as collector, capture_logs() as logs:
            svc._record_sweep_in_daily_report(SERVICE_NAME, batch_result)

        assert collector.add_result.called is should_emit
        assert not [
            log
            for log in logs
            if log["event"] == "replay_service.daily_report_record_failed"
        ]

    def test_collector_failure_does_not_break_the_sweep(self):
        """A collector failure is swallowed — observability never fails the replay."""
        svc = _make_service()
        collector = MagicMock(spec=DailyReportCollector)
        collector.add_result.side_effect = RuntimeError("collector down")

        with patch(
            "baldur.services.daily_report.get_daily_report_collector",
            return_value=collector,
        ):
            svc._record_sweep_in_daily_report(
                SERVICE_NAME, BatchReplayResult(total=1, success_count=1)
            )

        collector.add_result.assert_called_once()

    def test_collector_failure_logs_warning_with_context(self):
        """The swallowed failure still surfaces at WARNING with the service name."""
        svc = _make_service()
        collector = MagicMock(spec=DailyReportCollector)
        collector.add_result.side_effect = RuntimeError("collector down")

        with (
            patch(
                "baldur.services.daily_report.get_daily_report_collector",
                return_value=collector,
            ),
            capture_logs() as logs,
        ):
            svc._record_sweep_in_daily_report(
                SERVICE_NAME, BatchReplayResult(total=1, success_count=1)
            )

        record = next(
            log
            for log in logs
            if log["event"] == "replay_service.daily_report_record_failed"
        )
        assert record["log_level"] == "warning"
        assert record["service_name"] == SERVICE_NAME
        assert "collector down" in record["error"]

    def test_collector_resolution_failure_is_swallowed(self):
        """A collector that cannot even be constructed is fail-open too."""
        svc = _make_service()

        with (
            patch(
                "baldur.services.daily_report.get_daily_report_collector",
                side_effect=RuntimeError("registry unavailable"),
            ),
            capture_logs() as logs,
        ):
            svc._record_sweep_in_daily_report(
                SERVICE_NAME, BatchReplayResult(total=1, success_count=1)
            )

        assert any(
            log["event"] == "replay_service.daily_report_record_failed" for log in logs
        )


# =============================================================================
# Sweep exit paths — Behavior (end-to-end through the public entry point)
# =============================================================================


class TestCircuitCloseSweepDailyReportBehavior:
    """Only a sweep that actually replayed entries reaches the collector.

    Driven through `replay_on_circuit_close` so the early-exit branches are
    exercised as they occur in production, not simulated.
    """

    def test_completed_sweep_emits_entry_with_replayed_counts(self):
        """A pure-OSS sweep that replayed entries records them for the digest."""
        # Given: two eligible entries, one replaying successfully, one not
        svc = _make_service(entries=_dlq_entries(2))
        svc._execute_replay = MagicMock(
            spec=svc._execute_replay,
            side_effect=[
                ReplayResult.succeeded(dlq_id="dlq-0"),
                ReplayResult.failed(dlq_id="dlq-1", error="still failing"),
            ],
        )
        svc.repository.get_by_id.return_value = None

        # When
        with _patched_collector() as collector:
            result = svc.replay_on_circuit_close(
                service_name=SERVICE_NAME,
                max_items=2,
                service_failure_type_map=FAILURE_TYPE_MAP,
            )

        # Then
        assert result.total == 2
        payload = _sole_entry_payload(collector)
        assert payload["processed_count"] == 2
        assert payload["recovered_count"] == 1
        assert payload["failed_count"] == 1

    def test_empty_backlog_does_not_emit(self):
        """A recovery with nothing to replay leaves the digest untouched."""
        svc = _make_service(entries=[])

        with _patched_collector() as collector, capture_logs() as logs:
            result = svc.replay_on_circuit_close(
                service_name=SERVICE_NAME,
                service_failure_type_map=FAILURE_TYPE_MAP,
            )

        assert result.total == 0
        collector.add_result.assert_not_called()
        # Silence, not a swallowed division-by-zero (see the gate boundary test).
        assert not [
            log
            for log in logs
            if log["event"] == "replay_service.daily_report_record_failed"
        ]

    def test_unmapped_service_does_not_emit(self):
        """The no-failure-type-mapping exit returns before any replay."""
        svc = _make_service(entries=_dlq_entries(2))

        with _patched_collector() as collector:
            result = svc.replay_on_circuit_close(
                service_name=SERVICE_NAME,
                service_failure_type_map={},
            )

        assert result.total == 0
        collector.add_result.assert_not_called()

    def test_governance_block_does_not_emit(self):
        """A governance-blocked sweep replayed nothing, so it records nothing."""
        svc = _make_service(entries=_dlq_entries(2))
        blocked = MagicMock(spec=NoOpGovernanceChecker)
        blocked.check_all_governance.return_value = GovernanceCheckResult(
            allowed=False, block_message="kill switch engaged"
        )
        svc._governance = blocked

        with _patched_collector() as collector:
            result = svc.replay_on_circuit_close(
                service_name=SERVICE_NAME,
                service_failure_type_map=FAILURE_TYPE_MAP,
            )

        assert result.governance_blocked is True
        collector.add_result.assert_not_called()

    def test_inflight_skipped_sweep_does_not_emit(self):
        """A duplicate CB-close delivery is suppressed before the sweep body.

        Guards the digest against double-counting one logical recovery that
        the broker delivered twice.
        """
        # Given: the per-service inflight lock is already held
        cache = InMemoryCacheAdapter()
        holder = cache.get_lock(
            name=_replay_inflight_lock_name(SERVICE_NAME),
            timeout=timedelta(seconds=300),
        )
        assert holder.acquire(blocking=False) is True
        svc = _make_service(cache=cache, entries=_dlq_entries(2))
        svc._execute_replay = MagicMock(
            spec=svc._execute_replay,
            return_value=ReplayResult.succeeded(dlq_id="dlq-0"),
        )

        # When
        with _patched_collector() as collector:
            result = svc.replay_on_circuit_close(
                service_name=SERVICE_NAME,
                max_items=2,
                service_failure_type_map=FAILURE_TYPE_MAP,
            )

        # Then
        assert result.inflight_skipped is True
        collector.add_result.assert_not_called()

    def test_operator_batch_replay_does_not_emit_auto_replay_entry(self):
        """`replay_batch` is operator-initiated — it must stay out of the automatic count.

        D3 rejected emitting from the shared `_record_batch_completion` seam
        for exactly this reason: a console-driven batch is not an automatic
        recovery and would inflate the digest's "Auto-replay" line.
        """
        svc = _make_service(entries=_dlq_entries(2))
        svc._execute_replay = MagicMock(
            spec=svc._execute_replay,
            return_value=ReplayResult.succeeded(dlq_id="dlq-0"),
        )

        with _patched_collector() as collector:
            svc.replay_batch(max_items=2)

        collector.add_result.assert_not_called()


# =============================================================================
# Producer → digest round trip — Behavior
# =============================================================================


class TestSweepEntryRendersAutoReplayLineBehavior:
    """The emitted entry actually renders the digest's "Auto-replay" line.

    This is the end of the chain 711 exists to close: before the sweep
    emitted anything, the line could never render without PRO. Asserting the
    payload shape alone would not catch a producer/ingest key mismatch, so
    the entry fed to the digest here is the one the sweep really produced —
    never a hand-written dict — and the assertion is the operator-visible
    string.
    """

    def _emitted_payload(self) -> dict:
        """Run a real sweep and return the entry payload it pushed."""
        svc = _make_service(entries=_dlq_entries(3))
        svc._execute_replay = MagicMock(
            spec=svc._execute_replay,
            side_effect=[
                ReplayResult.succeeded(dlq_id="dlq-0"),
                ReplayResult.succeeded(dlq_id="dlq-1"),
                ReplayResult.failed(dlq_id="dlq-2", error="still failing"),
            ],
        )
        svc.repository.get_by_id.return_value = None

        with _patched_collector() as collector:
            svc.replay_on_circuit_close(
                service_name=SERVICE_NAME,
                max_items=3,
                service_failure_type_map=FAILURE_TYPE_MAP,
            )

        return _sole_entry_payload(collector)

    def test_pure_oss_sweep_renders_auto_replay_line_in_digest(self):
        """A sweep entry survives ingest and reaches the rendered Slack digest."""
        from baldur.services.daily_report.formatters import format_report_for_slack
        from baldur.services.daily_report.models import (
            DailyAutonomousReport,
            TaskResultEntry,
        )
        from baldur.services.daily_report.service import DailyReportService

        # Given: a report holding exactly the entry the sweep emitted
        report = DailyAutonomousReport()
        report.entries.append(
            TaskResultEntry(
                task_name="auto_replay_batch",
                result=self._emitted_payload(),
                timestamp=utc_now(),
            )
        )

        # When: the digest aggregates and renders it
        DailyReportService.__new__(
            DailyReportService
        )._collect_automated_actions_section(report)
        output = format_report_for_slack(report)

        # Then: the OSS operator sees the recovery work attributed correctly
        assert "Auto-replay: 1 batches, 2 recovered / 1 failed" in output
