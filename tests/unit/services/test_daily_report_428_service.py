"""
Unit tests for 428 Daily Report — Service layer additions.

Test targets:
  - DailyReportService._persist_report() — 90-day state backend persistence with
    sliding TTL and date pruning (Phase 2 / D4).
  - DailyReportService._collect_dlq_pending_breakdown() — FailedOperationRepository
    -> DLQPendingBreakdown (Phase 5 / D9).
  - DailyReportService._collect_automated_actions_section() — PRO entry aggregation
    into AutomatedActionsSummary (Phase 3 / D7).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from freezegun import freeze_time

from baldur.services.daily_report.models import (
    _RECOMMENDED_ACTIONS,
    DailyAutonomousReport,
    TaskResultEntry,
)
from baldur.services.daily_report.service import (
    _LEGACY_UMBRELLA_KEY,
    _PRUNE_SWEEP_BUFFER_DAYS,
    _STATE_BACKEND_KEY_PREFIX,
    DailyReportService,
)

# =============================================================================
# _persist_report() — State backend persistence
# =============================================================================


class TestPersistReportBehavior:
    """Functional tests for per-date state backend persistence (430 D1/D4)."""

    def test_persist_writes_under_per_date_key(self):
        """Report persisted under 'baldur:daily_reports:{YYYY-MM-DD}'."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.archived_count = 7
        date = datetime(2026, 4, 10, tzinfo=UTC)

        mock_backend = MagicMock()
        mock_backend.get.return_value = None  # No legacy umbrella
        mock_settings = MagicMock(keep_reports_days=90)

        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=mock_backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
        ):
            svc._persist_report(report, date)

        # Exactly one per-date write for today's report (no umbrella load).
        set_calls = [
            c
            for c in mock_backend.set.call_args_list
            if c.args[0].startswith(_STATE_BACKEND_KEY_PREFIX)
        ]
        assert len(set_calls) == 1
        assert set_calls[0].args[0] == f"{_STATE_BACKEND_KEY_PREFIX}2026-04-10"
        stored = set_calls[0].args[1]
        assert stored["archived_count"] == 7

    def test_persist_passes_include_entries_true(self):
        """to_dict(include_entries=True) is used so detail view has per-entry context."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.entries.append(
            TaskResultEntry(
                task_name="dlq_item_created",
                result={"domain": "payment"},
                timestamp=datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
            )
        )
        date = datetime(2026, 4, 10, tzinfo=UTC)

        mock_backend = MagicMock()
        mock_backend.get.return_value = None
        mock_settings = MagicMock(keep_reports_days=30)

        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=mock_backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
        ):
            svc._persist_report(report, date)

        persisted = mock_backend.set.call_args.args[1]
        assert "entries" in persisted
        assert persisted["entries"][0]["task_name"] == "dlq_item_created"
        assert persisted["entries"][0]["result"]["domain"] == "payment"

    def test_persist_ttl_is_keep_days_plus_seven_days(self):
        """TTL = (keep_days + 7) * 86400 seconds (sliding margin)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        date = datetime(2026, 4, 10, tzinfo=UTC)

        mock_backend = MagicMock()
        mock_backend.get.return_value = None
        mock_settings = MagicMock(keep_reports_days=90)

        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=mock_backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
        ):
            svc._persist_report(report, date)

        assert mock_backend.set.call_args.kwargs["ttl_seconds"] == (90 + 7) * 86400

    def test_persist_sweep_deletes_window_of_stale_date_keys(self):
        """Sweep deletes keys [keep_days, keep_days + buffer) days past today."""
        svc = DailyReportService()
        report = DailyAutonomousReport()

        fixed_now = datetime(2026, 4, 10, tzinfo=UTC)
        date = fixed_now
        keep_days = 30

        mock_backend = MagicMock()
        mock_backend.get.return_value = None  # No legacy umbrella
        mock_settings = MagicMock(keep_reports_days=keep_days)

        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=mock_backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.utils.time.utc_now",
                return_value=fixed_now,
            ),
        ):
            svc._persist_report(report, date)

        deleted_keys = [c.args[0] for c in mock_backend.delete.call_args_list]
        # Expected: exactly _PRUNE_SWEEP_BUFFER_DAYS delete calls, covering
        # days [keep_days, keep_days + buffer) back from fixed_now.
        assert len(deleted_keys) == _PRUNE_SWEEP_BUFFER_DAYS
        from datetime import timedelta

        for i in range(keep_days, keep_days + _PRUNE_SWEEP_BUFFER_DAYS):
            expected_date = (fixed_now - timedelta(days=i)).strftime("%Y-%m-%d")
            assert f"{_STATE_BACKEND_KEY_PREFIX}{expected_date}" in deleted_keys

    def test_persist_fails_open_on_backend_error(self):
        """State backend exception does not propagate — daily report succeeds."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        date = datetime(2026, 4, 10, tzinfo=UTC)

        mock_backend = MagicMock()
        mock_backend.get.side_effect = RuntimeError("redis down")
        mock_settings = MagicMock(keep_reports_days=90)

        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=mock_backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
        ):
            # When / Then — must not raise
            svc._persist_report(report, date)

        # set() never called because migration read via get() raised first
        mock_backend.set.assert_not_called()

    def test_persist_overwrites_same_date_key(self):
        """Persisting for a date that already has a per-date key overwrites it."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.archived_count = 42  # Updated value
        date = datetime(2026, 4, 10, tzinfo=UTC)

        mock_backend = MagicMock()
        mock_backend.get.return_value = None  # No legacy umbrella
        mock_settings = MagicMock(keep_reports_days=90)

        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=mock_backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
        ):
            svc._persist_report(report, date)

        # Single set() to the per-date key with the new value (no merge).
        set_call = mock_backend.set.call_args
        assert set_call.args[0] == f"{_STATE_BACKEND_KEY_PREFIX}2026-04-10"
        assert set_call.args[1]["archived_count"] == 42


