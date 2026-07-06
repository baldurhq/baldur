"""
Settings Cross-Validation Unit Tests.

Design doc: 420

Tests:
- Contract: Log event names, severity levels, runbook_url format
- Behavior: Each conflict check triggers/does not trigger correctly
- Boundary: Just below/at/above trigger thresholds
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from baldur.settings.cross_validation import (
    _check_backoff_cb_timeout,
    _check_dlq_replay_ratio,
    _check_error_budget_governance,
    _check_retry_cb_timeout,
    _check_sla_slo_hierarchy,
    _check_throttle_admission_starvation,
    check_all,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_settings() -> MagicMock:
    """Create a mock BaldurSettings with configurable values."""
    settings = MagicMock()

    # Default safe values (no conflict triggers)
    # Conflict 2
    settings.services_group.error_budget_gate.critical_threshold_percent = 10.0
    settings.services_group.governance.default_mode = "NORMAL"

    # Conflict 3a/3b
    settings.core.backoff.exponential_max_delay = 60.0
    settings.core.retry.max_delay = 60.0
    settings.core.circuit_breaker.recovery_timeout = 60

    # Conflict 4
    settings.scaling.throttle.min_limit = 10
    settings.core.admission_control.tier_non_essential_max_concurrent = 20

    # Conflict 5
    settings.slo_group.sla.default_hours = 24
    settings.slo_group.slo.default_window_days = 30

    # Conflict 6
    settings.services_group.dlq.overflow_evict_batch_size = 500
    settings.services_group.replay_automation.on_recovery_max_items = 50

    return settings


# =============================================================================
# Contract Tests — Event name and field structure
# =============================================================================


class TestCrossValidationEventNameContract:
    """Verify log event name follows LOGGING_STANDARDS convention."""

    def test_event_name_is_settings_conflict_detected(self, mock_settings):
        """All conflict warnings use 'settings.conflict_detected' event name."""
        # Given: settings that trigger conflict 2
        mock_settings.services_group.error_budget_gate.critical_threshold_percent = 0.0

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_error_budget_governance(mock_settings)

            # Then
            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            assert call_args[0][0] == "settings.conflict_detected"


class TestRunbookUrlFormatContract:
    """Verify runbook_url field format in all conflict checks."""

    @pytest.mark.parametrize(
        ("conflict_name", "expected_runbook"),
        [
            (
                "error_budget_governance",
                "/docs/runbooks/settings/error-budget-governance.md",
            ),
            ("backoff_cb_timeout", "/docs/runbooks/settings/backoff-cb-timeout.md"),
            ("retry_cb_timeout", "/docs/runbooks/settings/retry-cb-timeout.md"),
            (
                "throttle_admission_starvation",
                "/docs/runbooks/settings/throttle-admission.md",
            ),
            ("sla_slo_hierarchy", "/docs/runbooks/settings/sla-slo-hierarchy.md"),
            ("dlq_replay_ratio", "/docs/runbooks/settings/dlq-replay-ratio.md"),
        ],
    )
    def test_runbook_url_format(
        self, mock_settings, conflict_name: str, expected_runbook: str
    ):
        """Each conflict check includes correct runbook_url."""
        # Given: settings that trigger the specific conflict
        self._setup_conflict_trigger(mock_settings, conflict_name)

        # When
        check_fn = self._get_check_function(conflict_name)
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            check_fn(mock_settings)

            # Then
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["runbook_url"] == expected_runbook

    def _setup_conflict_trigger(self, settings: MagicMock, conflict: str) -> None:
        """Configure settings to trigger the specified conflict."""
        if conflict == "error_budget_governance":
            settings.services_group.error_budget_gate.critical_threshold_percent = 0.0
        elif conflict == "backoff_cb_timeout":
            settings.core.backoff.exponential_max_delay = 400.0
            settings.core.circuit_breaker.recovery_timeout = 60
        elif conflict == "retry_cb_timeout":
            settings.core.retry.max_delay = 400.0
            settings.core.circuit_breaker.recovery_timeout = 60
        elif conflict == "throttle_admission_starvation":
            settings.scaling.throttle.min_limit = 5
            settings.core.admission_control.tier_non_essential_max_concurrent = 5
        elif conflict == "sla_slo_hierarchy":
            settings.slo_group.sla.default_hours = 800
            settings.slo_group.slo.default_window_days = 30
        elif conflict == "dlq_replay_ratio":
            settings.services_group.dlq.overflow_evict_batch_size = 1000
            settings.services_group.replay_automation.on_recovery_max_items = 50

    def _get_check_function(self, conflict: str) -> Any:
        """Get the check function for the specified conflict."""
        mapping = {
            "error_budget_governance": _check_error_budget_governance,
            "backoff_cb_timeout": _check_backoff_cb_timeout,
            "retry_cb_timeout": _check_retry_cb_timeout,
            "throttle_admission_starvation": _check_throttle_admission_starvation,
            "sla_slo_hierarchy": _check_sla_slo_hierarchy,
            "dlq_replay_ratio": _check_dlq_replay_ratio,
        }
        return mapping[conflict]


class TestSeverityLevelContract:
    """Verify severity levels match design doc specification."""

    def test_error_budget_governance_severity_is_high(self, mock_settings):
        """Conflict 2 has HIGH severity."""
        mock_settings.services_group.error_budget_gate.critical_threshold_percent = 0.0

        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_error_budget_governance(mock_settings)
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["severity"] == "HIGH"

    def test_backoff_cb_timeout_severity_is_medium(self, mock_settings):
        """Conflict 3a has MEDIUM severity."""
        mock_settings.core.backoff.exponential_max_delay = 400.0

        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_backoff_cb_timeout(mock_settings)
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["severity"] == "MEDIUM"

    def test_retry_cb_timeout_severity_is_medium(self, mock_settings):
        """Conflict 3b has MEDIUM severity."""
        mock_settings.core.retry.max_delay = 400.0

        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_retry_cb_timeout(mock_settings)
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["severity"] == "MEDIUM"

    def test_throttle_admission_starvation_severity_is_medium(self, mock_settings):
        """Conflict 4 has MEDIUM severity."""
        mock_settings.scaling.throttle.min_limit = 5
        mock_settings.core.admission_control.tier_non_essential_max_concurrent = 5

        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_throttle_admission_starvation(mock_settings)
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["severity"] == "MEDIUM"

    def test_sla_slo_hierarchy_severity_is_medium(self, mock_settings):
        """Conflict 5 has MEDIUM severity."""
        mock_settings.slo_group.sla.default_hours = 800

        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_sla_slo_hierarchy(mock_settings)
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["severity"] == "MEDIUM"

    def test_dlq_replay_ratio_severity_is_low(self, mock_settings):
        """Conflict 6 has LOW severity."""
        mock_settings.services_group.dlq.overflow_evict_batch_size = 1000
        mock_settings.services_group.replay_automation.on_recovery_max_items = 50

        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_dlq_replay_ratio(mock_settings)
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["severity"] == "LOW"


# =============================================================================
# Behavior Tests — Conflict 2: Error Budget + Governance
# =============================================================================


class TestErrorBudgetGovernanceBehavior:
    """Conflict 2: critical_threshold_percent == 0.0 AND default_mode == 'NORMAL'."""

    def test_triggers_when_threshold_zero_and_mode_normal(self, mock_settings):
        """Warning logged when threshold=0.0 and mode='NORMAL'."""
        # Given
        mock_settings.services_group.error_budget_gate.critical_threshold_percent = 0.0
        mock_settings.services_group.governance.default_mode = "NORMAL"

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_error_budget_governance(mock_settings)

            # Then
            mock_logger.warning.assert_called_once()
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["conflict"] == "error_budget_governance"

    def test_no_warning_when_threshold_positive(self, mock_settings):
        """No warning when threshold > 0."""
        # Given
        mock_settings.services_group.error_budget_gate.critical_threshold_percent = 0.1
        mock_settings.services_group.governance.default_mode = "NORMAL"

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_error_budget_governance(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()

    def test_no_warning_when_mode_strict(self, mock_settings):
        """No warning when mode='STRICT' even if threshold=0."""
        # Given
        mock_settings.services_group.error_budget_gate.critical_threshold_percent = 0.0
        mock_settings.services_group.governance.default_mode = "STRICT"

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_error_budget_governance(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()

    def test_no_warning_with_default_values(self, mock_settings):
        """No false positive with default threshold=10.0."""
        # Given: defaults (threshold=10.0, mode="NORMAL")

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_error_budget_governance(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()


# =============================================================================
# Behavior Tests — Conflict 3a: Backoff max_delay > CB recovery_timeout × 5
# =============================================================================


class TestBackoffCbTimeoutBehavior:
    """Conflict 3a: backoff.exponential_max_delay > circuit_breaker.recovery_timeout * 5."""

    def test_triggers_when_backoff_exceeds_threshold(self, mock_settings):
        """Warning logged when max_delay > recovery_timeout * 5."""
        # Given: recovery_timeout=60, threshold=300, max_delay=301
        mock_settings.core.circuit_breaker.recovery_timeout = 60
        mock_settings.core.backoff.exponential_max_delay = 301.0

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_backoff_cb_timeout(mock_settings)

            # Then
            mock_logger.warning.assert_called_once()
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["conflict"] == "backoff_cb_timeout"
            assert call_kwargs["backoff_max_delay_s"] == 301.0
            assert call_kwargs["cb_recovery_timeout_s"] == 60
            assert call_kwargs["threshold_s"] == 300

    def test_no_warning_at_boundary_exact(self, mock_settings):
        """No warning at exact boundary (60 = 60*5/5, strict >)."""
        # Given: recovery_timeout=60, threshold=300, max_delay=300 (exact boundary)
        mock_settings.core.circuit_breaker.recovery_timeout = 60
        mock_settings.core.backoff.exponential_max_delay = 300.0

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_backoff_cb_timeout(mock_settings)

            # Then: strict > means 300 does NOT trigger
            mock_logger.warning.assert_not_called()

    def test_no_warning_below_threshold(self, mock_settings):
        """No warning when max_delay < recovery_timeout * 5."""
        # Given
        mock_settings.core.circuit_breaker.recovery_timeout = 60
        mock_settings.core.backoff.exponential_max_delay = 299.0

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_backoff_cb_timeout(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()

    def test_no_warning_with_default_values(self, mock_settings):
        """No false positive with defaults (60s = 60s*5 boundary)."""
        # Given: defaults (exponential_max_delay=60, recovery_timeout=60)

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_backoff_cb_timeout(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()


# =============================================================================
# Behavior Tests — Conflict 3b: Retry max_delay > CB recovery_timeout × 5
# =============================================================================


class TestRetryCbTimeoutBehavior:
    """Conflict 3b: retry.max_delay > circuit_breaker.recovery_timeout * 5."""

    def test_triggers_when_retry_exceeds_threshold(self, mock_settings):
        """Warning logged when max_delay > recovery_timeout * 5."""
        # Given: recovery_timeout=60, threshold=300, max_delay=301
        mock_settings.core.circuit_breaker.recovery_timeout = 60
        mock_settings.core.retry.max_delay = 301.0

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_retry_cb_timeout(mock_settings)

            # Then
            mock_logger.warning.assert_called_once()
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["conflict"] == "retry_cb_timeout"
            assert call_kwargs["retry_max_delay_s"] == 301.0

    def test_no_warning_at_boundary_exact(self, mock_settings):
        """No warning at exact boundary (strict >)."""
        # Given
        mock_settings.core.circuit_breaker.recovery_timeout = 60
        mock_settings.core.retry.max_delay = 300.0

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_retry_cb_timeout(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()

    def test_3a_and_3b_fire_independently(self, mock_settings):
        """3a and 3b are independent: one high doesn't affect the other."""
        # Given: 3a triggers, 3b safe
        mock_settings.core.circuit_breaker.recovery_timeout = 60
        mock_settings.core.backoff.exponential_max_delay = 400.0
        mock_settings.core.retry.max_delay = 60.0

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_backoff_cb_timeout(mock_settings)
            assert mock_logger.warning.call_count == 1

            mock_logger.reset_mock()
            _check_retry_cb_timeout(mock_settings)
            mock_logger.warning.assert_not_called()


