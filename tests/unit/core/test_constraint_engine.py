"""Unit tests for baldur.core.constraint_engine module (#372).

Test classification:
    - Contract: _normalize_param_value doc examples, ConstraintResult properties
    - Behavior: validate 5-step pipeline, validate_and_fix, edge cases, singleton
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.constraint_engine import (
    ConstraintEngine,
    ConstraintResult,
    ConstraintViolation,
    get_constraint_engine,
    reset_constraint_engine,
)
from baldur.core.settings_dependency import (
    SettingsDependencyGraph,
    SettingsInvariant,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_safety_bounds():
    """SafetyBounds mock that passes all checks by default."""
    sb = MagicMock()
    sb.is_within_bounds.return_value = True
    sb.get_bounds.return_value = {"min": 0, "max": 100000}
    sb.clamp_to_bounds.side_effect = lambda param, val, curr=None: val
    return sb


@pytest.fixture
def mock_learning_service():
    """LearningService mock — no blocks by default."""
    ls = MagicMock()
    ls.is_manual_only_mode.return_value = False
    ls.is_parameter_blocked.return_value = (False, None)
    return ls


@pytest.fixture
def simple_graph():
    """Graph with one invariant for testing."""
    g = SettingsDependencyGraph()
    g.add_invariant(
        SettingsInvariant(
            name="ordering",
            parameters=("base", "max_val"),
            check=lambda v: v.get("max_val", 999) > v.get("base", 0),
            description="max must exceed base",
            fix=lambda v: {**v, "max_val": max(v["max_val"], v["base"] * 2)},
        )
    )
    g.freeze()
    return g


@pytest.fixture
def engine(mock_safety_bounds, simple_graph, mock_learning_service):
    """ConstraintEngine with all dependencies injected."""
    return ConstraintEngine(
        safety_bounds=mock_safety_bounds,
        dependency_graph=simple_graph,
        learning_service=mock_learning_service,
    )


# ===========================================================================
# Contract Tests
# ===========================================================================


class TestNormalizeParamValueContract:
    """Verify document-specified float normalization examples."""

    @pytest.mark.parametrize(
        ("input_val", "expected"),
        [
            (0.3, "0.3"),
            (0.1 + 0.2, "0.3"),
            (0.123456789, "0.123457"),
            (1000.0, "1000"),
            (3, "3"),
            (0.0, "0"),
            (1.5, "1.5"),
        ],
        ids=[
            "exact_0.3",
            "float_arithmetic_0.1+0.2",
            "precision_truncation",
            "trailing_zero_removal",
            "integer_value",
            "zero",
            "simple_decimal",
        ],
    )
    def test_normalize_param_value(self, input_val, expected):
        """_normalize_param_value matches document specification."""
        assert ConstraintEngine._normalize_param_value(input_val) == expected


class TestConstraintResultContract:
    """Verify ConstraintResult data model properties."""

    def test_has_errors_true_when_error_violations_exist(self):
        """has_errors is True when violations contain severity='error'."""
        result = ConstraintResult(
            passed=False,
            violations=[
                ConstraintViolation(
                    source="test", parameter="x", message="m", severity="error"
                )
            ],
        )
        assert result.has_errors is True

    def test_has_errors_false_when_no_error_violations(self):
        """has_errors is False when no violations with severity='error'."""
        result = ConstraintResult(passed=True, violations=[])
        assert result.has_errors is False

    def test_has_warnings_true_when_warnings_exist(self):
        """has_warnings is True when warnings list is non-empty."""
        result = ConstraintResult(
            passed=True,
            warnings=[
                ConstraintViolation(
                    source="inv", parameter="y", message="w", severity="warning"
                )
            ],
        )
        assert result.has_warnings is True

    def test_has_warnings_false_when_empty(self):
        """has_warnings is False when warnings list is empty."""
        result = ConstraintResult(passed=True)
        assert result.has_warnings is False


# ===========================================================================
# Behavior Tests
# ===========================================================================


class TestValidateSafetyBoundsBehavior:
    """Verify Step 1: SafetyBounds validation."""

    def test_safety_bounds_violation_creates_error(self, engine, mock_safety_bounds):
        """SafetyBounds failure adds error violation with source='safety_bounds'."""
        mock_safety_bounds.is_within_bounds.return_value = False

        result = engine.validate(
            changes={"param_a": (100.0, 200.0)},
            check_governance=False,
        )

        assert result.passed is False
        assert any(v.source == "safety_bounds" for v in result.violations)
        assert result.violations[0].severity == "error"

    def test_safety_bounds_pass_no_violation(self, engine, mock_safety_bounds):
        """SafetyBounds pass produces no violations from Step 1."""
        mock_safety_bounds.is_within_bounds.return_value = True

        result = engine.validate(
            changes={"param_a": (100.0, 105.0)},
            check_governance=False,
        )

        bounds_violations = [
            v for v in result.violations if v.source == "safety_bounds"
        ]
        assert len(bounds_violations) == 0

    def test_safety_bounds_none_skips_check(self, simple_graph, mock_learning_service):
        """Engine with safety_bounds=None skips Step 1."""
        eng = ConstraintEngine(
            safety_bounds=None,
            dependency_graph=simple_graph,
            learning_service=mock_learning_service,
        )
        result = eng.validate(
            changes={"base": (100.0, 200.0)},
            current_values={"base": 100.0, "max_val": 5000.0},
            check_governance=False,
        )
        assert not any(v.source == "safety_bounds" for v in result.violations)


class TestValidateManualOnlyBehavior:
    """Verify Step 2: ManualOnly mode check."""

    def test_manual_only_blocks_all_parameters(self, engine, mock_learning_service):
        """Manual-only mode creates error for every parameter in changes."""
        mock_learning_service.is_manual_only_mode.return_value = True

        result = engine.validate(
            changes={"p1": (1.0, 2.0), "p2": (3.0, 4.0)},
            check_governance=False,
        )

        manual_violations = [v for v in result.violations if v.source == "manual_only"]
        assert len(manual_violations) == 2
        assert result.passed is False

    def test_manual_only_skips_blacklist_check(self, engine, mock_learning_service):
        """When manual_only is True, blacklist check (Step 3) is skipped."""
        mock_learning_service.is_manual_only_mode.return_value = True

        engine.validate(
            changes={"p1": (1.0, 2.0)},
            check_governance=False,
        )

        mock_learning_service.is_parameter_blocked.assert_not_called()


class TestValidateBlacklistBehavior:
    """Verify Step 3: Blacklist check."""

    def test_blacklisted_param_creates_error(self, engine, mock_learning_service):
        """Blacklisted parameter adds error violation with source='blacklist'."""
        mock_learning_service.is_parameter_blocked.return_value = (
            True,
            SimpleNamespace(reason="flapping", expires_at="2026-04-01"),
        )

        result = engine.validate(
            changes={"param_a": (1.0, 2.0)},
            check_governance=False,
        )

        bl_violations = [v for v in result.violations if v.source == "blacklist"]
        assert len(bl_violations) == 1
        assert bl_violations[0].details["reason"] == "flapping"

    def test_blacklist_uses_normalized_value(self, engine, mock_learning_service):
        """Blacklist check passes normalized value (not raw float)."""
        engine.validate(
            changes={"p": (1.0, 0.1 + 0.2)},
            module="test_module",
            check_governance=False,
        )

        call_args = mock_learning_service.is_parameter_blocked.call_args
        assert call_args[0][2] == "0.3"  # normalized, not "0.30000000000000004"


class TestValidateLearningServiceNoneBehavior:
    """Verify behavior when learning_service=None."""

    def test_steps_2_3_skipped_when_no_learning_service(
        self, mock_safety_bounds, simple_graph
    ):
        """Steps 2-3 gracefully skipped, rest proceeds normally."""
        eng = ConstraintEngine(
            safety_bounds=mock_safety_bounds,
            dependency_graph=simple_graph,
            learning_service=None,
        )

        result = eng.validate(
            changes={"base": (100.0, 200.0)},
            current_values={"base": 100.0, "max_val": 5000.0},
            check_governance=False,
        )

        # No manual_only or blacklist violations
        assert not any(v.source == "manual_only" for v in result.violations)
        assert not any(v.source == "blacklist" for v in result.violations)


class TestValidateInvariantsBehavior:
    """Verify Step 4: Invariant check."""

    def test_invariant_violation_creates_warning(self, engine):
        """Invariant violation adds warning (not error) with source='invariant'."""
        result = engine.validate(
            changes={"base": (100.0, 5000.0)},
            current_values={"base": 100.0, "max_val": 200.0},
            check_governance=False,
        )

        inv_warnings = [w for w in result.warnings if w.source == "invariant"]
        assert len(inv_warnings) == 1
        assert inv_warnings[0].severity == "warning"

    def test_invariant_with_fix_has_suggested_fix(self, engine):
        """Fixable invariant violation includes suggested_fix string."""
        result = engine.validate(
            changes={"base": (100.0, 5000.0)},
            current_values={"base": 100.0, "max_val": 200.0},
            check_governance=False,
        )

        inv_warnings = [w for w in result.warnings if w.source == "invariant"]
        assert inv_warnings[0].suggested_fix is not None
        assert "Auto-fixable" in inv_warnings[0].suggested_fix

    def test_invariant_check_uses_merged_values(
        self, mock_safety_bounds, mock_learning_service
    ):
        """Invariants are checked against merged (current_values + proposed)."""
        g = SettingsDependencyGraph()
        g.add_invariant(
            SettingsInvariant(
                name="sum_check",
                parameters=("a", "b"),
                check=lambda v: v["a"] + v["b"] < 100,
                description="sum must be under 100",
            )
        )
        g.freeze()

        eng = ConstraintEngine(
            safety_bounds=mock_safety_bounds,
            dependency_graph=g,
            learning_service=mock_learning_service,
        )

        # a=90 (current) + b=20 (proposed) = 110 > 100 → violation
        result = eng.validate(
            changes={"b": (10.0, 20.0)},
            current_values={"a": 90.0, "b": 10.0},
            check_governance=False,
        )
        assert len(result.warnings) == 1

    def test_no_current_values_falls_back_to_changes(self, engine):
        """Without current_values, invariant checks use values from changes."""
        result = engine.validate(
            changes={"base": (100.0, 5000.0), "max_val": (200.0, 200.0)},
            current_values=None,
            check_governance=False,
        )

        # base=5000, max_val=200 → invariant violated
        assert len(result.warnings) == 1


class TestValidateGovernanceBehavior:
    """Verify Step 5: Governance check."""

    def test_governance_block_creates_error(self, engine):
        """Governance block adds error violation with source='governance'."""
        pytest.importorskip("baldur_pro")
        # Mock the lazy import inside _check_governance
        gov_result = SimpleNamespace(
            allowed=False, block_reason="KILL_SWITCH", block_message="System halted"
        )
        with patch(
            "baldur_pro.services.governance.checks.check_all_governance",
            return_value=gov_result,
        ):
            result = engine.validate(
                changes={"p": (1.0, 2.0)},
                check_governance=True,
            )

        gov_violations = [v for v in result.violations if v.source == "governance"]
        assert len(gov_violations) == 1
        assert gov_violations[0].details["block_reason"] == "KILL_SWITCH"

    def test_governance_skipped_when_flag_false(self, engine):
        """check_governance=False skips Step 5 entirely."""
        result = engine.validate(
            changes={"base": (100.0, 200.0)},
            current_values={"base": 100.0, "max_val": 5000.0},
            check_governance=False,
        )
        assert not any(v.source == "governance" for v in result.violations)


class TestValidateMultiViolationBehavior:
    """Verify multiple violations are collected, not short-circuited."""

    def test_governance_and_bounds_violations_both_collected(
        self, mock_safety_bounds, simple_graph, mock_learning_service
    ):
        """Both SafetyBounds and Governance violations appear in result."""
        pytest.importorskip("baldur_pro")
        mock_safety_bounds.is_within_bounds.return_value = False
        eng = ConstraintEngine(
            safety_bounds=mock_safety_bounds,
            dependency_graph=simple_graph,
            learning_service=mock_learning_service,
        )

        gov_result = SimpleNamespace(
            allowed=False,
            block_reason="EMERGENCY",
            block_message="Emergency active",
        )
        with patch(
            "baldur_pro.services.governance.checks.check_all_governance",
            return_value=gov_result,
        ):
            result = eng.validate(
                changes={"base": (100.0, 200.0)},
                current_values={"base": 100.0, "max_val": 5000.0},
                check_governance=True,
            )

        sources = {v.source for v in result.violations}
        assert "safety_bounds" in sources
        assert "governance" in sources


class TestValidateDisabledBehavior:
    """Verify enabled=False bypasses all checks."""

    def test_disabled_returns_passed(self, engine):
        """When settings.enabled=False, validate returns passed=True immediately."""
        mock_settings = SimpleNamespace(enabled=False)
        with patch(
            "baldur.core.constraint_engine._get_settings",
            return_value=mock_settings,
        ):
            result = engine.validate(
                changes={"anything": (0.0, 999999.0)},
                check_governance=True,
            )
        assert result.passed is True
        assert len(result.violations) == 0


class TestValidateAndFixBehavior:
    """Verify validate_and_fix auto-correction logic."""

    def test_auto_fix_disabled_returns_without_fixing(self, engine):
        """auto_fix_enabled=False returns original proposed values."""
        mock_settings = SimpleNamespace(
            enabled=True, auto_fix_enabled=False, max_fix_iterations=3
        )
        with patch(
            "baldur.core.constraint_engine._get_settings",
            return_value=mock_settings,
        ):
            proposed, result = engine.validate_and_fix(
                changes={"base": (100.0, 5000.0)},
                current_values={"base": 100.0, "max_val": 200.0},
                check_governance=False,
            )

        # Invariant violated but not fixed
        assert proposed["base"] == 5000.0
        assert result.has_warnings

    def test_auto_fix_clamps_bounds_violation(self, mock_learning_service):
        """validate_and_fix clamps safety bounds violations."""
        sb = MagicMock()
        sb.is_within_bounds.side_effect = lambda p, v, c=None: v <= 1000
        sb.clamp_to_bounds.return_value = 1000.0
        sb.get_bounds.return_value = {"min": 0, "max": 1000}

        g = SettingsDependencyGraph()
        g.freeze()

        eng = ConstraintEngine(
            safety_bounds=sb, dependency_graph=g, learning_service=mock_learning_service
        )

        mock_settings = SimpleNamespace(
            enabled=True, auto_fix_enabled=True, max_fix_iterations=3
        )
        with patch(
            "baldur.core.constraint_engine._get_settings",
            return_value=mock_settings,
        ):
            proposed, _result = eng.validate_and_fix(
                changes={"param": (500.0, 2000.0)},
                current_values={"param": 500.0},
                check_governance=False,
            )

        assert proposed["param"] == 1000.0

    def test_auto_fix_applies_invariant_fix(
        self, mock_safety_bounds, mock_learning_service
    ):
        """validate_and_fix applies invariant fix functions."""
        g = SettingsDependencyGraph()
        g.add_invariant(
            SettingsInvariant(
                name="ordering",
                parameters=("base", "max_val"),
                check=lambda v: v.get("max_val", 999) > v.get("base", 0),
                description="max must exceed base",
                fix=lambda v: {**v, "max_val": max(v["max_val"], v["base"] * 2)},
            )
        )
        g.freeze()

        eng = ConstraintEngine(
            safety_bounds=mock_safety_bounds,
            dependency_graph=g,
            learning_service=mock_learning_service,
        )

        mock_settings = SimpleNamespace(
            enabled=True, auto_fix_enabled=True, max_fix_iterations=3
        )
        with patch(
            "baldur.core.constraint_engine._get_settings",
            return_value=mock_settings,
        ):
            proposed, result = eng.validate_and_fix(
                changes={"base": (100.0, 5000.0)},
                current_values={"base": 100.0, "max_val": 200.0},
                check_governance=False,
            )

        # Fix should set max_val >= base*2 = 10000
        assert proposed.get("max_val", 0) >= 5000.0

    def test_auto_fix_exhausted_after_max_iterations(
        self, mock_safety_bounds, mock_learning_service
    ):
        """Oscillating fix stops after max_fix_iterations."""
        call_count = {"n": 0}

        def oscillating_check(v):
            call_count["n"] += 1
            return False  # Always fails

        def oscillating_fix(v):
            # Fix that changes value but invariant still fails
            return {**v, "a": v["a"] + 1}

        g = SettingsDependencyGraph()
        g.add_invariant(
            SettingsInvariant(
                name="oscillating",
                parameters=("a",),
                check=oscillating_check,
                description="always fails",
                fix=oscillating_fix,
            )
        )
        g.freeze()

        eng = ConstraintEngine(
            safety_bounds=mock_safety_bounds,
            dependency_graph=g,
            learning_service=mock_learning_service,
        )

        mock_settings = SimpleNamespace(
            enabled=True, auto_fix_enabled=True, max_fix_iterations=3
        )
        with patch(
            "baldur.core.constraint_engine._get_settings",
            return_value=mock_settings,
        ):
            _proposed, result = eng.validate_and_fix(
                changes={"a": (1.0, 2.0)},
                current_values={"a": 1.0},
                check_governance=False,
            )

        # Should have warnings (invariant still violated)
        assert result.has_warnings


class TestSingletonLifecycleBehavior:
    """Verify get_constraint_engine/reset_constraint_engine singleton pattern."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_constraint_engine()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_constraint_engine()

    def test_get_returns_constraint_engine_instance(self):
        """get_constraint_engine returns a ConstraintEngine."""
        engine = get_constraint_engine()
        assert isinstance(engine, ConstraintEngine)

    def test_get_returns_same_instance(self):
        """Repeated calls return the same singleton."""
        e1 = get_constraint_engine()
        e2 = get_constraint_engine()
        assert e1 is e2

    def test_reset_clears_singleton(self):
        """reset creates a new instance on next get."""
        e1 = get_constraint_engine()
        reset_constraint_engine()
        e2 = get_constraint_engine()
        assert e1 is not e2

    def test_singleton_has_no_learning_service(self):
        """Singleton engine has learning_service=None (core/ cannot import services/)."""
        engine = get_constraint_engine()
        assert engine._learning_service is None