class TestPersistReportContract:
    """Contract values for state backend persistence (430)."""

    def test_state_backend_key_prefix_value(self):
        """Per-date key prefix contract."""
        assert _STATE_BACKEND_KEY_PREFIX == "baldur:daily_reports:"

    def test_legacy_umbrella_key_value(self):
        """Legacy umbrella key contract (for migration)."""
        assert _LEGACY_UMBRELLA_KEY == "baldur:daily_reports"


# Pin the clock: the migration prune cutoff keys off utc_now(), so these
# date-hardcoded migration cases must freeze time or they rot once wall-clock
# drifts past keep_reports_days from the hardcoded legacy dates.
@freeze_time("2026-04-10")
class TestPersistReportMigrationBehavior:
    """Legacy umbrella migration (430 D4) — idempotent one-shot migration."""

    def test_persist_migrates_legacy_umbrella_and_deletes_it(self):
        """Given a legacy umbrella, each date is written as a per-date key
        and the umbrella key is deleted."""
        # Given
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.archived_count = 11
        date = datetime(2026, 4, 10, tzinfo=UTC)

        legacy_umbrella = {
            "2026-03-20": {"archived_count": 1},
            "2026-04-01": {"archived_count": 2},
        }

        # Use a real in-memory backend so ordering of set/delete can be
        # checked via the resulting state rather than brittle call_args
        # sequences.
        from baldur.core.state_backend import MemoryStateBackend

        backend = MemoryStateBackend()
        backend.set(_LEGACY_UMBRELLA_KEY, legacy_umbrella)

        mock_settings = MagicMock(keep_reports_days=90)

        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
        ):
            # When
            svc._persist_report(report, date)

        # Then — umbrella gone
        assert backend.get(_LEGACY_UMBRELLA_KEY) is None
        # Legacy dates migrated to per-date keys (values preserved)
        for legacy_date, expected in legacy_umbrella.items():
            migrated = backend.get(f"{_STATE_BACKEND_KEY_PREFIX}{legacy_date}")
            assert migrated == expected
        # Today's report also present
        today_key = backend.get(f"{_STATE_BACKEND_KEY_PREFIX}2026-04-10")
        assert today_key is not None
        assert today_key["archived_count"] == 11

    def test_persist_migration_is_noop_on_clean_install(self):
        """No legacy umbrella in backend -> migration performs no additional
        writes beyond today's report, and no delete is issued against the
        umbrella key."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        date = datetime(2026, 4, 10, tzinfo=UTC)

        mock_backend = MagicMock()
        mock_backend.get.return_value = None
        mock_settings = MagicMock(keep_reports_days=90)

        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=mock_backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
        ):
            svc._persist_report(report, date)

        # Exactly one set() for today's per-date key.
        set_keys = [c.args[0] for c in mock_backend.set.call_args_list]
        assert set_keys == [f"{_STATE_BACKEND_KEY_PREFIX}2026-04-10"]
        # No delete against the legacy umbrella key.
        deleted_keys = [c.args[0] for c in mock_backend.delete.call_args_list]
        assert _LEGACY_UMBRELLA_KEY not in deleted_keys

    def test_persist_migration_resumes_safely_on_second_invocation(self):
        """Re-running persist after a completed migration is a strict no-op
        for legacy dates — no per-date writes are issued on the second call
        because the umbrella is already gone."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        fixed_now = datetime(2026, 4, 10, tzinfo=UTC)
        date = fixed_now
        # Use a recent date so migration cutoff does not skip it.
        legacy_date_str = (fixed_now - timedelta(days=5)).strftime("%Y-%m-%d")

        legacy_umbrella = {legacy_date_str: {"archived_count": 1}}

        from baldur.core.state_backend import MemoryStateBackend

        backend = MemoryStateBackend()
        backend.set(_LEGACY_UMBRELLA_KEY, legacy_umbrella)

        mock_settings = MagicMock(keep_reports_days=90)

        # Spy the backend's set() so we can count migration-writes per call.
        real_set = backend.set
        set_spy = MagicMock(side_effect=real_set)
        backend.set = set_spy

        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.utils.time.utc_now",
                return_value=fixed_now,
            ),
        ):
            # First call — migrates legacy date + writes today.
            svc._persist_report(report, date)
            first_call_migration_writes = [
                c
                for c in set_spy.call_args_list
                if c.args[0] == f"{_STATE_BACKEND_KEY_PREFIX}{legacy_date_str}"
            ]
            set_spy.reset_mock()

            # Second call — umbrella already absent, migration must no-op.
            svc._persist_report(report, date)
            second_call_migration_writes = [
                c
                for c in set_spy.call_args_list
                if c.args[0] == f"{_STATE_BACKEND_KEY_PREFIX}{legacy_date_str}"
            ]

        # First call wrote the legacy date exactly once.
        assert len(first_call_migration_writes) == 1
        # Second call issued zero migration writes for the legacy date.
        assert len(second_call_migration_writes) == 0
        # Migrated data intact; umbrella still absent.
        assert (
            backend.get(f"{_STATE_BACKEND_KEY_PREFIX}{legacy_date_str}")
            == legacy_umbrella[legacy_date_str]
        )
        assert backend.get(_LEGACY_UMBRELLA_KEY) is None

    def test_persist_migration_skips_stale_dates_beyond_cutoff(self):
        """Legacy dates strictly older than (today - keep_days) are NOT
        migrated to per-date keys, preventing indefinite retention on
        File/Memory backends that ignore TTL. Umbrella is still deleted."""
        svc = DailyReportService()
        report = DailyAutonomousReport()

        fixed_now = datetime(2026, 4, 10, tzinfo=UTC)
        date = fixed_now
        keep_days = 30
        # Cutoff (strict): 2026-03-11. 2026-01-01 is stale; 2026-04-01 is fresh.
        legacy_umbrella = {
            "2026-01-01": {"archived_count": 1},  # stale — skipped
            "2026-04-01": {"archived_count": 2},  # within window — migrated
        }

        from baldur.core.state_backend import MemoryStateBackend

        backend = MemoryStateBackend()
        backend.set(_LEGACY_UMBRELLA_KEY, legacy_umbrella)

        mock_settings = MagicMock(keep_reports_days=keep_days)

        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.utils.time.utc_now",
                return_value=fixed_now,
            ),
        ):
            svc._persist_report(report, date)

        # Stale date was skipped.
        assert backend.get(f"{_STATE_BACKEND_KEY_PREFIX}2026-01-01") is None
        # Fresh date was migrated.
        assert (
            backend.get(f"{_STATE_BACKEND_KEY_PREFIX}2026-04-01")
            == legacy_umbrella["2026-04-01"]
        )
        # Umbrella deleted regardless of skipped dates.
        assert backend.get(_LEGACY_UMBRELLA_KEY) is None

    def test_persist_migration_boundary_cutoff_date_is_skipped_day_after_is_migrated(
        self,
    ):
        """Boundary: the comparison is ``<=`` so ``date_str == cutoff_date_str``
        is skipped (matches the sweep which deletes ``today - keep_days``).
        The first day INSIDE retention (``cutoff + 1 day``) is migrated."""
        # Given
        svc = DailyReportService()
        report = DailyAutonomousReport()
        fixed_now = datetime(2026, 4, 10, tzinfo=UTC)
        keep_days = 30
        # cutoff_date_str in code: (today - keep_days).strftime -> 2026-03-11
        cutoff_date_str = (fixed_now - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        # day_inside_retention: cutoff + 1 day = first day still within keep_days.
        day_inside_retention = (fixed_now - timedelta(days=keep_days - 1)).strftime(
            "%Y-%m-%d"
        )
        legacy_umbrella = {
            cutoff_date_str: {"archived_count": 1},  # == cutoff -> skipped
            day_inside_retention: {"archived_count": 2},  # > cutoff -> migrated
        }

        from baldur.core.state_backend import MemoryStateBackend

        backend = MemoryStateBackend()
        backend.set(_LEGACY_UMBRELLA_KEY, legacy_umbrella)
        mock_settings = MagicMock(keep_reports_days=keep_days)

        # When
        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.utils.time.utc_now",
                return_value=fixed_now,
            ),
        ):
            svc._persist_report(report, fixed_now)

        # Then — cutoff date skipped (matches sweep boundary).
        assert backend.get(f"{_STATE_BACKEND_KEY_PREFIX}{cutoff_date_str}") is None
        # First day inside retention migrated and survives the sweep.
        assert (
            backend.get(f"{_STATE_BACKEND_KEY_PREFIX}{day_inside_retention}")
            == legacy_umbrella[day_inside_retention]
        )

    def test_persist_migration_all_stale_dates_deletes_umbrella_with_zero_writes(
        self,
    ):
        """Edge: every legacy date is older than cutoff -> 0 per-date writes,
        but the umbrella is still deleted so the migration does not retry on
        every subsequent save."""
        # Given
        svc = DailyReportService()
        report = DailyAutonomousReport()
        fixed_now = datetime(2026, 4, 10, tzinfo=UTC)
        keep_days = 30
        # Both dates are well before the cutoff (2026-03-11).
        legacy_umbrella = {
            "2026-01-01": {"archived_count": 1},
            "2026-02-01": {"archived_count": 2},
        }

        from baldur.core.state_backend import MemoryStateBackend

        backend = MemoryStateBackend()
        backend.set(_LEGACY_UMBRELLA_KEY, legacy_umbrella)

        # Spy set() to count which keys were written.
        real_set = backend.set
        set_spy = MagicMock(side_effect=real_set)
        backend.set = set_spy

        mock_settings = MagicMock(keep_reports_days=keep_days)

        # When
        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.utils.time.utc_now",
                return_value=fixed_now,
            ),
        ):
            svc._persist_report(report, fixed_now)

        # Then — neither stale date migrated.
        stale_prefixes = (
            f"{_STATE_BACKEND_KEY_PREFIX}2026-01-01",
            f"{_STATE_BACKEND_KEY_PREFIX}2026-02-01",
        )
        for stale_key in stale_prefixes:
            assert backend.get(stale_key) is None

        migration_writes = [
            c for c in set_spy.call_args_list if c.args[0] in stale_prefixes
        ]
        assert migration_writes == []

        # Umbrella still deleted — migration self-disables on next call.
        assert backend.get(_LEGACY_UMBRELLA_KEY) is None

    def test_persist_migration_log_emits_correct_migrated_and_skipped_counts(self):
        """Side effect: ``umbrella_migrated`` INFO log carries the correct
        ``dates_migrated`` and ``dates_skipped_stale`` counts so operators
        can see migration progress / cleanup from a single log line."""
        # Given
        svc = DailyReportService()
        report = DailyAutonomousReport()
        fixed_now = datetime(2026, 4, 10, tzinfo=UTC)
        keep_days = 30
        legacy_umbrella = {
            "2026-01-01": {"archived_count": 1},  # stale
            "2026-02-01": {"archived_count": 2},  # stale
            "2026-04-01": {"archived_count": 3},  # fresh -> migrated
        }

        from baldur.core.state_backend import MemoryStateBackend

        backend = MemoryStateBackend()
        backend.set(_LEGACY_UMBRELLA_KEY, legacy_umbrella)
        mock_settings = MagicMock(keep_reports_days=keep_days)

        # When
        with (
            patch(
                "baldur.core.state_backend.get_state_backend",
                return_value=backend,
            ),
            patch(
                "baldur.settings.daily_report.get_daily_report_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur.utils.time.utc_now",
                return_value=fixed_now,
            ),
            patch("baldur.services.daily_report.service.logger") as mock_logger,
        ):
            svc._persist_report(report, fixed_now)

        # Then — find the umbrella_migrated INFO call.
        migration_log_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and c.args[0] == "daily_report_service.umbrella_migrated"
        ]
        assert len(migration_log_calls) == 1
        kwargs = migration_log_calls[0].kwargs
        assert kwargs["dates_migrated"] == 1
        assert kwargs["dates_skipped_stale"] == 2


