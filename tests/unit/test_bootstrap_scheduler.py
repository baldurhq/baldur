"""Unit tests for bootstrap scheduler wiring (429 Part 6 / D6).

Scope:
- _start_default_scheduler AUTOSTART=0 early-return.
- Default job list registers six jobs (archive_old_dlq_entries, cb_recovery,
  cleanup_expired_config, daily_report, sla_drift, config_apply).
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
    _DEFAULT_SCHEDULED_JOBS,
    _PRO_GATED_JOBS,
    _build_celery_delegator,
    _build_config_apply_callable,
    _resolve_job_callable,
    _resolve_scheduler_elector,
    _start_default_scheduler,
    _tier_resolved_scheduled_jobs,
    _wrap_with_context,
)
from baldur.coordination.local_file_elector import LocalFileLeaderElector
from baldur.coordination.scheduler import LeaderScheduler


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
        """Exactly six jobs, keyed by name, with known intervals."""
        by_name = {
            name: interval for name, _mod, _attr, interval in _DEFAULT_SCHEDULED_JOBS
        }

        assert set(by_name) == {
            "daily_report",
            "sla_drift",
            "cb_recovery",
            "archive_old_dlq_entries",
            "cleanup_expired_config",
            "config_apply",
        }
        # Daily cadence — 24h in seconds
        assert by_name["daily_report"] == 24 * 60 * 60.0
        assert by_name["archive_old_dlq_entries"] == 24 * 60 * 60.0
        # Hourly cadence
        assert by_name["sla_drift"] == 60 * 60.0
        assert by_name["cleanup_expired_config"] == 60 * 60.0
        # Per-minute
        assert by_name["cb_recovery"] == 60.0
        # 665 D2 — config apply every 30s
        assert by_name["config_apply"] == 30.0


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
        """OSS registers the PRO set minus exactly the gated job.

        Guards the blast radius: a gate that also dropped an unrelated job — or
        that dropped nothing — leaves this equality failing. Both tiers are
        driven in one test so the two registered sets are directly comparable.
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
