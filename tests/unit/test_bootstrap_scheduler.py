"""Unit tests for bootstrap scheduler wiring (429 Part 6 / D6).

Scope:
- _start_default_scheduler AUTOSTART=0 early-return.
- Default job list registers seven jobs (archive_old_dlq_entries, cb_recovery,
  cb_override_expiry, cleanup_expired_config, daily_report, sla_drift,
  config_apply) once every registration filter passes — installed-tier
  presence, then the entitlement verdict, then the operator disable list.
- Unknown task_backend falls back to "inline" with WARNING log.
- arq backend explicitly logs "not_implemented" and falls back to inline.
- _build_celery_delegator returns None when Celery is missing.
- _wrap_with_context preserves contextvars across invocation.

Does NOT test LeaderScheduler internals — those have their own unit tests.
"""

from __future__ import annotations

import contextvars
from unittest.mock import MagicMock, patch

import pytest

from baldur.bootstrap import (
    _CELERY_TASK_NAMES,
    _DEFAULT_SCHEDULED_JOBS,
    _ENTITLEMENT_GATED_JOBS,
    _PRO_GATED_JOBS,
    _build_celery_delegator,
    _build_config_apply_callable,
    _entitlement_resolved_scheduled_jobs,
    _read_scheduler_settings,
    _resolve_job_callable,
    _resolve_scheduler_elector,
    _settings_resolved_scheduled_jobs,
    _start_default_scheduler,
    _tier_resolved_scheduled_jobs,
    _wrap_with_context,
)
from baldur.coordination.local_file_elector import LocalFileLeaderElector
from baldur.coordination.scheduler import LeaderScheduler
from baldur.core.entitlement import EntitlementResult, EntitlementStatus
from baldur.settings.scheduler import SchedulerSettings, reset_scheduler_settings


@pytest.fixture(autouse=True)
def _reset_scheduler_cache():
    """Clear the LeaderScheduler singleton cache between tests."""
    from baldur.coordination.scheduler import reset_schedulers

    reset_schedulers()
    yield
    reset_schedulers()


# =============================================================================
# Contract — default scheduled jobs list
# =============================================================================


class TestDefaultScheduledJobsContract:
    """Contract: the default jobs (429 D6 + 665 D2 config_apply) are registered
    with exactly these names and intervals."""

    def test_default_jobs_contract(self):
        """Exactly ten jobs, keyed by name, with known intervals."""
        by_name = {
            name: interval for name, _mod, _attr, interval in _DEFAULT_SCHEDULED_JOBS
        }

        assert set(by_name) == {
            "daily_report",
            "sla_drift",
            "cb_recovery",
            "cb_override_expiry",
            "archive_old_dlq_entries",
            "cleanup_expired_config",
            "config_apply",
            "scan_zombie_rollouts",
            "auto_promote_eligible",
            "collect_canary_metrics",
        }
        # Daily cadence — 24h in seconds
        assert by_name["daily_report"] == 24 * 60 * 60.0
        assert by_name["archive_old_dlq_entries"] == 24 * 60 * 60.0
        # Hourly cadence
        assert by_name["sla_drift"] == 60 * 60.0
        assert by_name["cleanup_expired_config"] == 60 * 60.0
        # Per-minute
        assert by_name["cb_recovery"] == 60.0
        # 741 D2 — same cost class and cadence as cb_recovery, so the console's
        # "lifts after N minutes" promise stays accurate to within a minute.
        assert by_name["cb_override_expiry"] == 60.0
        # 665 D2 — config apply every 30s
        assert by_name["config_apply"] == 30.0
        # Canary watchdog twin lane — cadences match the Celery beat entries.
        assert by_name["scan_zombie_rollouts"] == 300.0
        assert by_name["auto_promote_eligible"] == 60.0
        assert by_name["collect_canary_metrics"] == 120.0

    def test_override_expiry_job_is_not_pro_gated(self):
        """It ships on every install — Celery-less deployments are the point."""
        assert "cb_override_expiry" not in _PRO_GATED_JOBS
        assert "cb_override_expiry" in {
            job[0] for job in _tier_resolved_scheduled_jobs()
        }

    def test_override_expiry_job_delegates_to_the_celery_task_when_present(self):
        """With celery as the backend the existing beat task owns the sweep."""
        assert (
            _CELERY_TASK_NAMES["cb_override_expiry"]
            == "baldur.celery_tasks.expire_manual_overrides"
        )