# =============================================================================
# _collect_dlq_pending_breakdown() — DLQ breakdown collection (D9)
# =============================================================================


class TestCollectDlqPendingBreakdownBehavior:
    """Functional tests for DLQ pending breakdown (D9)."""

    def _make_repo_mock(self, stats: dict) -> MagicMock:
        repo = MagicMock()
        repo.get_statistics.return_value = stats
        return repo

    def test_breakdown_omitted_when_both_pending_maps_empty(self):
        """Both pending_by_domain and ...and_failure_type empty -> early return."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        repo = self._make_repo_mock(
            {
                "pending_by_domain": {},
                "pending_by_domain_and_failure_type": {},
            }
        )

        with patch(
            "baldur.factory.ProviderRegistry.get_failed_operation_repo",
            return_value=repo,
        ):
            svc._collect_dlq_pending_breakdown(report)

        assert report.dlq_pending_breakdown is None

    def test_breakdown_total_sums_pending_by_domain_values(self):
        """total = sum(pending_by_domain.values())."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        repo = self._make_repo_mock(
            {
                "pending_by_domain": {"payment": 3, "inventory": 5},
                "pending_by_domain_and_failure_type": {
                    "payment": {"NETWORK_ERROR": 3},
                    "inventory": {"TIMEOUT": 5},
                },
            }
        )

        with patch(
            "baldur.factory.ProviderRegistry.get_failed_operation_repo",
            return_value=repo,
        ):
            svc._collect_dlq_pending_breakdown(report)

        assert report.dlq_pending_breakdown is not None
        assert report.dlq_pending_breakdown.total == 8
        assert report.dlq_pending_breakdown.by_domain == {"payment": 3, "inventory": 5}

    def test_breakdown_aggregates_failure_type_across_domains(self):
        """Same failure_type across multiple domains aggregates count + domains list."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        repo = self._make_repo_mock(
            {
                "pending_by_domain": {"payment": 2, "billing": 4},
                "pending_by_domain_and_failure_type": {
                    "payment": {"TIMEOUT": 2},
                    "billing": {"TIMEOUT": 4},
                },
            }
        )

        with patch(
            "baldur.factory.ProviderRegistry.get_failed_operation_repo",
            return_value=repo,
        ):
            svc._collect_dlq_pending_breakdown(report)

        ft_map = report.dlq_pending_breakdown.by_failure_type
        assert "TIMEOUT" in ft_map
        assert ft_map["TIMEOUT"].count == 6
        assert set(ft_map["TIMEOUT"].domains) == {"payment", "billing"}

    def test_breakdown_uses_recommended_action_from_mapping(self):
        """Known failure_type gets its _RECOMMENDED_ACTIONS entry."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        repo = self._make_repo_mock(
            {
                "pending_by_domain": {"payment": 1},
                "pending_by_domain_and_failure_type": {
                    "payment": {"AUTH_ERROR": 1},
                },
            }
        )

        with patch(
            "baldur.factory.ProviderRegistry.get_failed_operation_repo",
            return_value=repo,
        ):
            svc._collect_dlq_pending_breakdown(report)

        action = report.dlq_pending_breakdown.by_failure_type["AUTH_ERROR"].action
        assert action == _RECOMMENDED_ACTIONS["AUTH_ERROR"]

    def test_breakdown_unknown_failure_type_uses_fallback_action(self):
        """Unknown failure_type uses 'Review and retry manually' fallback."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        repo = self._make_repo_mock(
            {
                "pending_by_domain": {"payment": 1},
                "pending_by_domain_and_failure_type": {
                    "payment": {"NOVEL_FAILURE": 1},
                },
            }
        )

        with patch(
            "baldur.factory.ProviderRegistry.get_failed_operation_repo",
            return_value=repo,
        ):
            svc._collect_dlq_pending_breakdown(report)

        action = report.dlq_pending_breakdown.by_failure_type["NOVEL_FAILURE"].action
        assert action == "Review and retry manually"

    def test_breakdown_domains_list_dedups_same_domain_multiple_failure_types(self):
        """Single domain with multiple failure_types appears once in each domain list."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        repo = self._make_repo_mock(
            {
                "pending_by_domain": {"payment": 5},
                "pending_by_domain_and_failure_type": {
                    "payment": {"TIMEOUT": 3, "AUTH_ERROR": 2},
                },
            }
        )

        with patch(
            "baldur.factory.ProviderRegistry.get_failed_operation_repo",
            return_value=repo,
        ):
            svc._collect_dlq_pending_breakdown(report)

        timeout_bd = report.dlq_pending_breakdown.by_failure_type["TIMEOUT"]
        auth_bd = report.dlq_pending_breakdown.by_failure_type["AUTH_ERROR"]
        assert timeout_bd.domains == ["payment"]
        assert auth_bd.domains == ["payment"]

    def test_breakdown_fails_open_on_repo_exception(self):
        """Repository exception -> breakdown stays None, no raise."""
        svc = DailyReportService()
        report = DailyAutonomousReport()

        with patch(
            "baldur.factory.ProviderRegistry.get_failed_operation_repo",
            side_effect=RuntimeError("repo missing"),
        ):
            svc._collect_dlq_pending_breakdown(report)

        assert report.dlq_pending_breakdown is None


