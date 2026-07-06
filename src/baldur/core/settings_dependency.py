"""Settings Dependency Graph — DAG of cross-parameter relationships.

Tracks directed dependencies between settings parameters and enforces
cross-parameter invariants.  Uses a freeze pattern for thread safety:
registration at init time only, all reads lock-free after freeze().
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.exceptions import BaldurError

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

__all__ = [
    "CycleDetectedError",
    "DependencyType",
    "SettingsDependency",
    "SettingsInvariant",
    "SettingsDependencyGraph",
    "get_dependency_graph",
    "reset_dependency_graph",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CycleDetectedError(BaldurError):
    """Raised when adding a dependency would create a cycle in the graph."""

    def __init__(
        self,
        cycle: list[str],
        trigger: SettingsDependency | None = None,
    ) -> None:
        self.cycle = cycle
        self.trigger = trigger
        super().__init__(f"Cycle detected: {' → '.join(cycle)}")

    def extra_context(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {"cycle": self.cycle}
        if self.trigger:
            ctx["trigger_source"] = self.trigger.source
            ctx["trigger_target"] = self.trigger.target
        return ctx


# ---------------------------------------------------------------------------
# Enums & Data Models
# ---------------------------------------------------------------------------


class DependencyType(str, Enum):
    """Dependency relationship type between settings."""

    REQUIRES_GREATER = "requires_greater"  # A must be > B
    REQUIRES_LESS = "requires_less"  # A must be < B
    PROPORTIONAL = "proportional"  # A increases → B should increase
    INVERSE = "inverse"  # A increases → B should decrease
    CONDITIONAL = "conditional"  # Custom condition


@dataclass(frozen=True)
class SettingsDependency:
    """Directed dependency edge between two settings parameters."""

    source: str  # Parameter that changed
    target: str  # Parameter affected
    dependency_type: DependencyType
    propagation_factor: float = 1.0  # How much target should change
    description: str = ""
    condition_fn: (
        Callable[
            [dict[str, float], dict[str, float]],  # (proposed, current)
            dict[str, float],  # → {param: suggested_value}
        ]
        | None
    ) = field(default=None, compare=False, hash=False)


@dataclass(frozen=True)
class SettingsInvariant:
    """Cross-parameter invariant that must always hold."""

    name: str
    parameters: tuple[str, ...]
    check: Callable[[dict[str, float]], bool] = field(compare=False, hash=False)
    description: str = ""
    fix: Callable[[dict[str, float]], dict[str, float]] | None = field(
        default=None, compare=False, hash=False
    )


# ---------------------------------------------------------------------------
# SettingsDependencyGraph
# ---------------------------------------------------------------------------


class SettingsDependencyGraph:
    """Directed acyclic graph of settings dependencies.

    Thread safety:
        Uses freeze pattern — registration at init time only, read-only after freeze().
        All query/propagation/validation methods are lock-free (no RLock overhead).
        Attempting to add dependencies after freeze() raises RuntimeError.
    """

    def __init__(self) -> None:
        self._adjacency: dict[str, list[SettingsDependency]] = {}
        self._reverse: dict[str, list[SettingsDependency]] = {}
        self._invariants: list[SettingsInvariant] = []
        self._frozen: bool = False

    # --- Registration (before freeze only) ---

    def add_dependency(self, dep: SettingsDependency) -> None:
        """Register a dependency edge.

        Raises:
            CycleDetectedError: if adding this edge would create a cycle.
            RuntimeError: if graph is frozen.
        """
        if self._frozen:
            raise RuntimeError("Cannot add dependency to frozen graph")

        # Cycle check: if target can already reach source, adding source→target creates a cycle
        cycle_path = self._find_path(dep.target, dep.source)
        if cycle_path is not None:
            full_cycle = [dep.source, *cycle_path, dep.source]
            logger.error(
                "dependency_graph.cycle_detected",
                cycle=full_cycle,
                source=dep.source,
                target=dep.target,
            )
            raise CycleDetectedError(full_cycle, trigger=dep)

        self._adjacency.setdefault(dep.source, []).append(dep)
        self._reverse.setdefault(dep.target, []).append(dep)

        logger.debug(
            "dependency_graph.dependency_registered",
            source=dep.source,
            target=dep.target,
            type=dep.dependency_type.value,
        )

    def add_invariant(self, inv: SettingsInvariant) -> None:
        """Register a cross-parameter invariant.

        Raises:
            RuntimeError: if graph is frozen.
        """
        if self._frozen:
            raise RuntimeError("Cannot add invariant to frozen graph")

        self._invariants.append(inv)

        logger.debug(
            "dependency_graph.invariant_registered",
            name=inv.name,
            parameters=inv.parameters,
        )

    def freeze(self) -> None:
        """Freeze the graph. No more registrations allowed.

        Called by get_dependency_graph() after all defaults are registered.
        After freeze, all read operations are lock-free and thread-safe.
        """
        self._frozen = True

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    # --- Query (lock-free after freeze) ---

    def get_dependents(self, parameter: str) -> list[SettingsDependency]:
        """Get all parameters affected by this parameter."""
        return list(self._adjacency.get(parameter, []))

    def get_dependencies(self, parameter: str) -> list[SettingsDependency]:
        """Get all parameters this parameter depends on."""
        return list(self._reverse.get(parameter, []))

    def get_all_affected(
        self,
        parameter: str,
        max_depth: int = 3,
    ) -> list[tuple[str, int]]:
        """BFS traversal: all transitively affected parameters with depth."""
        result: list[tuple[str, int]] = []
        visited: set[str] = {parameter}
        queue: deque[tuple[str, int]] = deque()

        for dep in self._adjacency.get(parameter, []):
            if dep.target not in visited:
                visited.add(dep.target)
                queue.append((dep.target, 1))

        while queue:
            node, depth = queue.popleft()
            result.append((node, depth))

            if depth >= max_depth:
                continue

            for dep in self._adjacency.get(node, []):
                if dep.target not in visited:
                    visited.add(dep.target)
                    queue.append((dep.target, depth + 1))

        return result

    # --- Propagation (lock-free after freeze) ---

    def propagate(  # noqa: C901, PLR0912
        self,
        changes: dict[str, tuple[float, float]],  # param → (old, new)
        current_values: dict[str, float],
        max_depth: int | None = None,
    ) -> dict[str, float]:
        """Compute cascading adjustments via level-order BFS.

        Uses level-order traversal to ensure all sources at the same BFS depth
        compute proposals against the same effective-value snapshot, eliminating
        order-dependency when multiple sources affect the same target (diamond).

        Diamond conflict resolution:
            When multiple sources propose values for the same target, proposals
            are merged by dependency type — constraint types (REQUIRES_GREATER/
            REQUIRES_LESS) establish floor/ceiling, preference types
            (PROPORTIONAL/INVERSE) suggest values, final result is clamped.

        Returns:
            Suggested new values for affected parameters (not in changes).
        """
        if max_depth is None:
            max_depth = 3

        suggested: dict[str, float] = {}

        # Effective values: current overridden by changes, then by suggestions
        effective = dict(current_values)
        for param, (_old, new) in changes.items():
            effective[param] = new

        queue: deque[tuple[str, int]] = deque()
        for param in changes:
            queue.append((param, 0))

        processed: set[str] = set()

        while queue:
            # --- Check depth limit ---
            current_depth = queue[0][1]
            if current_depth >= max_depth:
                break

            # --- Drain current depth level ---
            level_sources: list[str] = []
            while queue and queue[0][1] == current_depth:
                param, _ = queue.popleft()
                if param not in processed:
                    level_sources.append(param)
                    processed.add(param)

            # --- Collect proposals for this level ---
            proposals: dict[str, list[tuple[float, DependencyType]]] = {}

            for param in level_sources:
                for dep in self._adjacency.get(param, []):
                    target = dep.target
                    if target in changes:
                        continue  # Don't override explicit changes

                    # CONDITIONAL: apply immediately (no cascading)
                    if dep.dependency_type == DependencyType.CONDITIONAL:
                        if dep.condition_fn is not None:
                            proposed = {p: v for p, (_, v) in changes.items()}
                            proposed.update(suggested)
                            result = dep.condition_fn(proposed, current_values)
                            for k, v in result.items():
                                if k not in changes:
                                    suggested[k] = v
                                    effective[k] = v
                        continue

                    source_new = effective.get(param, 0.0)
                    target_current = effective.get(target, 0.0)
                    new_value = self._compute_propagated_value(
                        dep,
                        param,
                        source_new,
                        target_current,
                        changes,
                        current_values,
                    )

                    if new_value is not None:
                        proposals.setdefault(target, []).append(
                            (new_value, dep.dependency_type)
                        )

            # --- Merge proposals and update effective ---
            for target, prop_list in proposals.items():
                merged = self._merge_proposals(prop_list)
                suggested[target] = merged
                effective[target] = merged
                if target not in processed:
                    queue.append((target, current_depth + 1))

        logger.debug(
            "dependency_graph.propagation_completed",
            changes_count=len(changes),
            suggested_count=len(suggested),
        )

        return suggested

    # --- Validation (lock-free after freeze) ---

    def check_invariants(
        self,
        values: dict[str, float],
    ) -> list[tuple[SettingsInvariant, bool]]:
        """Check all invariants against given values.

        Returns:
            List of (invariant, passed) pairs.
        """
        results: list[tuple[SettingsInvariant, bool]] = []

        for inv in self._invariants:
            if all(p in values for p in inv.parameters):
                passed = inv.check(values)
                results.append((inv, passed))
                if not passed:
                    logger.warning(
                        "dependency_graph.invariant_violated",
                        invariant=inv.name,
                        parameters=inv.parameters,
                        description=inv.description,
                    )
            else:
                # Parameters missing — skip (not a violation)
                results.append((inv, True))

        return results

    def fix_invariant_violations(
        self,
        values: dict[str, float],
    ) -> tuple[dict[str, float], list[str]]:
        """Auto-fix invariant violations where fix function is provided.

        Returns:
            (fixed_values, list_of_fix_descriptions)
        """
        fixed = dict(values)
        descriptions: list[str] = []

        for inv in self._invariants:
            if (
                all(p in fixed for p in inv.parameters)
                and not inv.check(fixed)
                and inv.fix is not None
            ):
                fixed = inv.fix(fixed)
                descriptions.append(f"Fixed: {inv.description}")

        return fixed, descriptions

    # --- Safety ---

    def detect_cycles(self) -> list[list[str]]:
        """Detect cycles in the dependency graph (should be empty)."""
        cycles: list[list[str]] = []
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {}

        all_nodes: set[str] = set(self._adjacency.keys())
        for deps in self._adjacency.values():
            for dep in deps:
                all_nodes.add(dep.target)

        for node in all_nodes:
            color[node] = WHITE

        def _dfs(node: str, path: list[str]) -> None:
            color[node] = GRAY
            path.append(node)

            for dep in self._adjacency.get(node, []):
                target = dep.target
                if color.get(target, WHITE) == GRAY:
                    cycle_start = path.index(target)
                    cycles.append(path[cycle_start:] + [target])
                elif color.get(target, WHITE) == WHITE:
                    _dfs(target, path)

            path.pop()
            color[node] = BLACK

        for node in all_nodes:
            if color.get(node, WHITE) == WHITE:
                _dfs(node, [])

        return cycles

    # --- Internal helpers ---

    @staticmethod
    def _merge_proposals(  # noqa: C901
        proposals: list[tuple[float, DependencyType]],
    ) -> float:
        """Merge multiple proposals for the same target parameter.

        When multiple sources affect the same target (diamond dependency),
        proposals are merged by type:
            - REQUIRES_GREATER: floor constraint — take max (most restrictive)
            - REQUIRES_LESS: ceiling constraint — take min (most restrictive)
            - PROPORTIONAL/INVERSE: preference — take max of proposals
            - Final result is clamped between floor and ceiling
        """
        if len(proposals) == 1:
            return proposals[0][0]

        floors: list[float] = []
        ceilings: list[float] = []
        preferences: list[float] = []

        for value, dep_type in proposals:
            if dep_type == DependencyType.REQUIRES_GREATER:
                floors.append(value)
            elif dep_type == DependencyType.REQUIRES_LESS:
                ceilings.append(value)
            else:  # PROPORTIONAL, INVERSE
                preferences.append(value)

        floor = max(floors) if floors else None
        ceiling = min(ceilings) if ceilings else None

        if preferences:
            result = max(preferences)
        elif floor is not None:
            result = floor
        elif ceiling is not None:
            result = ceiling
        else:
            return proposals[0][0]

        if floor is not None and ceiling is not None and floor > ceiling:
            logger.warning(
                "dependency_graph.constraint_conflict_detected",
                floor=floor,
                ceiling=ceiling,
            )

        if floor is not None:
            result = max(result, floor)
        if ceiling is not None:
            result = min(result, ceiling)

        return result

    def _find_path(self, start: str, end: str) -> list[str] | None:
        """BFS to find path from start to end via adjacency. Returns path or None."""
        if start == end:
            return []

        visited: set[str] = {start}
        queue: deque[tuple[str, list[str]]] = deque([(start, [start])])

        while queue:
            node, path = queue.popleft()
            for dep in self._adjacency.get(node, []):
                if dep.target == end:
                    return path
                if dep.target not in visited:
                    visited.add(dep.target)
                    queue.append((dep.target, [*path, dep.target]))

        return None

    @staticmethod
    def _compute_propagated_value(
        dep: SettingsDependency,
        param: str,
        source_new: float,
        target_current: float,
        changes: dict[str, tuple[float, float]],
        current_values: dict[str, float],
    ) -> float | None:
        """Compute propagated value based on dependency type."""
        if dep.dependency_type == DependencyType.REQUIRES_GREATER:
            if target_current <= source_new:
                return source_new * (1 + dep.propagation_factor)

        elif dep.dependency_type == DependencyType.REQUIRES_LESS:
            if target_current >= source_new:
                return source_new * (1 - dep.propagation_factor)

        elif dep.dependency_type == DependencyType.PROPORTIONAL:
            source_old = (
                changes[param][0]
                if param in changes
                else current_values.get(param, 0.0)
            )
            if source_old != 0:
                delta_ratio = (source_new - source_old) / source_old
                return target_current * (1 + delta_ratio * dep.propagation_factor)

        elif dep.dependency_type == DependencyType.INVERSE:
            source_old = (
                changes[param][0]
                if param in changes
                else current_values.get(param, 0.0)
            )
            if source_old != 0:
                delta_ratio = (source_new - source_old) / source_old
                return target_current * (1 - delta_ratio * dep.propagation_factor)

        return None


# ---------------------------------------------------------------------------
# Default Dependencies & Invariants
# ---------------------------------------------------------------------------

DEFAULT_DEPENDENCIES: list[SettingsDependency] = [
    # backoff_max_ms must be > backoff_base_ms (10% margin)
    SettingsDependency(
        source="backoff_base_ms",
        target="backoff_max_ms",
        dependency_type=DependencyType.REQUIRES_GREATER,
        propagation_factor=0.1,
        description="backoff_max must exceed backoff_base",
    ),
    # timeout_ms should be > backoff_base_ms (10% margin)
    SettingsDependency(
        source="backoff_base_ms",
        target="timeout_ms",
        dependency_type=DependencyType.REQUIRES_GREATER,
        propagation_factor=0.1,
        description="timeout must exceed backoff_base for retries to be meaningful",
    ),
    # throttle_sla_critical_ms must be > throttle_sla_warning_ms (10% margin)
    SettingsDependency(
        source="throttle_sla_warning_ms",
        target="throttle_sla_critical_ms",
        dependency_type=DependencyType.REQUIRES_GREATER,
        propagation_factor=0.1,
        description="critical SLA threshold must exceed warning threshold",
    ),
    # retry_count ↑ → backoff_max_ms ↑ (proportional)
    SettingsDependency(
        source="retry_count",
        target="backoff_max_ms",
        dependency_type=DependencyType.PROPORTIONAL,
        propagation_factor=0.5,
        description="more retries benefit from higher max backoff",
    ),
    # circuit_breaker_threshold ↓ → retry_count ↓ (proportional)
    SettingsDependency(
        source="circuit_breaker_threshold",
        target="retry_count",
        dependency_type=DependencyType.PROPORTIONAL,
        propagation_factor=0.3,
        description="sensitive CB should reduce retry attempts",
    ),
    # rate_limit_rps ↓ → jitter_range ↑ (inverse)
    SettingsDependency(
        source="rate_limit_rps",
        target="jitter_range",
        dependency_type=DependencyType.INVERSE,
        propagation_factor=0.2,
        description="lower RPS limit benefits from more jitter to spread load",
    ),
]


DEFAULT_INVARIANTS: list[SettingsInvariant] = [
    SettingsInvariant(
        name="backoff_ordering",
        parameters=("backoff_base_ms", "backoff_max_ms"),
        check=lambda v: v.get("backoff_max_ms", 60000) > v.get("backoff_base_ms", 10),
        description="backoff_max_ms must be greater than backoff_base_ms",
        fix=lambda v: {
            **v,
            "backoff_max_ms": max(
                v.get("backoff_max_ms", 60000),
                v.get("backoff_base_ms", 10) * 2,
            ),
        },
    ),
    SettingsInvariant(
        name="sla_threshold_ordering",
        parameters=("throttle_sla_warning_ms", "throttle_sla_critical_ms"),
        check=lambda v: (
            v.get("throttle_sla_critical_ms", 5000)
            > v.get("throttle_sla_warning_ms", 50)
        ),
        description="critical SLA must exceed warning SLA",
        fix=lambda v: {
            **v,
            "throttle_sla_critical_ms": max(
                v.get("throttle_sla_critical_ms", 5000),
                v.get("throttle_sla_warning_ms", 50) * 2,
            ),
        },
    ),
    SettingsInvariant(
        name="timeout_backoff_coherence",
        parameters=("timeout_ms", "backoff_base_ms"),
        check=lambda v: v.get("timeout_ms", 30000) > v.get("backoff_base_ms", 10),
        description="timeout must be greater than backoff_base for retries to work",
        fix=lambda v: {
            **v,
            "timeout_ms": max(
                v.get("timeout_ms", 30000),
                v.get("backoff_base_ms", 10) * 3,
            ),
        },
    ),
    SettingsInvariant(
        name="retry_timeout_budget",
        parameters=("retry_count", "backoff_max_ms", "timeout_ms"),
        check=lambda v: (
            v.get("timeout_ms", 30000)
            >= v.get("retry_count", 3) * v.get("backoff_base_ms", 10)
        ),
        description="timeout must accommodate retry count × backoff",
        fix=None,  # Complex — requires manual decision
    ),
]


# ---------------------------------------------------------------------------
# Singleton Factory
# ---------------------------------------------------------------------------


def _create_dependency_graph() -> SettingsDependencyGraph:
    graph = SettingsDependencyGraph()
    for dep in DEFAULT_DEPENDENCIES:
        graph.add_dependency(dep)
    for inv in DEFAULT_INVARIANTS:
        graph.add_invariant(inv)
    graph.freeze()
    return graph


from baldur.utils.singleton import make_singleton_factory

get_dependency_graph, configure_dependency_graph, reset_dependency_graph = (
    make_singleton_factory("dependency_graph", _create_dependency_graph)
)
