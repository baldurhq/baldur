"""
Daily Report Service.

Thin Task, Fat Service principle:
- All daily report business logic lives here; tasks are thin wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import structlog

from baldur.audit.helpers import (
    log_daily_report_generated_audit,
    log_daily_report_send_failed_audit,
)
from baldur.core.serializable import SerializableMixin
from baldur.notification.helpers import notify
from baldur.services.event_bus.emitter import EventEmitterMixin

from .aggregator import aggregate_daily_results
from .formatters import _is_shipped, format_report_for_slack
from .models import (
    DailyAutonomousReport,
)

logger = structlog.get_logger()

# Persistence key layout (D1/D4 in 430):
# - Per-date keys hold individual day reports so no umbrella blob is read/written.
# - Legacy umbrella is migrated on first save after deploy, then deleted.
# - Pruning sweep covers a bounded window past the retention cutoff for
#   File/Memory backends that ignore TTL.
_STATE_BACKEND_KEY_PREFIX = "baldur:daily_reports:"
_LEGACY_UMBRELLA_KEY = "baldur:daily_reports"
_PRUNE_SWEEP_BUFFER_DAYS = 30

# Shadow PRO visibility policy (impl 452)
_INSTALL_MARKER_KEY = "baldur:install_marker:first_seen"
_SHADOW_PRO_GRACE_DAYS = 30


def _get_daily_report_recorder():
    """Lazy accessor for DailyReportMetricRecorder (graceful if metrics unavailable)."""
    try:
        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        if metrics._initialized:
            return metrics.daily_report
    except Exception:
        pass
    return None


@dataclass
class ReportResult(SerializableMixin):
    """Report generation result."""

    success: bool
    report: DailyAutonomousReport | None = None
    channels_sent: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize with structural reshape (Category C).

        Output differs from field structure: report object is decomposed
        into date + entry_count + summary, skip_reason is renamed to reason.
        """
        result: dict[str, Any] = {
            "success": self.success,
            "date": self.report.date.isoformat() if self.report else None,
            "channels_sent": self.channels_sent,
        }

        if self.skipped:
            result["skipped"] = True
            result["reason"] = self.skip_reason

        if self.error:
            result["error"] = self.error

        if self.report:
            result["entry_count"] = len(self.report.entries)
            result["summary"] = {
                "archived_count": self.report.archived_count,
                "expired_count": self.report.expired_count,
                "purged_count": self.report.purged_count,
                "recovered_count": self.report.recovered_count,
                "circuit_transitions": self.report.circuit_transitions,
                "task_failures": self.report.task_failures,
                "critical_alerts": self.report.critical_alerts,
                # DLQ (D8)
                "dlq_pending_count": self.report.dlq_pending_count,
                "dlq_new_entries_count": self.report.dlq_new_entries_count,
                "dlq_resolved_count": self.report.dlq_resolved_count,
            }
            # Typed summaries (conditional)
            if self.report.chaos_summary:
                result["summary"]["chaos"] = self.report.chaos_summary.to_dict()
            if self.report.load_shedding_summary:
                result["summary"]["load_shedding"] = (
                    self.report.load_shedding_summary.to_dict()
                )
            if self.report.error_budget_summary:
                result["summary"]["error_budget"] = (
                    self.report.error_budget_summary.to_dict()
                )
            if self.report.automated_actions_summary:
                result["summary"]["automated_actions"] = (
                    self.report.automated_actions_summary.to_dict()
                )
            if self.report.dlq_pending_breakdown:
                result["summary"]["dlq_pending_breakdown"] = (
                    self.report.dlq_pending_breakdown.to_dict()
                )

        return result