# =============================================================================
# Behavior — tier-resolved job registration
# =============================================================================


class TestTierResolvedScheduledJobsBehavior:
    """_tier_resolved_scheduled_jobs filters the contract tuple at registration.

    The PRO-gated jobs' capability is PRO-only, so on an install without the
    PRO distribution they could only fail on cadence. The filter narrows what
    gets registered; ``_DEFAULT_SCHEDULED_JOBS`` itself stays the full contract.
    """

    def test_pro_tier_returns_the_contract_tuple_unchanged(self, mock_pro_tier):
        """With PRO installed nothing is filtered — the same tuple comes back."""
        assert _tier_resolved_scheduled_jobs() == _DEFAULT_SCHEDULED_JOBS

    def test_oss_tier_removes_exactly_the_pro_gated_jobs(self, mock_oss_tier):
        """The set difference vs the contract tuple is exactly _PRO_GATED_JOBS.

        Computed from the source constants rather than hardcoded, so adding a
        job to either tuple keeps this honest instead of silently stale.
        """
        resolved_names = {job[0] for job in _tier_resolved_scheduled_jobs()}
        contract_names = {job[0] for job in _DEFAULT_SCHEDULED_JOBS}

        assert contract_names - resolved_names == set(_PRO_GATED_JOBS)

    def test_oss_tier_preserves_the_order_of_surviving_jobs(self, mock_oss_tier):
        """Filtering is order-preserving — registration order is not reshuffled."""
        resolved = _tier_resolved_scheduled_jobs()
        expected = tuple(
            job for job in _DEFAULT_SCHEDULED_JOBS if job[0] not in _PRO_GATED_JOBS
        )

        assert resolved == expected

    def test_contract_tuple_is_not_mutated_by_the_filter(self, mock_oss_tier):
        """The filter builds a new tuple; the module-level contract is untouched."""
        before = tuple(_DEFAULT_SCHEDULED_JOBS)

        _tier_resolved_scheduled_jobs()

        assert _DEFAULT_SCHEDULED_JOBS == before
        assert "archive_old_dlq_entries" in {job[0] for job in _DEFAULT_SCHEDULED_JOBS}

    def test_oss_tier_logs_each_gated_job_skip(self, mock_oss_tier):
        """Each filtered job leaves a DEBUG breadcrumb naming it."""
        import baldur.bootstrap as bootstrap_module

        with patch.object(bootstrap_module, "logger") as mock_logger:
            _tier_resolved_scheduled_jobs()

        logged_jobs = {
            call.kwargs["job"]
            for call in mock_logger.debug.call_args_list
            if call.args and call.args[0] == "scheduler.pro_gated_job_skipped"
        }
        assert logged_jobs == set(_PRO_GATED_JOBS)


# =============================================================================
# Behavior — entitlement-resolved job registration (759 D3)
# =============================================================================


