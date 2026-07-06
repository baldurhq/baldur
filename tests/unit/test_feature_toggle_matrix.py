"""
Feature Toggle Matrix Tests (doc 426, Phase D).

Parametrized verification that every feature with a master toggle
handles the disabled state gracefully:
1. Settings load correctly with enabled=False
2. Entry points return safe no-op values without crash
3. Singleton cache isolation via reset_*_settings() in teardown

Essential trade-off (C2): feature-specific edge cases are NOT covered here.
Those belong in per-feature unit tests. This matrix verifies toggle behavior only.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.settings.root import reset_config

# ============================================================================
# Toggle settings matrix: (env_prefix, settings_module, settings_class,
#                           getter_fn_name, reset_fn_name)
# ============================================================================
TOGGLE_SETTINGS_MATRIX = [
    # OSS Tier
    (
        "BALDUR_CB_",
        "baldur.settings.circuit_breaker",
        "CircuitBreakerSettings",
        "get_circuit_breaker_settings",
        "reset_circuit_breaker_settings",
    ),
    (
        "BALDUR_METRICS_",
        "baldur.settings.metrics",
        "MetricsSettings",
        "get_metrics_settings",
        "reset_metrics_settings",
    ),
    # Phase A new toggles (D5)
    (
        "BALDUR_RETRY_",
        "baldur.settings.retry",
        "RetrySettings",
        "get_retry_settings",
        "reset_retry_settings",
    ),
    (
        "BALDUR_IDEMPOTENCY_",
        "baldur.settings.idempotency",
        "IdempotencySettings",
        "get_idempotency_settings",
        "reset_idempotency_settings",
    ),
    (
        "BALDUR_DAILY_REPORT_",
        "baldur.settings.daily_report",
        "DailyReportSettings",
        "get_daily_report_settings",
        "reset_daily_report_settings",
    ),
    (
        "BALDUR_HEDGING_",
        "baldur.settings.hedging",
        "HedgingSettings",
        "get_hedging_settings",
        "reset_hedging_settings",
    ),
    (
        "BALDUR_POOL_MONITOR_",
        "baldur.settings.pool_monitor",
        "PoolMonitorSettings",
        "get_pool_monitor_settings",
        "reset_pool_monitor_settings",
    ),
    (
        "BALDUR_AUTO_TUNING_",
        "baldur.settings.auto_tuning",
        "AutoTuningSettings",
        "get_auto_tuning_settings",
        "reset_auto_tuning_settings",
    ),
    # PRO Tier (existing toggles)
    (
        "BALDUR_DLQ_",
        "baldur.settings.dlq",
        "DLQSettings",
        "get_dlq_settings",
        "reset_dlq_settings",
    ),
    (
        "BALDUR_AUDIT_",
        "baldur.settings.audit",
        "AuditSettings",
        "get_audit_settings",
        "reset_audit_settings",
    ),
    (
        "BALDUR_ADMISSION_CONTROL_",
        "baldur.settings.admission_control",
        "AdmissionControlSettings",
        "get_admission_control_settings",
        "reset_admission_control_settings",
    ),
    (
        "BALDUR_CHAOS_",
        "baldur.settings.chaos",
        "ChaosSettings",
        "get_chaos_settings",
        "reset_chaos_settings",
    ),
    (
        "BALDUR_ERROR_BUDGET_GATE_",
        "baldur.settings.error_budget_gate",
        "ErrorBudgetGateSettings",
        "get_error_budget_gate_settings",
        "reset_error_budget_gate_settings",
    ),
    # PostmortemSettings uses 'auto_enabled', not 'enabled' — non-standard toggle
    # ScaleSettings has no master 'enabled' — uses per-subsystem toggles
    (
        "BALDUR_SAGA_",
        "baldur.settings.saga",
        "SagaSettings",
        "get_saga_settings",
        "reset_saga_settings",
    ),
    (
        "BALDUR_META_WATCHDOG_",
        "baldur.settings.meta_watchdog",
        "MetaWatchdogSettings",
        "get_meta_watchdog_settings",
        "reset_meta_watchdog_settings",
    ),
    (
        "BALDUR_NOTIFICATION_",
        "baldur.settings.notification",
        "NotificationSettings",
        "get_notification_settings",
        "reset_notification_settings",
    ),
    (
        "BALDUR_CIRCUIT_MESH_",
        "baldur.settings.circuit_mesh",
        "CircuitMeshSettings",
        "get_circuit_mesh_settings",
        "reset_circuit_mesh_settings",
    ),
    (
        "BALDUR_CELL_TOPOLOGY_",
        "baldur.settings.cell_topology",
        "CellTopologySettings",
        "get_cell_topology_settings",
        "reset_cell_topology_settings",
    ),
]


def _id_from_env_prefix(item):
    """Generate readable test ID from env prefix."""
    return item[0].replace("BALDUR_", "").rstrip("_")


# ============================================================================
# Settings-level toggle verification
# ============================================================================


class TestToggleSettingsMatrix:
    """Verify every master toggle can be set to False via env var."""

    @pytest.fixture(autouse=True)
    def _reset_root(self):
        reset_config()
        yield
        reset_config()

    @pytest.mark.parametrize(
        ("env_prefix", "module_path", "class_name", "getter_name", "reset_name"),
        TOGGLE_SETTINGS_MATRIX,
        ids=[_id_from_env_prefix(item) for item in TOGGLE_SETTINGS_MATRIX],
    )
    def test_toggle_false_via_env(
        self,
        monkeypatch,
        env_prefix,
        module_path,
        class_name,
        getter_name,
        reset_name,
    ):
        """Settings class accepts enabled=False without error."""
        import importlib

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        reset_fn = getattr(mod, reset_name)

        monkeypatch.setenv(f"{env_prefix}ENABLED", "false")
        reset_fn()
        reset_config()

        settings = cls()
        assert settings.enabled is False

    @pytest.mark.parametrize(
        ("env_prefix", "module_path", "class_name", "getter_name", "reset_name"),
        TOGGLE_SETTINGS_MATRIX,
        ids=[_id_from_env_prefix(item) for item in TOGGLE_SETTINGS_MATRIX],
    )
    def test_toggle_default_is_defined(
        self,
        env_prefix,
        module_path,
        class_name,
        getter_name,
        reset_name,
    ):
        """Every settings class has an explicit enabled default (True or False)."""
        import importlib

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        settings = cls()

        assert isinstance(settings.enabled, bool)


# ============================================================================
# Entry point tests for Phase A new toggles (D5)
# ============================================================================


class TestRetryToggleDisabled:
    """RetryPolicy.execute() with global retry disabled."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from baldur.settings.retry import reset_retry_settings

        reset_retry_settings()
        reset_config()
        yield
        reset_retry_settings()
        reset_config()

    def test_single_execution_on_success(self, monkeypatch):
        """When retry disabled, function executes once and succeeds."""
        from baldur.interfaces.resilience_policy import PolicyOutcome
        from baldur.services.retry_handler.models import RetryPolicyConfig
        from baldur.services.retry_handler.policy import RetryPolicy

        monkeypatch.setenv("BALDUR_RETRY_ENABLED", "false")
        reset_config()

        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=3, domain="test"))
        result = policy.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.total_attempts == 1

    def test_single_execution_on_failure(self, monkeypatch):
        """When retry disabled, function executes once and propagates error."""
        from baldur.interfaces.resilience_policy import PolicyOutcome
        from baldur.services.retry_handler.models import RetryPolicyConfig
        from baldur.services.retry_handler.policy import RetryPolicy

        monkeypatch.setenv("BALDUR_RETRY_ENABLED", "false")
        reset_config()

        def failing():
            raise ValueError("boom")

        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=3, domain="test"))
        result = policy.execute(failing)

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, ValueError)
        assert result.total_attempts == 1


