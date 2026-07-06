"""Constraint Validation Engine — unified single entry point for settings validation.

Aggregates five validation steps in order (cheapest first, fail-fast):
    1. SafetyBounds — absolute range + max change rate
    2. ManualOnly — module-level auto-tuning block
    3. Blacklist — per-parameter block
    4. Invariants — cross-parameter invariants
    5. Governance — kill switch, emergency, error budget
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.safety_bounds import SafetyBounds
from baldur.core.serializable import SerializableMixin
from baldur.core.settings_dependency import (
    SettingsDependencyGraph,
    get_dependency_graph,
)

if TYPE_CHECKING:
    from baldur.interfaces.learning import LearningServiceProtocol

logger = structlog.get_logger(__name__)

__all__ = [
    "ConstraintViolation",
    "ConstraintResult",
    "ConstraintEngine",
    "get_constraint_engine",
    "configure_constraint_engine",
    "reset_constraint_engine",
]


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class ConstraintViolation(SerializableMixin):
    """Single constraint violation detail."""

    source: str  # "safety_bounds" | "blacklist" | "manual_only" | "invariant" | "governance"
    parameter: str
    message: str
    severity: str  # "error" | "warning"
    suggested_fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConstraintResult(SerializableMixin):
    """Aggregate constraint validation result."""

    passed: bool
    violations: list[ConstraintViolation] = field(default_factory=list)
    warnings: list[ConstraintViolation] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(v.severity == "error" for v in self.violations)

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


# ---------------------------------------------------------------------------
# Helper: lazy settings loader
# ---------------------------------------------------------------------------


def _get_settings() -> Any:
    """Load SettingsDependencySettings lazily (avoid circular import)."""
    try:
        from baldur.settings.settings_dependency import (
            get_settings_dependency_settings,
        )

        return get_settings_dependency_settings()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ConstraintEngine
# ---------------------------------------------------------------------------


class ConstraintEngine:
    """Unified constraint validation — single entry point.

    Aggregates:
        1. SafetyBounds.is_within_bounds(param, proposed, current)
        2. LearningService.is_manual_only_mode(module)
        3. LearningService.is_parameter_blocked(module, param, normalized)
        4. SettingsDependencyGraph.check_invariants(values)
        5. check_all_governance(**kwargs) → GovernanceCheckResult

    Validation order: SafetyBounds → ManualOnly → Blacklist → Invariants → Governance
    """

    def __init__(
        self,
        safety_bounds: SafetyBounds | None = None,
        dependency_graph: SettingsDependencyGraph | None = None,
        learning_service: LearningServiceProtocol | None = None,
    ) -> None:
        self._safety_bounds = safety_bounds
        self._dependency_graph = dependency_graph
        self._learning_service = learning_service

    @staticmethod
    def _normalize_param_value(value: float) -> str:
        """Normalize float to canonical string for blacklist comparison.

        Ensures consistent representation regardless of float arithmetic path.
        Uses round-trip safe format: enough precision to distinguish values,
        but normalized to avoid float representation artifacts.

        Examples:
            0.3              → "0.3"
            0.1 + 0.2        → "0.3"       (not "0.30000000000000004")
            0.123456789      → "0.123457"
            1000.0           → "1000"
            3                → "3"
        """
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def validate(
        self,
        changes: dict[str, tuple[float, float]],  # param → (current, proposed)
        current_values: dict[str, float] | None = None,
        *,
        module: str = "auto_tuning",
        check_governance: bool = True,
        operation_name: str = "recommendation",
    ) -> ConstraintResult:
        """Full validation pipeline.

        Args:
            changes: Parameter changes to validate. Keys are parameter names,
                values are (current_value, proposed_value) tuples.
            current_values: Full set of current parameter values.
                Required for invariant checking context.
            module: Module context for blacklist lookup.
            check_governance: Whether to run governance checks (Step 5).
            operation_name: Operation name for governance audit trail.

        Returns:
            ConstraintResult with all violations/warnings.
        """
        settings = _get_settings()
        if settings is not None and not settings.enabled:
            return ConstraintResult(passed=True)

        violations: list[ConstraintViolation] = []
        warnings: list[ConstraintViolation] = []

        # Step 1: SafetyBounds
        self._check_safety_bounds(changes, violations)

        # Step 2: ManualOnly
        manual_only = self._check_manual_only(changes, module, violations)

        # Step 3: Blacklist (skip if manual_only — already blocked all)
        if not manual_only:
            self._check_blacklist(changes, module, violations)

        # Step 4: Invariants
        self._check_invariants(changes, current_values, warnings)

        # Step 5: Governance
        if check_governance:
            self._check_governance(operation_name, violations)

        passed = len(violations) == 0
        log_fn = logger.info if passed else logger.warning
        log_fn(
            "constraint_engine.validation_completed",
            passed=passed,
            violation_count=len(violations),
            warning_count=len(warnings),
        )

        return ConstraintResult(
            passed=passed,
            violations=violations,
            warnings=warnings,
        )

    def validate_and_fix(  # noqa: C901, PLR0912
        self,
        changes: dict[str, tuple[float, float]],
        current_values: dict[str, float],
        *,
        module: str = "auto_tuning",
        check_governance: bool = True,
    ) -> tuple[dict[str, float], ConstraintResult]:
        """Validate and auto-fix where possible.

        Returns:
            (fixed_proposed_values, final_result)
        """
        settings = _get_settings()
        max_iterations = settings.max_fix_iterations if settings else 3
        auto_fix = settings.auto_fix_enabled if settings else False

        proposed = {p: v for p, (_, v) in changes.items()}
        result: ConstraintResult | None = None

        for iteration in range(max_iterations + 1):
            # Build changes dict including any params added by fixes
            iter_changes: dict[str, tuple[float, float]] = {}
            for p, v in proposed.items():
                current = changes[p][0] if p in changes else current_values.get(p, 0.0)
                iter_changes[p] = (current, v)

            result = self.validate(
                iter_changes,
                current_values,
                module=module,
                check_governance=check_governance,
            )

            if result.passed and not result.has_warnings:
                return proposed, result

            if not auto_fix:
                return proposed, result

            if iteration >= max_iterations:
                break

            # Try to apply fixes
            any_fixed = False

            # Fix invariant violations
            if self._dependency_graph is not None:
                invariant_warnings = [
                    w for w in result.warnings if w.source == "invariant"
                ]
                if invariant_warnings:
                    merged = {**current_values, **proposed}
                    fixed_values, _descriptions = (
                        self._dependency_graph.fix_invariant_violations(merged)
                    )
                    for p, v in fixed_values.items():
                        if v != merged.get(p):
                            proposed[p] = v
                            any_fixed = True

            # Clamp bounds violations
            if self._safety_bounds is not None:
                bounds_violations = [
                    v for v in result.violations if v.source == "safety_bounds"
                ]
                for violation in bounds_violations:
                    param = violation.parameter
                    if param in proposed:
                        current = (
                            changes[param][0]
                            if param in changes
                            else current_values.get(param, 0.0)
                        )
                        clamped = self._safety_bounds.clamp_to_bounds(
                            param,
                            proposed[param],
                            current,
                        )
                        if clamped != proposed[param]:
                            proposed[param] = clamped
                            any_fixed = True

            if not any_fixed:
                break

            logger.info(
                "constraint_engine.auto_fix_applied",
                iteration=iteration + 1,
            )

        assert result is not None  # noqa: S101 — guaranteed by loop

        if not result.passed or result.has_warnings:
            logger.warning(
                "constraint_engine.auto_fix_exhausted",
                max_iterations=max_iterations,
                remaining_violations=len(result.violations),
                remaining_warnings=len(result.warnings),
            )

        return proposed, result

    # --- Private validation steps ---

    def _check_safety_bounds(
        self,
        changes: dict[str, tuple[float, float]],
        violations: list[ConstraintViolation],
    ) -> None:
        """Step 1: SafetyBounds range + change rate check."""
        if self._safety_bounds is None:
            return

        for param, (current, proposed) in changes.items():
            if not self._safety_bounds.is_within_bounds(param, proposed, current):
                bounds = self._safety_bounds.get_bounds(param)
                details: dict[str, Any] = {"proposed": proposed}
                if bounds is not None:
                    details.update(bounds)

                violations.append(
                    ConstraintViolation(
                        source="safety_bounds",
                        parameter=param,
                        message=f"Value {proposed} for '{param}' exceeds safety bounds",
                        severity="error",
                        details=details,
                    )
                )
                logger.warning(
                    "constraint_engine.safety_bounds_violated",
                    parameter=param,
                    proposed=proposed,
                    current=current,
                )

    def _check_manual_only(
        self,
        changes: dict[str, tuple[float, float]],
        module: str,
        violations: list[ConstraintViolation],
    ) -> bool:
        """Step 2: Module-level manual-only mode check. Returns True if blocked."""
        if self._learning_service is None:
            logger.debug(
                "constraint_engine.learning_service_unavailable",
                skipped_steps="manual_only, blacklist",
            )
            return False

        if self._learning_service.is_manual_only_mode(module):
            for param in changes:
                violations.append(
                    ConstraintViolation(
                        source="manual_only",
                        parameter=param,
                        message=f"Module '{module}' is in manual-only mode",
                        severity="error",
                    )
                )
            logger.warning(
                "constraint_engine.manual_only_blocked",
                module=module,
            )
            return True

        return False

    def _check_blacklist(
        self,
        changes: dict[str, tuple[float, float]],
        module: str,
        violations: list[ConstraintViolation],
    ) -> None:
        """Step 3: Per-parameter blacklist check."""
        if self._learning_service is None:
            return

        for param, (_current, proposed) in changes.items():
            normalized = self._normalize_param_value(proposed)
            blocked, info = self._learning_service.is_parameter_blocked(
                module,
                param,
                normalized,
            )
            if blocked:
                details: dict[str, Any] = {"module": module}
                if info is not None:
                    details["reason"] = getattr(info, "reason", "")
                    details["expires_at"] = str(getattr(info, "expires_at", ""))

                violations.append(
                    ConstraintViolation(
                        source="blacklist",
                        parameter=param,
                        message=(
                            f"Parameter '{param}'={normalized} is blacklisted"
                            f" for module '{module}'"
                        ),
                        severity="error",
                        details=details,
                    )
                )
                logger.warning(
                    "constraint_engine.blacklist_blocked",
                    parameter=param,
                    value=normalized,
                    module=module,
                )

    def _check_invariants(
        self,
        changes: dict[str, tuple[float, float]],
        current_values: dict[str, float] | None,
        warnings: list[ConstraintViolation],
    ) -> None:
        """Step 4: Cross-parameter invariant check."""
        if self._dependency_graph is None:
            return

        proposed_values = {p: v for p, (_, v) in changes.items()}
        if current_values is not None:
            merged = {**current_values, **proposed_values}
        else:
            merged = {
                **{p: c for p, (c, _) in changes.items()},
                **proposed_values,
            }

        inv_results = self._dependency_graph.check_invariants(merged)
        for inv, passed in inv_results:
            if not passed:
                suggested_fix = None
                if inv.fix is not None:
                    suggested_fix = f"Auto-fixable: {inv.description}"

                warnings.append(
                    ConstraintViolation(
                        source="invariant",
                        parameter=", ".join(inv.parameters),
                        message=f"Invariant '{inv.name}' violated: {inv.description}",
                        severity="warning",
                        suggested_fix=suggested_fix,
                    )
                )
                logger.warning(
                    "constraint_engine.invariant_violated",
                    invariant=inv.name,
                    parameters=inv.parameters,
                )

    def _check_governance(
        self,
        operation_name: str,
        violations: list[ConstraintViolation],
    ) -> None:
        """Step 5: Governance check (kill switch, emergency, error budget)."""
        try:
            from baldur.factory.registry import ProviderRegistry

            gov_result = ProviderRegistry.governance.get().check_all_governance(
                operation_name=operation_name,
                audit_on_block=True,
            )
            if not gov_result.allowed:
                violations.append(
                    ConstraintViolation(
                        source="governance",
                        parameter="*",
                        message=f"Governance blocked: {gov_result.block_message}",
                        severity="error",
                        details={
                            "block_reason": (
                                str(gov_result.block_reason)
                                if gov_result.block_reason
                                else None
                            ),
                            "block_message": gov_result.block_message,
                        },
                    )
                )
                logger.warning(
                    "constraint_engine.governance_blocked",
                    block_reason=str(gov_result.block_reason),
                    block_message=gov_result.block_message,
                )
        except ImportError:
            logger.debug(
                "constraint_engine.governance_unavailable",
                message="Governance checks skipped: module not available",
            )


# ---------------------------------------------------------------------------
# Singleton Factory
# ---------------------------------------------------------------------------


def _create_constraint_engine() -> ConstraintEngine:
    return ConstraintEngine(
        safety_bounds=SafetyBounds(),
        dependency_graph=get_dependency_graph(),
        learning_service=None,
    )


from baldur.utils.singleton import make_singleton_factory

get_constraint_engine, configure_constraint_engine, reset_constraint_engine = (
    make_singleton_factory("constraint_engine", _create_constraint_engine)
)