class TestEntitlementResolvedScheduledJobsBehavior:
    """_entitlement_resolved_scheduled_jobs drops gated jobs without an ACTIVE verdict.

    A second filter composed after the tier-presence one, not a branch inside it:
    presence and licence are different questions with different failure modes.
    Applying pending runtime-config changes is a licensed capability, so an
    import probe standing in for the verdict lets an unentitled process import
    PRO code on a cadence and stand ready to apply PRO config changes.

    Entitlement is non-ACTIVE by default in the test process, so every arm that
    wants the job kept drives the verdict explicitly.
    """

    @pytest.mark.parametrize(
        ("status", "keeps_gated_jobs"),
        [
            (EntitlementStatus.ACTIVE, True),
            (EntitlementStatus.MISSING, False),
            (EntitlementStatus.INVALID, False),
        ],
        ids=["active", "missing", "invalid"],
    )
    def test_verdict_decides_whether_the_gated_jobs_survive(
        self, status, keeps_gated_jobs
    ):
        """ACTIVE keeps the tuple whole; every other verdict drops exactly the
        gated names, order otherwise preserved."""
        with patch(
            "baldur.core.entitlement.get_entitlement_status",
            return_value=EntitlementResult(status=status),
        ):
            resolved = _entitlement_resolved_scheduled_jobs(_DEFAULT_SCHEDULED_JOBS)

        if keeps_gated_jobs:
            assert resolved == _DEFAULT_SCHEDULED_JOBS
        else:
            assert resolved == tuple(
                job
                for job in _DEFAULT_SCHEDULED_JOBS
                if job[0] not in _ENTITLEMENT_GATED_JOBS
            )

    def test_unreadable_verdict_drops_the_gated_jobs(self):
        """Indeterminate reads as not entitled — the same direction the change
        *creation* surface fails in, so the applier is never the only half of
        the feature left live."""
        with patch(
            "baldur.core.entitlement.get_entitlement_status",
            side_effect=RuntimeError("licence store down"),
        ):
            resolved = _entitlement_resolved_scheduled_jobs(_DEFAULT_SCHEDULED_JOBS)

        assert {job[0] for job in resolved} & set(_ENTITLEMENT_GATED_JOBS) == set()

    def test_job_list_without_a_gated_name_never_reads_the_verdict(self):
        """The short-circuit is load-bearing, so it is asserted directly.

        No OSS-only boot calls get_entitlement_status() today; an unconditional
        call would add a settings construction, a licence-file read, an INFO
        line and two gauge writes to every boot of the tier this gate cannot
        help. Without this assertion the short-circuit is invisible to tests.
        """
        ungated = tuple(
            job
            for job in _DEFAULT_SCHEDULED_JOBS
            if job[0] not in _ENTITLEMENT_GATED_JOBS
        )

        with patch("baldur.core.entitlement.get_entitlement_status") as mock_verdict:
            resolved = _entitlement_resolved_scheduled_jobs(ungated)

        assert resolved is ungated
        mock_verdict.assert_not_called()

    def test_verdict_is_resolved_through_the_module_attribute_seam(
        self, monkeypatch, mock_pro_tier
    ):
        """Patching baldur.core.entitlement's own attribute must reach the gate.

        Both gates import the producer inside their function body precisely so a
        replaced module attribute is seen — the tests and the scenario testbed
        force a verdict that way. If a refactor hoists the import to module
        level, bootstrap binds the real producer at import time and every
        ACTIVE-arm test in this file starts passing vacuously against the
        non-ACTIVE default. The hoist is asserted against directly, and the
        registration arm is what the seam buys.
        """
        import baldur.bootstrap as bootstrap_module

        assert not hasattr(bootstrap_module, "get_entitlement_status")

        monkeypatch.setenv("BALDUR_SCHEDULER_AUTOSTART", "1")
        reset_scheduler_settings()
        mock_sched = MagicMock(spec=LeaderScheduler)

        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=EntitlementResult(status=EntitlementStatus.ACTIVE),
            ),
            patch(
                "baldur.coordination.scheduler.get_leader_scheduler",
                return_value=mock_sched,
            ),
        ):
            _start_default_scheduler(task_backend="inline")

        registered = {
            call.kwargs.get("name") or call.args[0]
            for call in mock_sched.add_job.call_args_list
        }
        assert set(_ENTITLEMENT_GATED_JOBS) <= registered


# =============================================================================
# Behavior — operator disable list (759 D4)
# =============================================================================