class TestIdempotencyToggleDisabled:
    """IdempotencyGuard.check() with idempotency disabled."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        reset_config()
        yield
        reset_idempotency_settings()
        reset_config()

    def test_guard_allows_when_disabled(self, monkeypatch):
        """Disabled idempotency guard allows all requests through."""
        from baldur.interfaces.resilience_policy import PolicyContext
        from baldur.resilience.policies.idempotency import IdempotencyGuard

        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ENABLED", "false")
        reset_config()

        guard = IdempotencyGuard(key_generator=lambda ctx: "test-key")
        ctx = PolicyContext(domain="test")
        result = guard.check(context=ctx)

        assert result.allowed is True


class TestDailyReportToggleDisabled:
    """Daily report with feature disabled."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from baldur.settings.daily_report import reset_daily_report_settings

        reset_daily_report_settings()
        reset_config()
        yield
        reset_daily_report_settings()
        reset_config()

    def test_returns_disabled_status(self, monkeypatch):
        """Disabled daily report returns status dict without executing."""
        monkeypatch.setenv("BALDUR_DAILY_REPORT_ENABLED", "false")
        reset_config()

        from baldur.tasks.daily_report import generate_daily_autonomous_report

        result = generate_daily_autonomous_report()

        assert result["status"] == "disabled"