# =============================================================================
# Behavior Tests — Conflict 4: Throttle + Admission Control both tight
# =============================================================================


class TestThrottleAdmissionStarvationBehavior:
    """Conflict 4: throttle.min_limit <= 10 AND admission_control.tier_non_essential_max_concurrent <= 10."""

    def test_triggers_when_both_tight(self, mock_settings):
        """Warning logged when both values <= 10."""
        # Given
        mock_settings.scaling.throttle.min_limit = 5
        mock_settings.core.admission_control.tier_non_essential_max_concurrent = 5

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_throttle_admission_starvation(mock_settings)

            # Then
            mock_logger.warning.assert_called_once()
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["conflict"] == "throttle_admission_starvation"

    def test_triggers_at_boundary_both_10(self, mock_settings):
        """Warning logged when both values = 10 (inclusive)."""
        # Given
        mock_settings.scaling.throttle.min_limit = 10
        mock_settings.core.admission_control.tier_non_essential_max_concurrent = 10

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_throttle_admission_starvation(mock_settings)

            # Then
            mock_logger.warning.assert_called_once()

    def test_no_warning_when_throttle_above_10(self, mock_settings):
        """No warning when throttle.min_limit > 10."""
        # Given
        mock_settings.scaling.throttle.min_limit = 11
        mock_settings.core.admission_control.tier_non_essential_max_concurrent = 5

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_throttle_admission_starvation(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()

    def test_no_warning_when_admission_above_10(self, mock_settings):
        """No warning when tier_non_essential_max_concurrent > 10."""
        # Given
        mock_settings.scaling.throttle.min_limit = 5
        mock_settings.core.admission_control.tier_non_essential_max_concurrent = 11

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_throttle_admission_starvation(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()

    def test_no_warning_with_default_values(self, mock_settings):
        """No false positive with defaults (min_limit=10, max_concurrent=20)."""
        # Given: defaults

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_throttle_admission_starvation(mock_settings)

            # Then: max_concurrent=20 > 10, so no warning
            mock_logger.warning.assert_not_called()


# =============================================================================
# Behavior Tests — Conflict 5: SLA hours vs SLO window
# =============================================================================


class TestSlaSloHierarchyBehavior:
    """Conflict 5: sla.default_hours > slo.default_window_days * 24."""

    def test_triggers_when_sla_exceeds_slo_window(self, mock_settings):
        """Warning logged when sla_hours > slo_days * 24."""
        # Given: slo_days=30 -> slo_hours=720, sla_hours=721
        mock_settings.slo_group.slo.default_window_days = 30
        mock_settings.slo_group.sla.default_hours = 721

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_sla_slo_hierarchy(mock_settings)

            # Then
            mock_logger.warning.assert_called_once()
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["conflict"] == "sla_slo_hierarchy"
            assert call_kwargs["sla_default_hours"] == 721
            assert call_kwargs["slo_window_days"] == 30
            assert call_kwargs["slo_window_hours"] == 720

    def test_no_warning_at_boundary_exact(self, mock_settings):
        """No warning when sla_hours = slo_days * 24 (strict >)."""
        # Given
        mock_settings.slo_group.slo.default_window_days = 30
        mock_settings.slo_group.sla.default_hours = 720

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_sla_slo_hierarchy(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()

    def test_no_warning_below_threshold(self, mock_settings):
        """No warning when sla_hours < slo_days * 24."""
        # Given
        mock_settings.slo_group.slo.default_window_days = 30
        mock_settings.slo_group.sla.default_hours = 719

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_sla_slo_hierarchy(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()

    def test_no_warning_with_default_values(self, mock_settings):
        """No false positive with defaults (24h < 720h)."""
        # Given: defaults

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_sla_slo_hierarchy(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()


# =============================================================================
# Behavior Tests — Conflict 6: DLQ evict batch vs replay max items
# =============================================================================


class TestDlqReplayRatioBehavior:
    """Conflict 6: dlq.overflow_evict_batch_size / replay.on_recovery_max_items > 10."""

    def test_triggers_when_ratio_exceeds_10(self, mock_settings):
        """Warning logged when ratio > 10."""
        # Given: 1000 / 50 = 20 > 10
        mock_settings.services_group.dlq.overflow_evict_batch_size = 1000
        mock_settings.services_group.replay_automation.on_recovery_max_items = 50

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_dlq_replay_ratio(mock_settings)

            # Then
            mock_logger.warning.assert_called_once()
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["conflict"] == "dlq_replay_ratio"
            assert call_kwargs["dlq_evict_batch_size"] == 1000
            assert call_kwargs["replay_on_recovery_max_items"] == 50
            assert call_kwargs["ratio"] == 20.0

    def test_no_warning_at_boundary_exact(self, mock_settings):
        """No warning when ratio = 10 exactly (strict >)."""
        # Given: 500 / 50 = 10
        mock_settings.services_group.dlq.overflow_evict_batch_size = 500
        mock_settings.services_group.replay_automation.on_recovery_max_items = 50

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_dlq_replay_ratio(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()

    def test_no_warning_below_threshold(self, mock_settings):
        """No warning when ratio < 10."""
        # Given: 450 / 50 = 9
        mock_settings.services_group.dlq.overflow_evict_batch_size = 450
        mock_settings.services_group.replay_automation.on_recovery_max_items = 50

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_dlq_replay_ratio(mock_settings)

            # Then
            mock_logger.warning.assert_not_called()

    def test_triggers_with_default_values(self, mock_settings):
        """Default values trigger LOW warning (1000/50=20 > 10)."""
        # Given: defaults
        mock_settings.services_group.dlq.overflow_evict_batch_size = 1000
        mock_settings.services_group.replay_automation.on_recovery_max_items = 50

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_dlq_replay_ratio(mock_settings)

            # Then
            mock_logger.warning.assert_called_once()

    def test_handles_division_safely(self, mock_settings):
        """No division error when on_recovery_max_items > 0 (ge=1 constraint)."""
        # Given: minimum valid value
        mock_settings.services_group.dlq.overflow_evict_batch_size = 100
        mock_settings.services_group.replay_automation.on_recovery_max_items = 1

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            _check_dlq_replay_ratio(mock_settings)

            # Then: ratio = 100 > 10, triggers
            mock_logger.warning.assert_called_once()


# =============================================================================
# Behavior Tests — check_all() orchestration
# =============================================================================


class TestCheckAllBehavior:
    """check_all() calls all 6 conflict checks."""

    def test_calls_all_check_functions(self, mock_settings):
        """check_all() invokes all 6 conflict checks."""
        with (
            patch(
                "baldur.settings.cross_validation._check_error_budget_governance"
            ) as mock_c2,
            patch(
                "baldur.settings.cross_validation._check_backoff_cb_timeout"
            ) as mock_c3a,
            patch(
                "baldur.settings.cross_validation._check_retry_cb_timeout"
            ) as mock_c3b,
            patch(
                "baldur.settings.cross_validation._check_throttle_admission_starvation"
            ) as mock_c4,
            patch(
                "baldur.settings.cross_validation._check_sla_slo_hierarchy"
            ) as mock_c5,
            patch(
                "baldur.settings.cross_validation._check_dlq_replay_ratio"
            ) as mock_c6,
        ):
            check_all(mock_settings)

            # All functions called exactly once with settings
            mock_c2.assert_called_once_with(mock_settings)
            mock_c3a.assert_called_once_with(mock_settings)
            mock_c3b.assert_called_once_with(mock_settings)
            mock_c4.assert_called_once_with(mock_settings)
            mock_c5.assert_called_once_with(mock_settings)
            mock_c6.assert_called_once_with(mock_settings)

    def test_multiple_conflicts_all_log(self, mock_settings):
        """Multiple conflicts each produce their own warning."""
        # Given: trigger conflicts 2 and 4
        mock_settings.services_group.error_budget_gate.critical_threshold_percent = 0.0
        mock_settings.scaling.throttle.min_limit = 5
        mock_settings.core.admission_control.tier_non_essential_max_concurrent = 5
        # Also trigger 6 (default values)
        mock_settings.services_group.dlq.overflow_evict_batch_size = 1000
        mock_settings.services_group.replay_automation.on_recovery_max_items = 50

        # When
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            check_all(mock_settings)

            # Then: 3 warnings (conflict 2, 4, 6)
            assert mock_logger.warning.call_count == 3

            # Verify conflict names
            conflicts_logged = [
                call[1]["conflict"] for call in mock_logger.warning.call_args_list
            ]
            assert "error_budget_governance" in conflicts_logged
            assert "throttle_admission_starvation" in conflicts_logged
            assert "dlq_replay_ratio" in conflicts_logged


# =============================================================================
# Load-time Integration Tests — Scenario 1.7 (safe startup gate)
#
# The tests above exercise each _check_* function with a MagicMock settings
# stand-in. Scenario 1.7 ("Settings conflict -> safe startup failure") asks
# for the end-to-end load-time path: env-var override -> BaldurSettings()
# construction -> _run_cross_validation model_validator -> check_all -> log.
#
# Two contracts are also asserted here:
#   1. "Safe startup": HIGH/MEDIUM/LOW conflicts WARN, never raise.
#   2. CRITICAL absence in #420: the only CRITICAL pair (chaos + emergency)
#      moved to #421 runtime guard. No load-time path raises today.
# =============================================================================


class TestLoadTimeWiringContract:
    """Verifies cross_validation is wired into BaldurSettings load path (#420 D2)."""

    def test_baldur_settings_construction_invokes_check_all(self):
        """BaldurSettings() construction triggers cross_validation.check_all."""
        from baldur.settings.root import BaldurSettings

        with patch("baldur.settings.cross_validation.check_all") as mock_check_all:
            settings = BaldurSettings()

            # check_all is invoked at least once with the settings instance.
            # (Pydantic may run the validator more than once during construction;
            # the contract is that it fires on the load-time path.)
            assert mock_check_all.call_count >= 1
            assert mock_check_all.call_args_list[0].args[0] is settings

    def test_run_cross_validation_returns_self(self):
        """The model_validator returns the settings instance unchanged."""
        from baldur.settings.root import BaldurSettings

        # Construction succeeds and yields a usable instance — the validator
        # contract for mode="after" is `(self) -> Self`.
        settings = BaldurSettings()
        assert isinstance(settings, BaldurSettings)


class TestLoadTimeWarnBehavior:
    """Scenario 1.7: HIGH/MEDIUM/LOW conflicts WARN at load time, do NOT raise."""

    def test_high_severity_conflict_at_load_time_warns_does_not_raise(
        self, monkeypatch
    ):
        """Conflict 2 (HIGH) via env var: BaldurSettings() succeeds + HIGH warning."""
        # Given: env vars trigger error_budget_governance (HIGH)
        monkeypatch.setenv("BALDUR_ERROR_BUDGET_GATE_CRITICAL_THRESHOLD_PERCENT", "0.0")
        monkeypatch.setenv("BALDUR_GOVERNANCE_DEFAULT_MODE", "NORMAL")

        from baldur.settings.root import BaldurSettings

        # When: construct BaldurSettings (must NOT raise)
        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            settings = BaldurSettings()

            # Then: HIGH-severity warning logged for the right conflict
            high_warnings = [
                call
                for call in mock_logger.warning.call_args_list
                if call.kwargs.get("severity") == "HIGH"
                and call.kwargs.get("conflict") == "error_budget_governance"
            ]
            assert len(high_warnings) >= 1
            # Conflicting values still in effect — warn-only, no override
            assert (
                settings.services_group.error_budget_gate.critical_threshold_percent
                == 0.0
            )
            assert settings.services_group.governance.default_mode == "NORMAL"

    def test_medium_severity_conflict_at_load_time_warns_does_not_raise(
        self, monkeypatch
    ):
        """Conflict 3a (MEDIUM) via env var: BaldurSettings() succeeds + MEDIUM warning."""
        # Given: backoff_max=500 > recovery_timeout(60)*5=300
        monkeypatch.setenv("BALDUR_BACKOFF_EXPONENTIAL_MAX_DELAY", "500.0")

        from baldur.settings.root import BaldurSettings

        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            settings = BaldurSettings()

            medium_warnings = [
                call
                for call in mock_logger.warning.call_args_list
                if call.kwargs.get("severity") == "MEDIUM"
                and call.kwargs.get("conflict") == "backoff_cb_timeout"
            ]
            assert len(medium_warnings) >= 1
            assert settings.core.backoff.exponential_max_delay == 500.0

    def test_safe_startup_with_multiple_conflicts_does_not_raise(self, monkeypatch):
        """Multiple HIGH+MEDIUM conflicts at load time: BaldurSettings() succeeds."""
        # Given: HIGH + MEDIUM triggered simultaneously via env
        monkeypatch.setenv("BALDUR_ERROR_BUDGET_GATE_CRITICAL_THRESHOLD_PERCENT", "0.0")
        monkeypatch.setenv("BALDUR_BACKOFF_EXPONENTIAL_MAX_DELAY", "500.0")

        from baldur.settings.root import BaldurSettings

        # When/Then: construction must succeed (safe startup contract)
        settings = BaldurSettings()
        assert (
            settings.services_group.error_budget_gate.critical_threshold_percent == 0.0
        )
        assert settings.core.backoff.exponential_max_delay == 500.0


class TestNoCriticalRaisesAtLoadTimeContract:
    """No #420 conflict raises — CRITICAL chaos+emergency split to #421 runtime."""

    def test_all_six_conflicts_simultaneously_only_warn(self, monkeypatch):
        """Trigger all 6 conflicts at once: BaldurSettings() warns >=6, raises 0."""
        # Given: env-var triggers for each conflict in 420
        # Conflict 2 (HIGH)
        monkeypatch.setenv("BALDUR_ERROR_BUDGET_GATE_CRITICAL_THRESHOLD_PERCENT", "0.0")
        monkeypatch.setenv("BALDUR_GOVERNANCE_DEFAULT_MODE", "NORMAL")
        # Conflict 3a (MEDIUM)
        monkeypatch.setenv("BALDUR_BACKOFF_EXPONENTIAL_MAX_DELAY", "500.0")
        # Conflict 3b (MEDIUM)
        monkeypatch.setenv("BALDUR_RETRY_MAX_DELAY", "500.0")
        # Conflict 4 (MEDIUM): both <= 10
        monkeypatch.setenv("BALDUR_THROTTLE_MIN_LIMIT", "5")
        monkeypatch.setenv(
            "BALDUR_ADMISSION_CONTROL_TIER_NON_ESSENTIAL_MAX_CONCURRENT", "5"
        )
        # Conflict 5 (MEDIUM): shrink SLO window to 1 day (24h) so any SLA > 24h
        # triggers the hierarchy check. SLA per-field bound is le=720, so we
        # cannot drive sla_hours above 720; instead we reduce the SLO window.
        monkeypatch.setenv("BALDUR_SLO_DEFAULT_WINDOW_DAYS", "1")
        monkeypatch.setenv("BALDUR_SLA_DEFAULT_HOURS", "25")
        # Conflict 6 (LOW): bump DLQ evict batch so ratio = 2000/100 = 20 > 10.
        # Current defaults (500/100 = 5) do NOT trigger; spec table in #420 lists
        # 1000/50 but field defaults were since adjusted.
        monkeypatch.setenv("BALDUR_DLQ_OVERFLOW_EVICT_BATCH_SIZE", "2000")

        from baldur.settings.root import BaldurSettings

        with patch("baldur.settings.cross_validation.logger") as mock_logger:
            # Must NOT raise — proves no CRITICAL load-time path exists
            settings = BaldurSettings()

            # Every #420 conflict produced its warning
            conflicts_seen = {
                call.kwargs.get("conflict")
                for call in mock_logger.warning.call_args_list
                if call.kwargs.get("conflict") is not None
            }
            assert conflicts_seen == {
                "error_budget_governance",
                "backoff_cb_timeout",
                "retry_cb_timeout",
                "throttle_admission_starvation",
                "sla_slo_hierarchy",
                "dlq_replay_ratio",
            }

            # All severities are warn-tier — no CRITICAL/raise path
            severities = {
                call.kwargs.get("severity")
                for call in mock_logger.warning.call_args_list
                if call.kwargs.get("severity") is not None
            }
            assert severities == {"HIGH", "MEDIUM", "LOW"}
            assert "CRITICAL" not in severities

            # Settings instance is fully constructed despite all conflicts
            assert isinstance(settings, BaldurSettings)