class TestDisabledJobsFilterBehavior:
    """_settings_resolved_scheduled_jobs applies BALDUR_SCHEDULER_DISABLED_JOBS."""

    @staticmethod
    def _events(mock_logger, level: str, event: str) -> set[str]:
        """Job names reported under ``event`` at ``level``."""
        return {
            call.kwargs["job"]
            for call in getattr(mock_logger, level).call_args_list
            if call.args and call.args[0] == event
        }

    def test_named_job_is_dropped_and_reported(self):
        """The named job leaves the registered set and an INFO breadcrumb."""
        import baldur.bootstrap as bootstrap_module

        with patch.object(bootstrap_module, "logger") as mock_logger:
            resolved = _settings_resolved_scheduled_jobs(
                _DEFAULT_SCHEDULED_JOBS, ("config_apply",)
            )

        assert "config_apply" not in {job[0] for job in resolved}
        assert resolved == tuple(
            job for job in _DEFAULT_SCHEDULED_JOBS if job[0] != "config_apply"
        )
        assert self._events(
            mock_logger, "info", "scheduler.job_disabled_by_settings"
        ) == {"config_apply"}

    def test_empty_disable_list_returns_the_incoming_tuple(self):
        """The default costs nothing — same object back, no scan, no logging."""
        import baldur.bootstrap as bootstrap_module

        with patch.object(bootstrap_module, "logger") as mock_logger:
            resolved = _settings_resolved_scheduled_jobs(_DEFAULT_SCHEDULED_JOBS, ())

        assert resolved is _DEFAULT_SCHEDULED_JOBS
        mock_logger.info.assert_not_called()
        mock_logger.warning.assert_not_called()

    def test_name_matching_no_default_job_is_reported_unknown(self):
        """A misspelt name warns and otherwise changes nothing."""
        import baldur.bootstrap as bootstrap_module

        with patch.object(bootstrap_module, "logger") as mock_logger:
            resolved = _settings_resolved_scheduled_jobs(
                _DEFAULT_SCHEDULED_JOBS, ("config_aply",)
            )

        assert resolved == _DEFAULT_SCHEDULED_JOBS
        assert self._events(
            mock_logger, "warning", "scheduler.unknown_disabled_job"
        ) == {"config_aply"}

    def test_already_tier_filtered_job_name_is_not_reported_unknown(self):
        """An OSS operator naming a PRO-gated job wrote correct config.

        Unknown names are judged against the full default-job contract, never
        against the already-filtered list — otherwise the operator is told their
        correct configuration names a job that does not exist.
        """
        import baldur.bootstrap as bootstrap_module

        oss_jobs = tuple(
            job for job in _DEFAULT_SCHEDULED_JOBS if job[0] not in _PRO_GATED_JOBS
        )
        assert "config_apply" not in {job[0] for job in oss_jobs}

        with patch.object(bootstrap_module, "logger") as mock_logger:
            resolved = _settings_resolved_scheduled_jobs(oss_jobs, ("config_apply",))

        assert resolved == oss_jobs
        assert (
            self._events(mock_logger, "warning", "scheduler.unknown_disabled_job")
            == set()
        )


# =============================================================================
# Behavior — scheduler settings read fallback (759 D4)
# =============================================================================


class TestSchedulerSettingsReadFallbackBehavior:
    """_read_scheduler_settings never lets a settings fault stop the jobs."""

    def test_settings_values_are_forwarded_verbatim(self):
        """Both knobs come back as the settings object reports them.

        Driven by a real SchedulerSettings rather than a double, so the parse
        the reader depends on is the production one.
        """
        settings = SchedulerSettings(autostart=False, disabled_jobs="sla_drift")

        with patch(
            "baldur.settings.scheduler.get_scheduler_settings",
            return_value=settings,
        ):
            assert _read_scheduler_settings() == (False, ("sla_drift",))

    def test_settings_producer_failure_falls_back_to_running_everything(self):
        """Fail-safe: a broken settings machinery must not silently stop the jobs."""
        import baldur.bootstrap as bootstrap_module

        with (
            patch(
                "baldur.settings.scheduler.get_scheduler_settings",
                side_effect=RuntimeError("settings down"),
            ),
            patch.object(bootstrap_module, "logger") as mock_logger,
        ):
            result = _read_scheduler_settings()

        assert result == (True, ())
        assert any(
            call.args and call.args[0] == "scheduler.settings_unavailable"
            for call in mock_logger.warning.call_args_list
        )

    def test_second_field_read_cannot_re_raise_past_the_fallback(self):
        """Both fields are read inside the one try.

        The settings object is lazily constructed, so a read that succeeds for
        autostart and then fails for the job list would escape a fallback that
        wrapped only the first read.
        """

        class _FailsOnTheJobList:
            autostart = False

            def get_disabled_job_names(self):
                raise RuntimeError("late fault")

        with patch(
            "baldur.settings.scheduler.get_scheduler_settings",
            return_value=_FailsOnTheJobList(),
        ):
            assert _read_scheduler_settings() == (True, ())