class TestHedgingToggleDisabled:
    """HedgingPolicy.execute() with hedging disabled."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from baldur.settings.hedging import reset_hedging_settings

        reset_hedging_settings()
        reset_config()
        yield
        reset_hedging_settings()
        reset_config()

    def test_falls_back_to_single_execution(self, monkeypatch):
        """Disabled hedging falls back to single execution of primary func."""
        pytest.importorskip("baldur_pro")
        from baldur.resilience.policies.hedging import HedgingPolicy

        monkeypatch.setenv("BALDUR_HEDGING_ENABLED", "false")
        reset_config()

        policy = HedgingPolicy(candidates=[lambda: "alt"])
        result = policy.execute(lambda: "primary")

        assert result.value == "primary"


class TestPoolMonitorToggleDisabled:
    """ConnectionPoolMonitor with monitoring disabled."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from baldur.settings.pool_monitor import reset_pool_monitor_settings

        reset_pool_monitor_settings()
        reset_config()
        yield
        reset_pool_monitor_settings()
        reset_config()

    def test_check_health_returns_healthy(self, monkeypatch):
        """Disabled monitor returns HEALTHY with zero stats."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.pool_monitor import (
            ConnectionPoolMonitor,
            PoolHealthStatus,
        )

        monkeypatch.setenv("BALDUR_POOL_MONITOR_ENABLED", "false")
        reset_config()

        monitor = ConnectionPoolMonitor.from_settings()
        status, stats = monitor.check_health()

        assert status == PoolHealthStatus.HEALTHY
        assert stats.active_connections == 0


class TestAutoTuningToggleDisabled:
    """AutoTuningService.start() with auto-tuning disabled."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from baldur.settings.auto_tuning import reset_auto_tuning_settings

        reset_auto_tuning_settings()
        reset_config()
        yield
        reset_auto_tuning_settings()
        reset_config()

    def test_start_returns_false(self, monkeypatch):
        """Disabled auto-tuning start() returns False without starting."""
        pytest.importorskip("baldur_pro")
        monkeypatch.setenv("BALDUR_AUTO_TUNING_ENABLED", "false")
        reset_config()

        with patch(
            "baldur_pro.services.auto_tuning.service.AutoTuningService.__init__",
            return_value=None,
        ):
            from baldur_pro.services.auto_tuning.service import AutoTuningService

            service = AutoTuningService()
            # Minimal attributes to avoid AttributeError
            service._lock = MagicMock()

            result = service.start()
            assert result is False


# ============================================================================
# Cross-service disabled feature interaction (D1 Null Object)
# ============================================================================


class TestNullObjectCrossService:
    """Verify Null Object patterns for cross-service calls."""

    def test_control_api_service_with_null_cb(self):
        """ControlAPIService initializes with NullCircuitBreakerService on import failure."""
        from baldur.services.control_api_service.service import (
            NullCircuitBreakerService,
        )

        null_cb = NullCircuitBreakerService()

        result = null_cb.force_close("test-service", reason="test")
        assert result.success is False
        assert result.service_name == "test-service"

        result = null_cb.force_open("test-service")
        assert result.success is False

        state = null_cb.get_or_create_state("test-service")
        assert state.failure_count == 0
        assert state.state == "closed"

        assert null_cb.get_all_states() == []

        # No-op methods should not raise
        null_cb.record_failure("test-service")
        null_cb.record_success("test-service")
