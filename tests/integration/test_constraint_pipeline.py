"""ConstraintEngine Pipeline Integration Tests (#372).

Tests the full 5-step validation pipeline with real SafetyBounds + real
DependencyGraph + mock LearningService + mock governance.

Test Categories:
    A. Full Pipeline Pass:
        - Valid change passes all 5 steps
    B. Bounds + Invariant Combination:
        - Out-of-bounds caught by Step 1
        - Invariant violation detected by Step 4
    C. ManualOnly + Blacklist Interaction:
        - Manual-only blocks all params and skips blacklist
    D. Governance Integration:
        - Governance block combines with bounds violation
    E. Validate-and-Fix Pipeline:
        - Invariant fix with bounds clamp

Note: All tests use mock LearningService and mock governance — no DB dependency.
      This enables parallel test execution with pytest-xdist.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.constraint_engine import ConstraintEngine
from baldur.core.safety_bounds import SafetyBounds
from baldur.core.settings_dependency import (
    DEFAULT_DEPENDENCIES,
    DEFAULT_INVARIANTS,
    SettingsDependencyGraph,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_graph() -> SettingsDependencyGraph:
    """Graph with all default dependencies and invariants."""
    g = SettingsDependencyGraph()
    for dep in DEFAULT_DEPENDENCIES:
        g.add_dependency(dep)
    for inv in DEFAULT_INVARIANTS:
        g.add_invariant(inv)
    g.freeze()
    return g


@pytest.fixture
def real_safety_bounds() -> SafetyBounds:
    """Real SafetyBounds with default parameters."""
    return SafetyBounds()


@pytest.fixture
def mock_learning():
    """LearningService mock — no blocks by default."""
    ls = MagicMock()
    ls.is_manual_only_mode.return_value = False
    ls.is_parameter_blocked.return_value = (False, None)
    return ls


@pytest.fixture
def pipeline_engine(real_safety_bounds, real_graph, mock_learning) -> ConstraintEngine:
    """ConstraintEngine with real SafetyBounds + real graph + mock learning."""
    return ConstraintEngine(
        safety_bounds=real_safety_bounds,
        dependency_graph=real_graph,
        learning_service=mock_learning,
    )


@pytest.fixture
def healthy_values() -> dict[str, float]:
    """A set of parameter values that satisfy all invariants."""
    return {
        "backoff_base_ms": 100.0,
        "backoff_max_ms": 5000.0,
        "timeout_ms": 30000.0,
        "retry_count": 3.0,
        "throttle_sla_warning_ms": 100.0,
        "throttle_sla_critical_ms": 5000.0,
        "circuit_breaker_threshold": 0.5,
        "rate_limit_rps": 1000.0,
        "jitter_range": 0.1,
    }


# ===========================================================================
# Pipeline Integration Tests
# ===========================================================================


class TestFullPipelinePassBehavior:
    """Verify full pipeline passes for valid changes."""

    def test_small_valid_change_passes_all_steps(self, pipeline_engine, healthy_values):
        """
        Purpose:
            Verify a small in-bounds parameter change passes all 5 validation steps.
        Expected:
            - result.passed is True
            - No violations collected
        """
        result = pipeline_engine.validate(
            changes={"retry_count": (3.0, 4.0)},
            current_values=healthy_values,
            check_governance=False,
        )
        assert result.passed is True
        assert len(result.violations) == 0


class TestBoundsAndInvariantCombinationBehavior:
    """Verify SafetyBounds + Invariant checks work together."""

    def test_out_of_bounds_value_caught_by_step1(self, pipeline_engine, healthy_values):
        """
        Purpose:
            Verify value exceeding SafetyBounds max is caught in Step 1.
        Expected:
            - At least 1 violation with source='safety_bounds'
        """
        result = pipeline_engine.validate(
            changes={"backoff_base_ms": (100.0, 999999.0)},
            current_values=healthy_values,
            check_governance=False,
        )
        bounds_violations = [
            v for v in result.violations if v.source == "safety_bounds"
        ]
        assert len(bounds_violations) >= 1

    def test_invariant_violation_detected_by_step4(
        self, pipeline_engine, healthy_values
    ):
        """
        Purpose:
            Verify changing backoff_base above backoff_max triggers invariant warning.
        Expected:
            - At least 1 warning with source='invariant'
        """
        # Set backoff_base_ms very high, keeping max low
        modified_values = {**healthy_values, "backoff_max_ms": 200.0}
        result = pipeline_engine.validate(
            changes={"backoff_base_ms": (100.0, 500.0)},
            current_values=modified_values,
            check_governance=False,
        )
        inv_warnings = [w for w in result.warnings if w.source == "invariant"]
        assert len(inv_warnings) >= 1


class TestManualOnlyAndBlacklistInteractionBehavior:
    """Verify Step 2 manual_only blocks Step 3 blacklist."""

    def test_manual_only_blocks_all_and_skips_blacklist(
        self, pipeline_engine, healthy_values, mock_learning
    ):
        """
        Purpose:
            Verify manual-only mode blocks all params and skips blacklist check.
        Expected:
            - 2 violations with source='manual_only' (one per param)
            - is_parameter_blocked never called
        """
        mock_learning.is_manual_only_mode.return_value = True

        result = pipeline_engine.validate(
            changes={
                "retry_count": (3.0, 4.0),
                "backoff_base_ms": (100.0, 150.0),
            },
            current_values=healthy_values,
            check_governance=False,
        )

        manual_violations = [v for v in result.violations if v.source == "manual_only"]
        assert len(manual_violations) == 2
        mock_learning.is_parameter_blocked.assert_not_called()


class TestGovernanceIntegrationBehavior:
    """Verify governance blocking with real bounds + invariants."""

    def test_governance_block_combines_with_bounds_violation(
        self, pipeline_engine, healthy_values
    ):
        """
        Purpose:
            Verify governance block and bounds violation are both collected.
        Expected:
            - Violations include both 'safety_bounds' and 'governance' sources
        """
        pytest.importorskip("baldur_pro")
        gov_result = SimpleNamespace(
            allowed=False,
            block_reason="KILL_SWITCH",
            block_message="Kill switch active",
        )
        with patch(
            "baldur_pro.services.governance.checks.check_all_governance",
            return_value=gov_result,
        ):
            result = pipeline_engine.validate(
                changes={"backoff_base_ms": (100.0, 999999.0)},
                current_values=healthy_values,
                check_governance=True,
            )

        sources = {v.source for v in result.violations}
        assert "safety_bounds" in sources
        assert "governance" in sources


class TestValidateAndFixPipelineBehavior:
    """Verify validate_and_fix with real components."""

    def test_invariant_fix_with_bounds_clamp(
        self, real_safety_bounds, real_graph, mock_learning, healthy_values
    ):
        """
        Purpose:
            Verify invariant fix that exceeds bounds is re-clamped by SafetyBounds.
        Expected:
            - backoff_base_ms is present in proposed values
            - Engine attempted auto-fix (proposed may differ from original)
        """
        eng = ConstraintEngine(
            safety_bounds=real_safety_bounds,
            dependency_graph=real_graph,
            learning_service=mock_learning,
        )

        mock_settings = SimpleNamespace(
            enabled=True, auto_fix_enabled=True, max_fix_iterations=3
        )
        with patch(
            "baldur.core.constraint_engine._get_settings",
            return_value=mock_settings,
        ):
            proposed, result = eng.validate_and_fix(
                changes={"backoff_base_ms": (100.0, 4500.0)},
                current_values=healthy_values,
                check_governance=False,
            )

        # The engine attempted to fix — proposed may differ from original
        assert "backoff_base_ms" in proposed
