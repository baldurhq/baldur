"""``init()`` and the per-worker starter load the control state and start the refresher.

Target: ``baldur.bootstrap._load_control_state`` / ``_start_control_state_refresher``
— every process that calls ``init()`` reads the switch state (and the emergency
level PRO's startup extension registered) once before it serves anything, then
starts its refresher; a forked worker does the same through its starter unless
its refresher already runs. Neither is gated on the fork-source predicate, and
neither lets a store failure out of startup.

Verification techniques applied (§8):
  - §8.5 Dependency interaction — one synchronous pass, then the thread; the
    starter skips the pass while a refresher runs
  - §8.12 Branch outcome — running vs not running; fork source vs worker
  - §8.2 Exception/edge cases — a failing load or start logs and never raises
  - Ordering — ``init()`` loads after the PRO extensions register their keys
    and before the background workers start
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import create_autospec, patch

import pytest
from structlog.testing import capture_logs

from baldur import bootstrap
from baldur.core import control_state as control_state_module
from baldur.core.control_state import ControlStateRefresher
from baldur.core.state_backend import (
    MemoryStateBackend,
    configure_state_backend,
    reset_state_backend,
)
from baldur.services.system_control import (
    STATE_KEY,
    SystemControlManager,
    SystemState,
    get_system_control,
    reset_system_control,
)


@pytest.fixture
def refresher_double():
    """The process refresher, replaced by a double that records the calls."""
    double = create_autospec(ControlStateRefresher, instance=True)
    double.is_running = False
    with patch.object(control_state_module, "_refresher", double):
        yield double


@pytest.fixture
def fresh_manager():
    """No manager built yet in this process; dropped again afterwards."""
    SystemControlManager._instance = None
    reset_system_control(cleanup=False)
    yield
    SystemControlManager._instance = None
    reset_system_control(cleanup=False)


def _pass_and_start_calls(double) -> list[str]:
    return [c[0] for c in double.mock_calls if c[0] in ("refresh_now", "start")]


class TestControlStateBootstrapBehavior:
    """The load and the starter: one pass, then the thread (D5, D6)."""

    def test_load_reads_once_then_starts_the_refresher(
        self, refresher_double, fresh_manager
    ):
        """A process serves its first request on a copy it read itself."""
        bootstrap._load_control_state()

        assert _pass_and_start_calls(refresher_double) == ["refresh_now", "start"]
        refresher_double.register.assert_called_once()
        assert refresher_double.register.call_args.args == (STATE_KEY,)

    def test_load_runs_in_a_fork_source_too(self, refresher_double, fresh_manager):
        """No fork-source gate: a ``--preload`` master's own jobs read a live copy."""
        with patch(
            "baldur.core.process_utils.is_fork_source_process", return_value=True
        ):
            bootstrap._load_control_state()

        assert _pass_and_start_calls(refresher_double) == ["refresh_now", "start"]

    def test_load_failure_is_logged_and_never_raised(
        self, refresher_double, fresh_manager
    ):
        """A store that cannot be reached leaves startup going."""
        refresher_double.refresh_now.side_effect = RuntimeError("store unbuildable")

        with capture_logs() as logs:
            bootstrap._load_control_state()

        failed = [
            log for log in logs if log["event"] == "baldur.control_state_load_failed"
        ]
        assert [log["log_level"] for log in failed] == ["warning"]

    @pytest.mark.parametrize(
        ("running", "expected_calls"),
        [(False, ["refresh_now", "start"]), (True, ["start"])],
        ids=["worker_without_refresher", "process_whose_refresher_runs"],
    )
    def test_starter_reads_only_when_no_refresher_runs(
        self, refresher_double, fresh_manager, running, expected_calls
    ):
        """A forked worker reads before serving; init()'s own process skips it."""
        refresher_double.is_running = running

        bootstrap._start_control_state_refresher()

        assert _pass_and_start_calls(refresher_double) == expected_calls

    def test_starter_failure_is_logged_and_never_raised(
        self, refresher_double, fresh_manager
    ):
        """A worker whose store fails still starts serving."""
        refresher_double.start.side_effect = RuntimeError("thread ceiling")

        with capture_logs() as logs:
            bootstrap._start_control_state_refresher()

        assert "baldur.control_state_refresher_start_failed" in [
            log["event"] for log in logs
        ]

    def test_load_assigns_the_stored_switch_before_returning(self, fresh_manager):
        """End to end: after the load the process already acts on the stored brake."""
        # Given: a store holding a pulled kill switch, and a fresh refresher
        store = MemoryStateBackend()
        store.set(STATE_KEY, SystemState(enabled=False).to_dict())
        configure_state_backend(store)
        refresher = ControlStateRefresher()

        # When
        try:
            with patch.object(control_state_module, "_refresher", refresher):
                bootstrap._load_control_state()
                enabled = get_system_control().is_enabled()
                known = get_system_control().is_state_known()
        finally:
            refresher._reset()
            reset_state_backend()

        # Then
        assert (enabled, known) == (False, True)


# Every init() step but the two under test, replaced by an order recorder.
_OTHER_INIT_STEPS = (
    "_validate_startup_config",
    "_register_default_event_handlers",
    "_init_bridge_instrumentation",
    "_instrument_otel_if_enabled",
    "_register_shutdown_handlers",
    "_wire_registry_defaults",
    "_validate_idempotency_cache_in_production",
    "_install_idempotency_gate",
    "_emit_tier_setting_warnings",
    "_enforce_post_hook_requirements",
    "_seed_circuit_breaker_config",
    "_warn_unknown_env_vars",
    "_apply_audit_default_provider",
    "_reconcile_distributed_hash_chain",
    "_start_audit_pipeline_if_enabled",
    "_start_dlq_outbox_if_enabled",
    "_configure_error_budget_if_enabled",
    "_register_metrics_provider_if_configured",
    "_record_env_snapshot",
    "_start_default_scheduler",
    "_register_sql_statistics_if_available",
    "_start_admin_server_if_enabled",
    "start_background_workers",
    "_arm_celery_bootstrap_receivers",
    "_schedule_celery_deferral_check",
)


class TestInitControlStateOrderBehavior:
    """The load sits after the PRO extensions and before the workers start."""

    @pytest.fixture(autouse=True)
    def _isolated_init_state(self):
        bootstrap.reset_init_state()
        yield
        bootstrap.reset_init_state()

    @staticmethod
    def _init_order() -> list[str]:
        order: list[str] = []

        def track(name):
            def step(*_args, **_kwargs):
                order.append(name)

            return step

        with ExitStack() as stack:
            for name in (
                *_OTHER_INIT_STEPS,
                "_run_pro_extensions",
                "_load_control_state",
            ):
                stack.enter_context(patch.object(bootstrap, name, track(name)))
            stack.enter_context(
                patch.object(bootstrap, "_build_startup_report", lambda *_a, **_k: {})
            )
            bootstrap.init()
        return order

    def test_load_runs_after_the_pro_extensions_register_their_keys(self):
        """The emergency level is registered by the extension; the load must see it."""
        order = self._init_order()

        assert order.index("_run_pro_extensions") < order.index("_load_control_state")

    def test_load_runs_before_the_background_workers_start(self):
        """The per-worker starter finds a loaded process with a running refresher."""
        order = self._init_order()

        assert order.index("_load_control_state") < order.index(
            "start_background_workers"
        )