# =============================================================================
# Behavior — AUTOSTART env gate and unknown backend fallback
# =============================================================================


class TestStartDefaultSchedulerBehavior:
    """Behavior tests for _start_default_scheduler branching logic."""

    @staticmethod
    def _registered_job_names(mock_scheduler) -> set[str]:
        """Job names the scheduler was actually asked to register."""
        return {
            call.kwargs.get("name") or call.args[0]
            for call in mock_scheduler.add_job.call_args_list
        }

    def test_gate_only_removes_the_pro_job_from_the_registered_set(self, monkeypatch):
        """OSS registers the PRO set minus exactly the gated jobs.

        Guards the blast radius: a gate that also dropped an unrelated job — or
        that dropped nothing — leaves this equality failing. Both tiers are
        driven in one test so the two registered sets are directly comparable.

        The entitlement verdict is driven ACTIVE for both arms because presence
        is no longer the only registration gate: the test process carries no
        licence token, so otherwise the entitlement-gated job would be absent
        from the PRO arm as well and the difference would understate the
        presence gate it is measuring.
        """
        monkeypatch.setenv("BALDUR_SCHEDULER_AUTOSTART", "1")

        def registered_under(pro_installed: bool) -> set[str]:
            from baldur.coordination.scheduler import reset_schedulers

            reset_schedulers()
            mock_sched = MagicMock(spec=LeaderScheduler)
            with (
                patch(
                    "baldur.utils.tier.is_pro_installed",
                    return_value=pro_installed,
                ),
                patch(
                    "baldur.core.entitlement.get_entitlement_status",
                    return_value=EntitlementResult(status=EntitlementStatus.ACTIVE),
                ),
                patch(
                    "baldur.coordination.scheduler.get_leader_scheduler",
                    return_value=mock_sched,
                ),
            ):
                _start_default_scheduler(task_backend="inline")
            return self._registered_job_names(mock_sched)

        pro_names = registered_under(True)
        oss_names = registered_under(False)

        assert pro_names - oss_names == set(_PRO_GATED_JOBS)
        assert oss_names - pro_names == set()

    def test_autostart_disabled_never_reaches_the_entitlement_read(self, monkeypatch):
        """Filter order: autostart is answered before any licence read.

        The all-or-nothing off-switch is the escape hatch a test process or a
        worker uses to stay inert; resolving a verdict behind it would put a
        settings construction and a licence-file read back on that path.
        """
        monkeypatch.setenv("BALDUR_SCHEDULER_AUTOSTART", "0")
        reset_scheduler_settings()

        with (
            patch("baldur.core.entitlement.get_entitlement_status") as mock_verdict,
            patch("baldur.coordination.scheduler.get_leader_scheduler") as mock_get,
        ):
            _start_default_scheduler(task_backend="inline")

        mock_verdict.assert_not_called()
        mock_get.assert_not_called()

    def test_oss_only_boot_never_reads_the_entitlement_verdict(
        self, monkeypatch, mock_oss_tier
    ):
        """Filter order: installed-tier presence runs before the verdict.

        On an OSS-only install the presence filter has already removed every
        entitlement-gated job, so the second filter short-circuits and the boot
        gains no licence read at all.
        """
        monkeypatch.setenv("BALDUR_SCHEDULER_AUTOSTART", "1")
        reset_scheduler_settings()
        mock_sched = MagicMock(spec=LeaderScheduler)

        with (
            patch("baldur.core.entitlement.get_entitlement_status") as mock_verdict,
            patch(
                "baldur.coordination.scheduler.get_leader_scheduler",
                return_value=mock_sched,
            ),
        ):
            _start_default_scheduler(task_backend="inline")

        mock_verdict.assert_not_called()
        assert "config_apply" not in self._registered_job_names(mock_sched)

    def test_disabled_jobs_setting_removes_exactly_the_named_job(
        self, monkeypatch, mock_pro_tier
    ):
        """The targeted off-switch drops its job and leaves every other one.

        Run against a control boot rather than against the contract tuple, so a
        job that fails to resolve its callable in this process cannot make the
        blast-radius assertion pass or fail for an unrelated reason.
        """
        monkeypatch.setenv("BALDUR_SCHEDULER_AUTOSTART", "1")

        def registered_with(disabled_jobs: str) -> set[str]:
            from baldur.coordination.scheduler import reset_schedulers

            reset_schedulers()
            monkeypatch.setenv("BALDUR_SCHEDULER_DISABLED_JOBS", disabled_jobs)
            reset_scheduler_settings()
            mock_sched = MagicMock(spec=LeaderScheduler)
            with (
                patch(
                    "baldur.core.entitlement.get_entitlement_status",
                    return_value=EntitlementResult(status=EntitlementStatus.ACTIVE),
                ),
                patch(
                    "baldur.coordination.scheduler.get_leader_scheduler",
                    return_value=mock_sched,
                ),
            ):
                _start_default_scheduler(task_backend="inline")
            return self._registered_job_names(mock_sched)

        control_names = registered_with("")
        disabled_names = registered_with("config_apply")

        assert control_names - disabled_names == {"config_apply"}
        assert disabled_names - control_names == set()

    def test_autostart_env_zero_skips_scheduler_entirely(self, monkeypatch):
        """BALDUR_SCHEDULER_AUTOSTART=0 → no scheduler import or start.

        Patches the real import target ``baldur.coordination.scheduler.
        get_leader_scheduler`` because bootstrap.py imports it locally inside
        the function body; patching the ``bootstrap`` module's own attribute
        space would miss the actual call path and pass trivially.
        """
        monkeypatch.setenv("BALDUR_SCHEDULER_AUTOSTART", "0")

        with patch("baldur.coordination.scheduler.get_leader_scheduler") as mock_get:
            _start_default_scheduler()

        mock_get.assert_not_called()

    def test_unknown_backend_falls_back_to_inline(self, monkeypatch, caplog):
        """Given task_backend='unknown', we fall back to inline with a WARNING."""
        monkeypatch.setenv("BALDUR_SCHEDULER_AUTOSTART", "1")

        mock_sched = MagicMock()
        with (
            patch(
                "baldur.coordination.scheduler.get_leader_scheduler",
                return_value=mock_sched,
            ),
            caplog.at_level("WARNING"),
        ):
            _start_default_scheduler(task_backend="nonsense_backend")

        # Warning emitted about unknown backend
        log_events = [rec.message for rec in caplog.records]
        assert any("unknown_task_backend" in msg for msg in log_events)

    def test_arq_backend_logs_not_implemented_and_uses_inline(
        self, monkeypatch, caplog
    ):
        """arq is reserved; logs 'arq_backend_not_implemented' and uses inline."""
        monkeypatch.setenv("BALDUR_SCHEDULER_AUTOSTART", "1")
        mock_sched = MagicMock()

        with (
            patch(
                "baldur.coordination.scheduler.get_leader_scheduler",
                return_value=mock_sched,
            ),
            caplog.at_level("WARNING"),
        ):
            _start_default_scheduler(task_backend="arq")

        log_events = [rec.message for rec in caplog.records]
        assert any("arq_backend_not_implemented" in msg for msg in log_events)