# =============================================================================
# _collect_automated_actions_section() — PRO entry aggregation (D7)
# =============================================================================


class TestCollectAutomatedActionsSectionBehavior:
    """Functional tests for PRO automated actions aggregation."""

    def _entry(self, task_name: str, result: dict | None = None):
        return TaskResultEntry(
            task_name=task_name,
            result=result or {},
            timestamp=datetime(2026, 4, 10, tzinfo=UTC),
        )

    def test_no_matching_entries_leaves_summary_none(self):
        """No PRO task_names -> automated_actions_summary stays None."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.entries.append(self._entry("cleanup_archived"))

        svc._collect_automated_actions_section(report)

        assert report.automated_actions_summary is None

    def test_auto_replay_batch_aggregates_recovered_and_failed(self):
        """auto_replay_batch entries aggregate recovered_count + failed_count."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.entries.extend(
            [
                self._entry(
                    "auto_replay_batch", {"recovered_count": 3, "failed_count": 1}
                ),
                self._entry(
                    "auto_replay_batch", {"recovered_count": 2, "failed_count": 0}
                ),
            ]
        )

        svc._collect_automated_actions_section(report)

        s = report.automated_actions_summary
        assert s is not None
        assert s.auto_replay_batches == 2
        assert s.auto_replay_recovered == 5
        assert s.auto_replay_failed == 1

    def test_auto_replay_missing_count_fields_defaults_to_zero(self):
        """Missing recovered_count/failed_count in result -> treated as 0."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.entries.append(self._entry("auto_replay_batch", {}))

        svc._collect_automated_actions_section(report)

        s = report.automated_actions_summary
        assert s.auto_replay_batches == 1
        assert s.auto_replay_recovered == 0
        assert s.auto_replay_failed == 0

    def test_all_eight_task_name_prefixes_mapped(self):
        """All 8 task_name values map to the corresponding summary counter."""
        svc = DailyReportService()
        report = DailyAutonomousReport()

        report.entries.extend(
            [
                self._entry("auto_replay_batch"),
                self._entry("canary_rollout_completed"),
                self._entry("canary_rollback_triggered"),
                self._entry("auto_tuning_applied"),
                self._entry("emergency_level_changed"),
                self._entry("saga_completed"),
                self._entry("saga_compensated"),
                self._entry("governance_policy_blocked"),
            ]
        )

        svc._collect_automated_actions_section(report)

        s = report.automated_actions_summary
        assert s.auto_replay_batches == 1
        assert s.canary_completed == 1
        assert s.canary_rolled_back == 1
        assert s.auto_tuning_applied == 1
        assert s.emergency_level_changes == 1
        assert s.saga_completed == 1
        assert s.saga_compensated == 1
        assert s.governance_blocked == 1

    def test_entries_are_not_removed_after_aggregation(self):
        """Entries remain in report.entries for detail API access (Phase 4)."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.entries.append(self._entry("saga_completed"))

        svc._collect_automated_actions_section(report)

        # Entries kept so detail API can show per-event context
        assert len(report.entries) == 1
        assert report.entries[0].task_name == "saga_completed"

    def test_non_matching_task_name_is_ignored(self):
        """Entries with unrelated task_name don't affect summary."""
        svc = DailyReportService()
        report = DailyAutonomousReport()
        report.entries.extend(
            [
                self._entry("saga_completed"),
                self._entry("unrelated_task"),
            ]
        )

        svc._collect_automated_actions_section(report)

        s = report.automated_actions_summary
        assert s.saga_completed == 1
        # Only one action counted in the whole summary
        action_count_sum = (
            s.auto_replay_batches
            + s.canary_completed
            + s.canary_rolled_back
            + s.auto_tuning_applied
            + s.emergency_level_changes
            + s.saga_completed
            + s.saga_compensated
            + s.governance_blocked
        )
        assert action_count_sum == 1