class DailyReportService(EventEmitterMixin):
    """
    Daily report generation and delivery service.

    All daily report generation logic is handled here.
    Tasks delegate to this service following the Thin Task, Fat Service principle.
    """

    _event_source = "daily_report_service"

    def generate_and_send_report(  # noqa: C901
        self,
        date: datetime | None = None,
        channels: list[str] | None = None,
    ) -> ReportResult:
        """
        Generate and send daily report.

        Args:
            date: Report target date (default: yesterday UTC)
            channels: Delivery channel list (default: from settings)

        Returns:
            ReportResult
        """
        from datetime import timedelta

        from baldur.utils.time import utc_now

        if channels is None:
            from baldur.settings.daily_report import get_daily_report_settings

            channels = list(get_daily_report_settings().default_channels)

        if date is None:
            date = utc_now() - timedelta(days=1)

        logger.info(
            "daily_report_service.generating_report",
            date=date,
        )

        try:
            # 1. Aggregate results
            report = aggregate_daily_results(date)

            # 2. Snapshot phase
            self._collect_snapshots(report, date)

            # 3. Skip if truly nothing to report.
            # All snapshot-populated summaries must be considered — a day with
            # no entries but non-empty DLQ pending backlog must still deliver.
            has_entries = len(report.entries) > 0
            has_snapshots = any(
                [
                    report.chaos_summary,
                    report.load_shedding_summary,
                    report.error_budget_summary,
                    report.shadow_pro_summary,
                    report.automated_actions_summary,
                    report.dlq_pending_breakdown,
                ]
            )
            if not has_entries and not has_snapshots:
                logger.info("daily_report_service.no_data_report_skipping")
                recorder = _get_daily_report_recorder()
                if recorder:
                    recorder.record_skipped("no_data")
                return ReportResult(
                    success=True,
                    report=report,
                    skipped=True,
                    skip_reason="no_data",
                )

            # 4. Send to channels
            sent_channels = []
            recorder = _get_daily_report_recorder()
            for channel in channels:
                try:
                    self._send_to_channel(report, channel)
                    sent_channels.append(channel)
                    if recorder:
                        recorder.record_delivery(channel, True)
                except Exception as e:
                    logger.exception(
                        "daily_report_service.send_failed",
                        channel=channel,
                        error=e,
                    )
                    if recorder:
                        recorder.record_delivery(channel, False)

                    # Emit DAILY_REPORT_SEND_FAILED event
                    try:
                        from baldur.services.event_bus.bus.event_types import (
                            EventType,
                        )

                        self._emit_event(
                            EventType.DAILY_REPORT_SEND_FAILED,
                            data={
                                "channel": channel,
                                "error": str(e),
                                "date": (
                                    date.strftime("%Y-%m-%d") if date else "unknown"
                                ),
                            },
                        )
                    except Exception:
                        pass  # Event emission is non-critical

                    # Audit trail for delivery failure
                    log_daily_report_send_failed_audit(channel, str(e))

            logger.info(
                "daily_report_service.report_sent_entries_channels",
                entries_count=len(report.entries),
                sent_channels=sent_channels,
            )

            if recorder:
                recorder.record_generated()

            # Persist report to state backend for 90-day history query (D4).
            # Run after delivery so report is preserved regardless of delivery
            # success — fail-open: persistence failure never fails delivery.
            self._persist_report(report, date)

            # Audit trail for successful report generation
            date_str = date.strftime("%Y-%m-%d") if date else "unknown"
            log_daily_report_generated_audit(date_str, sent_channels)

            return ReportResult(
                success=True,
                report=report,
                channels_sent=sent_channels,
            )

        except Exception as e:
            logger.exception(
                "daily_report_service.generation_failed",
                error=e,
            )
            return ReportResult(success=False, error=str(e))

    def _collect_snapshots(self, report: DailyAutonomousReport, date: datetime) -> None:  # noqa: C901, PLR0912, PLR0915
        """Collect snapshot data from external modules (fail-open per source)."""
        from .models import (
            ChaosReportSummary,
            ErrorBudgetGateSummary,
            LoadSheddingSummary,
        )

        # XP3: Chaos resilience summary
        try:
            from baldur.factory.registry import ProviderRegistry

            generator = ProviderRegistry.report_generator.safe_get()
            if generator is None:
                raise RuntimeError("baldur_pro ReportGenerator not registered")
            date_str = date.strftime("%Y-%m-%d")
            chaos_report = generator.get_report_by_date(date_str)
            if chaos_report:
                report.chaos_summary = ChaosReportSummary(
                    grade=chaos_report.grade,
                    grade_trend=chaos_report.grade_trend,
                    experiments_total=chaos_report.total_experiments,
                    experiments_passed=chaos_report.passed_experiments,
                    experiments_failed=chaos_report.failed_experiments,
                    sla_breaches=chaos_report.total_sla_breaches,
                    error_budget_consumed_pct=chaos_report.error_budget_consumed_percent,
                )
        except Exception:
            pass

        # XP5: Load shedding stats (Redis-first, in-memory fallback)
        try:
            dropped_total = processed_total = 0
            dropped_by_tier: dict[str, int] = {}
            processed_by_tier: dict[str, int] = {}
            level = ""
            source = None

            # Try Redis first (multi-process aggregated data)
            try:
                from baldur.factory import ProviderRegistry

                cache = ProviderRegistry.get_cache()
                date_key = date.strftime("%Y-%m-%d")
                prefix = f"baldur:rate_controller:{date_key}"
                redis_dropped = cache.get(f"{prefix}:dropped")
                redis_processed = cache.get(f"{prefix}:processed")
                if redis_dropped is not None or redis_processed is not None:
                    dropped_total = int(redis_dropped or 0)
                    processed_total = int(redis_processed or 0)
                    for tier in ("critical", "standard", "non_essential"):
                        val = cache.get(f"{prefix}:dropped:{tier}")
                        if val is not None:
                            dropped_by_tier[tier] = int(val)
                    source = "redis"
            except Exception:
                pass  # Redis unavailable -> fall through to in-memory

            # Fallback: in-memory counters (single-process only)
            if source is None:
                from baldur.scaling.rate_controller import get_rate_controller

                state = get_rate_controller().get_state()
                # False Zero guard: processed+dropped==0 means either fresh instance
                # (report worker, no traffic) or genuinely idle. Both -> skip section.
                if state.processed_count > 0 or state.dropped_count > 0:
                    dropped_total = state.dropped_count
                    dropped_by_tier = dict(state.dropped_by_tier or {})
                    processed_total = state.processed_count
                    processed_by_tier = dict(state.processed_by_tier or {})
                    level = (
                        state.level.value
                        if hasattr(state.level, "value")
                        else str(state.level)
                    )
                    source = "memory"

            if source is not None:
                report.load_shedding_summary = LoadSheddingSummary(
                    dropped_total=dropped_total,
                    dropped_by_tier=dropped_by_tier,
                    processed_total=processed_total,
                    processed_by_tier=processed_by_tier,
                    level=level,
                )
        except Exception:
            pass

        # D8 supplement: dlq_pending_count (gauge -> snapshot, not event-driven)
        # Note: update_dlq_pending_gauges() also sets Prometheus gauge as side-effect.
        # Gauge.set() is idempotent — no metric spike at report time.
        try:
            from baldur.services.metrics.updaters import update_dlq_pending_gauges

            pending = update_dlq_pending_gauges()
            report.dlq_pending_count = sum(pending.values())
        except Exception:
            pass

        # D9: DLQ pending breakdown — per-domain + per-failure_type with
        # actionable guidance. Fail-open: breakdown omitted on error,
        # dlq_pending_count (O(1) ZCARD) preserved.
        self._collect_dlq_pending_breakdown(report)

        # UU-E5: Error budget gate summary (from accumulated entries)
        _EB_BLOCKED = "error_budget_gate_blocked"
        _EB_WARNING = "error_budget_gate_warning"
        blocks = sum(1 for e in report.entries if e.task_name == _EB_BLOCKED)
        warnings = sum(1 for e in report.entries if e.task_name == _EB_WARNING)
        if blocks > 0 or warnings > 0:
            report.error_budget_summary = ErrorBudgetGateSummary(
                blocks=blocks, warnings=warnings
            )

        # Purge aggregated EB entries — summary already extracted
        eb_names = {_EB_BLOCKED, _EB_WARNING}
        report.entries = [e for e in report.entries if e.task_name not in eb_names]

        # D5: Entitlement re-check (Celery deployments re-validate during daily_report)
        try:
            from baldur.core.entitlement import get_entitlement_status

            get_entitlement_status(force=True)
        except Exception:
            pass

        # Phase 3 (D7): Automated Actions summary from PRO entries
        self._collect_automated_actions_section(report)

        # 427 §4.6: Shadow PRO insights from OSS observables
        self._collect_shadow_pro_section(report)

    def _collect_dlq_pending_breakdown(self, report: DailyAutonomousReport) -> None:
        """Populate DLQPendingBreakdown from FailedOperationRepository.

        D9: Calls get_statistics() and reads new `pending_by_domain` and
        `pending_by_domain_and_failure_type` fields. Maps failure_type to
        recommended_action via _RECOMMENDED_ACTIONS (copy of dlq_recorder
        dict). Fail-open on repository unavailability.
        """
        from .models import (
            _RECOMMENDED_ACTIONS,
            DLQFailureTypeBreakdown,
            DLQPendingBreakdown,
        )

        try:
            from baldur.factory import ProviderRegistry

            repo = ProviderRegistry.get_failed_operation_repo()
            stats = repo.get_statistics()

            pending_by_domain = stats.get("pending_by_domain", {})
            pending_by_domain_and_ft = stats.get(
                "pending_by_domain_and_failure_type", {}
            )

            if not pending_by_domain and not pending_by_domain_and_ft:
                return

            # Normalize blank-domain entries to "unknown" consistently across
            # both by_domain and by_failure_type.domains — raw adapter output
            # may have "" for entries missing a domain label.
            def _norm(d: str) -> str:
                return d or "unknown"

            # Collapse per-domain-per-failure-type into per-failure-type map
            # so Slack formatter can show aggregate lines like:
            #   "payment: 2 AUTH_ERROR — Check credentials and permissions"
            by_failure_type: dict[str, DLQFailureTypeBreakdown] = {}
            for domain, ft_map in pending_by_domain_and_ft.items():
                normalized_domain = _norm(domain)
                for failure_type, count in ft_map.items():
                    if failure_type not in by_failure_type:
                        by_failure_type[failure_type] = DLQFailureTypeBreakdown(
                            count=0,
                            domains=[],
                            action=_RECOMMENDED_ACTIONS.get(
                                failure_type, "Review and retry manually"
                            ),
                        )
                    bd = by_failure_type[failure_type]
                    bd.count += count
                    if normalized_domain not in bd.domains:
                        bd.domains.append(normalized_domain)

            normalized_by_domain: dict[str, int] = {}
            for domain, count in pending_by_domain.items():
                key = _norm(domain)
                normalized_by_domain[key] = normalized_by_domain.get(key, 0) + count

            total = sum(normalized_by_domain.values())
            report.dlq_pending_breakdown = DLQPendingBreakdown(
                total=total,
                by_domain=normalized_by_domain,
                by_failure_type=by_failure_type,
            )
        except Exception as e:
            logger.warning(
                "daily_report_service.dlq_pending_breakdown_failed",
                error=e,
            )

    def _collect_automated_actions_section(self, report: DailyAutonomousReport) -> None:  # noqa: C901
        """Aggregate automated actions from PRO service entries (Phase 3, D7).

        Entries with specific task_names produced by PRO services are counted
        into a typed AutomatedActionsSummary. Entries are NOT removed — they
        remain available for the detail API view (per-event context).
        """
        from .models import AutomatedActionsSummary

        summary = AutomatedActionsSummary()
        has_any = False

        for entry in report.entries:
            name = entry.task_name
            r = entry.result or {}

            if name == "auto_replay_batch":
                summary.auto_replay_batches += 1
                summary.auto_replay_recovered += int(r.get("recovered_count", 0))
                summary.auto_replay_failed += int(r.get("failed_count", 0))
                has_any = True
            elif name == "canary_rollout_completed":
                summary.canary_completed += 1
                has_any = True
            elif name == "canary_rollback_triggered":
                summary.canary_rolled_back += 1
                has_any = True
            elif name == "auto_tuning_applied":
                summary.auto_tuning_applied += 1
                has_any = True
            elif name == "emergency_level_changed":
                summary.emergency_level_changes += 1
                has_any = True
            elif name == "saga_completed":
                summary.saga_completed += 1
                has_any = True
            elif name == "saga_compensated":
                summary.saga_compensated += 1
                has_any = True
            elif name == "governance_policy_blocked":
                summary.governance_blocked += 1
                has_any = True

        if has_any:
            report.automated_actions_summary = summary

    def _collect_shadow_pro_section(self, report: DailyAutonomousReport) -> None:
        """Derive shadow PRO insights from existing OSS report fields.

        Visibility policy (impl 452): paying PRO customers are auto-suppressed,
        OSS users follow the operator-configured cadence (`shadow_pro_mode`):
        auto = daily for the first 30 days then weekly anniversary,
        daily = always, weekly = anniversary only, off = never.
        """

        from baldur.settings.daily_report import get_daily_report_settings
        from baldur.utils.time import utc_now

        from .models import ShadowProSummary

        mode = get_daily_report_settings().shadow_pro_mode
        if mode == "off":
            return

        # PRO customers should never see the upsell. ImportError is the OSS
        # baseline (PRO module absent), so it stays silent. Validator runtime
        # failures are logged so operators can diagnose mis-suppression.
        try:
            from baldur.core.entitlement import get_entitlement_status
        except ImportError:
            pass
        else:
            try:
                if get_entitlement_status().is_active:
                    return
            except Exception as e:
                logger.warning(
                    "daily_report_service.entitlement_check_failed",
                    error=str(e),
                )

        today = utc_now().date()
        if not self._shadow_pro_should_render(mode, today):
            return

        cb_trips = report.circuit_transitions
        failed_ops = report.task_failures
        drift_warnings = report.drift_warnings_count

        if cb_trips > 0 or failed_ops > 0 or drift_warnings > 0:
            report.shadow_pro_summary = ShadowProSummary(
                cb_trips_without_auto_degradation=cb_trips,
                failed_ops_without_dlq=failed_ops,
                drift_warnings_manual_only=drift_warnings,
            )

    def _shadow_pro_should_render(self, mode: str, today: date) -> bool:
        """Apply cadence policy: auto/daily/weekly. 'off' is filtered earlier."""
        if mode == "daily":
            return True

        install_date = self._get_or_set_install_marker(today)
        days_since_install = (today - install_date).days

        if mode == "weekly":
            return days_since_install % 7 == 0

        # mode == "auto": daily during grace, then weekly anniversary
        if days_since_install < _SHADOW_PRO_GRACE_DAYS:
            return True
        return days_since_install % 7 == 0

    def _get_or_set_install_marker(self, today: date) -> date:
        """Read the persisted install date or write today's date as a fresh marker.

        Fail-open per C3: backend failure is treated as 'no marker present'
        and today is returned (grace day 1) — matches the daily_report module's
        existing fail-open posture for `state_backend` outages.
        """
        from baldur.core.state_backend import get_state_backend

        try:
            backend = get_state_backend()
            stored = backend.get(_INSTALL_MARKER_KEY)
            if isinstance(stored, dict):
                raw = stored.get("first_seen")
                if isinstance(raw, str):
                    try:
                        return date.fromisoformat(raw)
                    except (TypeError, ValueError):
                        pass  # corrupt marker -> overwrite below
            backend.set(_INSTALL_MARKER_KEY, {"first_seen": today.isoformat()})
        except Exception as e:
            logger.warning(
                "daily_report_service.install_marker_io_failed",
                error=str(e),
            )
        return today

    def _determine_severity(self, report: DailyAutonomousReport) -> str:
        """Determine notification severity from report state."""
        if report.critical_alerts > 0:
            return "critical"
        if report.task_failures > 0:
            return "warning"
        return "info"

    def _has_actionable_items(self, report: DailyAutonomousReport) -> bool:
        """Check if report contains items requiring PagerDuty alert.

        Deferred-tier signals (error-budget / chaos / load-shedding) are gated
        by ``_is_shipped`` so a Deferred-only day skips PagerDuty entirely,
        keeping the page free of empty Deferred-feature summaries (D5).
        """
        if report.critical_alerts > 0:
            return True
        if report.task_failures >= 5:
            return True
        if (
            _is_shipped("error_budget")
            and report.error_budget_summary
            and report.error_budget_summary.blocks > 0
        ):
            return True
        if (
            _is_shipped("chaos")
            and report.chaos_summary
            and report.chaos_summary.grade in ("D", "F")
        ):
            return True
        return bool(
            _is_shipped("load_shedding")
            and report.load_shedding_summary
            and report.load_shedding_summary.level in ("high", "critical")
        )

    def _has_critical_items(self, report: DailyAutonomousReport) -> bool:
        """Subset of actionable items requiring PagerDuty critical severity."""
        if report.critical_alerts > 0:
            return True
        return bool(
            _is_shipped("error_budget")
            and report.error_budget_summary
            and report.error_budget_summary.blocks > 0
        )

    # =========================================================================
    # Persistence (D1/D4 — per-date keys + idempotent legacy migration)
    # =========================================================================

    def _persist_report(self, report: DailyAutonomousReport, date: datetime) -> None:
        """Persist report under a per-date state backend key (D1/D4).

        Layout:
        - Per-date key ``baldur:daily_reports:{YYYY-MM-DD}`` holds one day's
          report (``to_dict(include_entries=True)``). Single-key writes
          eliminate the multi-MB umbrella RMW.
        - ``ttl_seconds = (keep_reports_days + 7) * 86400`` lets Redis
          auto-expire stale dates; File/Memory backends ignore TTL and rely
          on the explicit sweep below.

        Steps:
        1. Idempotent one-shot migration of the legacy umbrella, if present.
        2. Write this date's report.
        3. Sweep a bounded window of dates beyond the retention cutoff so
           File/Memory backends don't accumulate arrears.

        Fail-open: any exception is swallowed so persistence failure never
        fails report delivery.
        """
        try:
            from datetime import timedelta

            from baldur.core.state_backend import get_state_backend
            from baldur.settings.daily_report import get_daily_report_settings
            from baldur.utils.time import utc_now

            settings = get_daily_report_settings()
            keep_days = settings.keep_reports_days
            ttl_seconds = (keep_days + 7) * 86400
            backend = get_state_backend()

            today = utc_now()
            cutoff_date_str = (today - timedelta(days=keep_days)).strftime("%Y-%m-%d")

            self._migrate_legacy_umbrella_if_present(
                backend, ttl_seconds, cutoff_date_str
            )

            date_str = date.strftime("%Y-%m-%d")
            backend.set(
                f"{_STATE_BACKEND_KEY_PREFIX}{date_str}",
                report.to_dict(include_entries=True),
                ttl_seconds=ttl_seconds,
            )

            self._prune_stale_date_keys(backend, today, keep_days)

            logger.debug(
                "daily_report_service.report_persisted",
                date=date_str,
                total_dates=1,
            )
        except Exception as e:
            logger.warning(
                "daily_report_service.persist_failed",
                error=e,
            )

    def _migrate_legacy_umbrella_if_present(
        self, backend: Any, ttl_seconds: int, cutoff_date_str: str
    ) -> None:
        """Migrate the legacy umbrella dict to per-date keys (idempotent).

        Each date inside the umbrella is written under its per-date key with
        the current TTL, then the umbrella key itself is deleted. Per-date
        writes are idempotent — re-running after a partial crash completes
        the migration safely.

        Dates at or before ``cutoff_date_str`` (YYYY-MM-DD) are skipped so
        stale entries do not resurface indefinitely on File/Memory backends
        that ignore TTL. The ``<=`` boundary matches ``_prune_stale_date_keys``
        which sweeps starting at ``today - keep_days`` (= cutoff date).
        """
        legacy = backend.get(_LEGACY_UMBRELLA_KEY)
        if not legacy:
            return

        migrated = 0
        skipped = 0
        for date_str, report_dict in legacy.items():
            if date_str <= cutoff_date_str:
                skipped += 1
                continue
            backend.set(
                f"{_STATE_BACKEND_KEY_PREFIX}{date_str}",
                report_dict,
                ttl_seconds=ttl_seconds,
            )
            migrated += 1

        backend.delete(_LEGACY_UMBRELLA_KEY)

        logger.info(
            "daily_report_service.umbrella_migrated",
            dates_migrated=migrated,
            dates_skipped_stale=skipped,
        )

    def _prune_stale_date_keys(
        self, backend: Any, today: datetime, keep_days: int
    ) -> None:
        """Explicit sweep for dates beyond the retention cutoff.

        Iterates ``[keep_days, keep_days + _PRUNE_SWEEP_BUFFER_DAYS)`` days
        back from ``today`` and calls ``backend.delete()`` on each per-date
        key. Delete is idempotent (no-op if absent), so this is safe across
        backends that do and don't honor TTL.
        """
        from datetime import timedelta

        for i in range(keep_days, keep_days + _PRUNE_SWEEP_BUFFER_DAYS):
            stale_date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            backend.delete(f"{_STATE_BACKEND_KEY_PREFIX}{stale_date}")

    def _send_to_channel(self, report: DailyAutonomousReport, channel: str) -> None:
        """Route report to the appropriate channel with format and severity."""
        from baldur.services.security_notification.models import (
            PagerDutySeverity,
        )

        severity = self._determine_severity(report)

        if channel == "slack":
            message = format_report_for_slack(report)
        elif channel == "pagerduty":
            if not self._has_actionable_items(report):
                return  # Nothing actionable -> skip PagerDuty
            from .formatters import format_report_for_pagerduty

            message = format_report_for_pagerduty(report)
            severity = (
                PagerDutySeverity.CRITICAL.value
                if self._has_critical_items(report)
                else PagerDutySeverity.ERROR.value
            )
        else:
            logger.warning(
                "daily_report_service.unknown_channel_skipped",
                channel=channel,
            )
            return

        self._send_report(report, message, severity, channel=channel)

    def _send_report(
        self,
        report: DailyAutonomousReport,
        message: str,
        severity: str,
        channel: str = "slack",
    ) -> None:
        """Send report via unified notification manager."""
        try:
            notify(
                title=f"[Baldur] Daily Report ({report.date:%Y-%m-%d})",
                message=message,
                priority=severity,
                category="report",
                source="daily_report",
                metadata=report.to_dict(),
                channels=[channel],
            )
        except Exception as e:
            logger.exception(
                "daily_report_service.send_failed",
                error=e,
            )
            raise


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_daily_report_service, configure_daily_report_service, reset_daily_report_service = (
    make_singleton_factory("daily_report_service", DailyReportService)
)

__all__ = [
    "ReportResult",
    "DailyReportService",
    "get_daily_report_service",
    "configure_daily_report_service",
    "reset_daily_report_service",
]