# =============================================================================
# Behavior — _build_celery_delegator
# =============================================================================


class TestBuildCeleryDelegatorBehavior:
    """_build_celery_delegator should return None for unknown / unshipped jobs."""

    def test_unknown_job_returns_none(self):
        """Jobs not listed in _CELERY_TASK_NAMES get no delegator."""
        assert _build_celery_delegator("not_a_real_job") is None

    def test_known_job_returns_callable_when_celery_installed(self):
        """Known job name produces a zero-arg callable (celery already in deps)."""
        fn = _build_celery_delegator("daily_report")

        assert callable(fn)


# =============================================================================
# Behavior — _resolve_job_callable synthetic branches
# =============================================================================


class TestResolveJobCallableBehavior:
    """_resolve_job_callable routes synthetic names through dedicated builders."""

    def test_synthetic_cb_recovery_returns_callable(self):
        """cb_recovery attr shortcut returns a zero-arg callable."""
        fn = _resolve_job_callable("baldur.services", "_synthetic_cb_recovery_check")

        assert callable(fn)
        assert fn.__name__ == "cb_recovery_tick"

    def test_synthetic_cb_override_expiry_returns_callable(self):
        """cb_override_expiry resolves without importing the Celery task."""
        fn = _resolve_job_callable("baldur.services", "_synthetic_cb_override_expiry")

        assert callable(fn)
        assert fn.__name__ == "cb_override_expiry_tick"

    def test_synthetic_cb_override_expiry_runs_the_service_sweep(self):
        """The synthetic callable is the sweep, not a lookalike.

        Resolving to a callable proves nothing about which method it drives —
        the whole point of the job is that a Celery-less install still clears
        lapsed manual overrides.
        """
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        fn = _resolve_job_callable("baldur.services", "_synthetic_cb_override_expiry")
        cb_service = MagicMock(spec=CircuitBreakerService)
        cb_service.check_and_expire_manual_overrides.return_value = ["payment-api"]

        with patch(
            "baldur.services.get_circuit_breaker_service",
            return_value=cb_service,
        ):
            result = fn()

        cb_service.check_and_expire_manual_overrides.assert_called_once_with()
        assert result == ["payment-api"]

    def test_synthetic_sla_drift_returns_callable(self):
        """sla_drift attr shortcut returns a zero-arg callable."""
        fn = _resolve_job_callable(
            "baldur.tasks.drift_detection", "_synthetic_sla_drift_check"
        )

        assert callable(fn)
        assert fn.__name__ == "sla_drift_tick"

    def test_missing_module_returns_none(self):
        """Nonexistent module path returns None (skipped, not crashing)."""
        fn = _resolve_job_callable("baldur.nonexistent_module", "doesnt_matter")

        assert fn is None

    def test_missing_attribute_returns_none(self):
        """Module exists but attr missing → None."""
        fn = _resolve_job_callable("baldur.bootstrap", "definitely_not_a_function")

        assert fn is None


# =============================================================================
# Behavior — _wrap_with_context preserves contextvars across invocation
# =============================================================================


class TestWrapWithContextBehavior:
    """contextvars.copy_context() must pass through the caller's variables."""

    def test_wrap_propagates_contextvar_value(self):
        """A contextvar bound before wrap is visible inside wrap's invocation."""
        var: contextvars.ContextVar[str] = contextvars.ContextVar(
            "_test_ctx", default="unset"
        )

        captured: list[str] = []

        def target() -> None:
            captured.append(var.get())

        var.set("payload")
        wrapped = _wrap_with_context(target)
        # Mutate the contextvar after wrapping to prove the captured snapshot wins.
        var.set("after_wrap")

        wrapped()

        assert captured == ["payload"]


# =============================================================================
# Behavior — _build_config_apply_callable (665 D2)
# =============================================================================


class TestBuildConfigApplyCallableBehavior:
    """The inline config-apply target is a celery-free service delegator (665 D2)."""

    def test_returns_named_callable(self):
        """The built callable is zero-arg and named 'config_apply_tick'."""
        fn = _build_config_apply_callable()

        assert callable(fn)
        assert fn.__name__ == "config_apply_tick"

    def test_delegates_to_config_apply_service(self):
        """Invoking the callable forwards to ConfigApplyService.apply_pending_changes.

        Single Act: the lazy ``get_config_apply_service`` import inside the
        callable is patched, so the call composes the service and returns its
        result verbatim — no business logic in the bootstrap synthetic.
        """
        fn = _build_config_apply_callable()

        with patch(
            "baldur.services.execution_services.get_config_apply_service"
        ) as mock_get:
            mock_service = MagicMock()
            mock_service.apply_pending_changes.return_value = {
                "status": "success",
                "applied": 2,
            }
            mock_get.return_value = mock_service

            result = fn()

        mock_service.apply_pending_changes.assert_called_once_with()
        assert result == {"status": "success", "applied": 2}

    def test_synthetic_resolution_does_not_import_celery_module(self):
        """Resolving _synthetic_config_apply must NOT import baldur.tasks.config_apply.

        That module hard-imports celery (an optional extra) and is unimportable
        on a celery-less inline install — the exact deployment the synthetic
        serves. The synthetic branch short-circuits before any module import, so
        ``importlib.import_module`` is never reached.
        """
        with patch("baldur.bootstrap.importlib.import_module") as mock_import:
            fn = _resolve_job_callable("baldur.services", "_synthetic_config_apply")

        assert callable(fn)
        mock_import.assert_not_called()


# =============================================================================
# Behavior — inline scheduler elector selection (665 D5)
# =============================================================================


class TestStartDefaultSchedulerElectorBehavior:
    """_start_default_scheduler picks LocalFileLeaderElector when distributed
    leader election is disabled (the single-host default) (665 D5)."""

    @pytest.mark.parametrize(
        ("le_enabled", "expect_local"),
        [(False, True), (True, False)],
    )
    def test_resolve_scheduler_elector_by_le_setting(self, le_enabled, expect_local):
        """LE disabled -> LocalFileLeaderElector; LE enabled -> None (factory elector)."""
        fake_settings = MagicMock()
        fake_settings.enabled = le_enabled

        with patch(
            "baldur.coordination.config.get_leader_election_settings",
            return_value=fake_settings,
        ):
            elector = _resolve_scheduler_elector("scheduler")

        if expect_local:
            assert isinstance(elector, LocalFileLeaderElector)
        else:
            assert elector is None

    def test_resolve_scheduler_elector_falls_back_to_local_on_settings_error(self):
        """A settings failure resolves the local elector (never crashes init)."""
        with patch(
            "baldur.coordination.config.get_leader_election_settings",
            side_effect=RuntimeError("settings down"),
        ):
            elector = _resolve_scheduler_elector("scheduler")

        assert isinstance(elector, LocalFileLeaderElector)

    def test_start_default_scheduler_forwards_local_elector_when_le_disabled(
        self, monkeypatch
    ):
        """_start_default_scheduler injects the LocalFileLeaderElector into the
        scheduler when leader election is disabled.

        The scheduler is mocked, so the real elector is constructed (its ctor is
        side-effect-free) but never started — no lock file, no retry thread.
        """
        monkeypatch.setenv("BALDUR_SCHEDULER_AUTOSTART", "1")
        fake_le = MagicMock()
        fake_le.enabled = False
        mock_sched = MagicMock()

        with (
            patch(
                "baldur.coordination.config.get_leader_election_settings",
                return_value=fake_le,
            ),
            patch(
                "baldur.coordination.scheduler.get_leader_scheduler",
                return_value=mock_sched,
            ) as mock_get,
        ):
            _start_default_scheduler(task_backend="inline")

        elector_kwarg = mock_get.call_args.kwargs.get("elector")
        assert isinstance(elector_kwarg, LocalFileLeaderElector)
