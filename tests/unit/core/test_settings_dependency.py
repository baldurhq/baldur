"""Unit tests for baldur.core.settings_dependency module (#372).

Test classification:
    - Contract: DEFAULT_DEPENDENCIES/INVARIANTS counts, DependencyType values, propagation formulas
    - Behavior: Graph CRUD, propagation, invariant check/fix, cycle detection, singleton lifecycle
"""

from __future__ import annotations

import pytest

from baldur.core.exceptions import BaldurError
from baldur.core.settings_dependency import (
    DEFAULT_DEPENDENCIES,
    DEFAULT_INVARIANTS,
    CycleDetectedError,
    DependencyType,
    SettingsDependency,
    SettingsDependencyGraph,
    SettingsInvariant,
    get_dependency_graph,
    reset_dependency_graph,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def graph() -> SettingsDependencyGraph:
    """Fresh unfrozen graph for each test."""
    return SettingsDependencyGraph()


@pytest.fixture
def simple_dep() -> SettingsDependency:
    """Simple REQUIRES_GREATER dependency."""
    return SettingsDependency(
        source="base",
        target="max_val",
        dependency_type=DependencyType.REQUIRES_GREATER,
        propagation_factor=0.1,
        description="max must exceed base",
    )


@pytest.fixture
def simple_invariant() -> SettingsInvariant:
    """Simple invariant: max > base."""
    return SettingsInvariant(
        name="ordering",
        parameters=("base", "max_val"),
        check=lambda v: v["max_val"] > v["base"],
        description="max must be greater than base",
        fix=lambda v: {**v, "max_val": max(v["max_val"], v["base"] * 2)},
    )


# ===========================================================================
# Contract Tests
# ===========================================================================


class TestDefaultRegistrationsContract:
    """Verify design-document specified counts and values for defaults."""

    def test_default_dependencies_count(self):
        """Document specifies exactly 6 default dependencies."""
        assert len(DEFAULT_DEPENDENCIES) == 6

    def test_default_invariants_count(self):
        """Document specifies exactly 4 default invariants."""
        assert len(DEFAULT_INVARIANTS) == 4

    def test_default_invariant_names(self):
        """Document specifies these 4 invariant names."""
        names = {inv.name for inv in DEFAULT_INVARIANTS}
        assert names == {
            "backoff_ordering",
            "sla_threshold_ordering",
            "timeout_backoff_coherence",
            "retry_timeout_budget",
        }

    def test_retry_timeout_budget_has_no_fix(self):
        """retry_timeout_budget is documented as fix=None (requires manual decision)."""
        inv = next(i for i in DEFAULT_INVARIANTS if i.name == "retry_timeout_budget")
        assert inv.fix is None

    def test_backoff_ordering_fix_doubles_base(self):
        """backoff_ordering fix sets max to at least base*2."""
        inv = next(i for i in DEFAULT_INVARIANTS if i.name == "backoff_ordering")
        result = inv.fix({"backoff_base_ms": 1000, "backoff_max_ms": 500})
        assert result["backoff_max_ms"] == 2000

    def test_sla_threshold_ordering_fix_doubles_warning(self):
        """sla_threshold_ordering fix sets critical to at least warning*2."""
        inv = next(i for i in DEFAULT_INVARIANTS if i.name == "sla_threshold_ordering")
        result = inv.fix(
            {"throttle_sla_warning_ms": 100, "throttle_sla_critical_ms": 50}
        )
        assert result["throttle_sla_critical_ms"] == 200

    def test_timeout_backoff_coherence_fix_triples_base(self):
        """timeout_backoff_coherence fix sets timeout to at least base*3."""
        inv = next(
            i for i in DEFAULT_INVARIANTS if i.name == "timeout_backoff_coherence"
        )
        result = inv.fix({"backoff_base_ms": 5000, "timeout_ms": 3000})
        assert result["timeout_ms"] == 15000


class TestDependencyTypeContract:
    """Verify DependencyType enum values per design document."""

    def test_has_five_members(self):
        """Document specifies exactly 5 dependency types."""
        assert len(DependencyType) == 5

    def test_enum_values(self):
        """DependencyType values match document specification."""
        assert DependencyType.REQUIRES_GREATER == "requires_greater"
        assert DependencyType.REQUIRES_LESS == "requires_less"
        assert DependencyType.PROPORTIONAL == "proportional"
        assert DependencyType.INVERSE == "inverse"
        assert DependencyType.CONDITIONAL == "conditional"

    def test_json_serializable_as_str(self):
        """DependencyType inherits str for JSON serialization."""
        assert isinstance(DependencyType.REQUIRES_GREATER, str)


class TestPropagationFormulasContract:
    """Verify document-specified propagation formula examples."""

    def test_proportional_retry_count_3_to_5(self):
        """Document example: retry_count 3->5 (factor=0.5) -> backoff_max 5000->6667."""
        graph = SettingsDependencyGraph()
        graph.add_dependency(
            SettingsDependency(
                source="retry_count",
                target="backoff_max_ms",
                dependency_type=DependencyType.PROPORTIONAL,
                propagation_factor=0.5,
            )
        )
        graph.freeze()

        suggested = graph.propagate(
            changes={"retry_count": (3.0, 5.0)},
            current_values={"retry_count": 3.0, "backoff_max_ms": 5000.0},
        )

        assert suggested["backoff_max_ms"] == pytest.approx(6666.67, rel=1e-3)


# ===========================================================================
# Behavior Tests
# ===========================================================================


class TestCycleDetectedErrorBehavior:
    """Verify CycleDetectedError exception behavior."""

    def test_inherits_baldur_error(self):
        """CycleDetectedError is a BaldurError subclass."""
        err = CycleDetectedError(["a", "b", "a"])
        assert isinstance(err, BaldurError)

    def test_message_format(self):
        """Error message joins cycle nodes with arrow separator."""
        err = CycleDetectedError(["a", "b", "a"])
        assert str(err) == "Cycle detected: a \u2192 b \u2192 a"

    def test_extra_context_without_trigger(self):
        """extra_context returns cycle only when trigger is None."""
        err = CycleDetectedError(["x", "y", "x"])
        ctx = err.extra_context()
        assert ctx == {"cycle": ["x", "y", "x"]}
        assert "trigger_source" not in ctx

    def test_extra_context_with_trigger(self):
        """extra_context includes trigger source/target when provided."""
        dep = SettingsDependency(
            source="a", target="b", dependency_type=DependencyType.REQUIRES_GREATER
        )
        err = CycleDetectedError(["a", "b", "a"], trigger=dep)
        ctx = err.extra_context()
        assert ctx["trigger_source"] == "a"
        assert ctx["trigger_target"] == "b"
        assert ctx["cycle"] == ["a", "b", "a"]


class TestGraphRegistrationBehavior:
    """Verify add_dependency/add_invariant registration behavior."""

    def test_add_dependency_registers_in_adjacency(self, graph, simple_dep):
        """add_dependency populates adjacency list."""
        graph.add_dependency(simple_dep)
        assert len(graph.get_dependents("base")) == 1
        assert graph.get_dependents("base")[0].target == "max_val"

    def test_add_dependency_registers_in_reverse(self, graph, simple_dep):
        """add_dependency populates reverse adjacency."""
        graph.add_dependency(simple_dep)
        assert len(graph.get_dependencies("max_val")) == 1
        assert graph.get_dependencies("max_val")[0].source == "base"

    def test_add_invariant_registered(self, graph, simple_invariant):
        """add_invariant makes invariant available for check."""
        graph.add_invariant(simple_invariant)
        results = graph.check_invariants({"base": 10, "max_val": 20})
        assert len(results) == 1
        assert results[0][1] is True

    def test_get_dependents_unknown_param_returns_empty(self, graph):
        """get_dependents returns empty list for unknown parameter."""
        assert graph.get_dependents("nonexistent") == []

    def test_get_dependencies_unknown_param_returns_empty(self, graph):
        """get_dependencies returns empty list for unknown parameter."""
        assert graph.get_dependencies("nonexistent") == []


class TestFreezeStateBehavior:
    """Verify freeze pattern state transitions."""

    def test_new_graph_is_not_frozen(self, graph):
        """Newly created graph is unfrozen."""
        assert graph.is_frozen is False

    def test_freeze_sets_frozen_flag(self, graph):
        """freeze() transitions to frozen state."""
        graph.freeze()
        assert graph.is_frozen is True

    def test_add_dependency_after_freeze_raises_runtime_error(self, graph, simple_dep):
        """Adding dependency after freeze raises RuntimeError."""
        graph.freeze()
        with pytest.raises(RuntimeError, match="frozen"):
            graph.add_dependency(simple_dep)

    def test_add_invariant_after_freeze_raises_runtime_error(
        self, graph, simple_invariant
    ):
        """Adding invariant after freeze raises RuntimeError."""
        graph.freeze()
        with pytest.raises(RuntimeError, match="frozen"):
            graph.add_invariant(simple_invariant)


class TestCycleDetectionBehavior:
    """Verify cycle detection during add_dependency and detect_cycles."""

    def test_direct_cycle_raises_error(self, graph):
        """A->B, B->A creates a direct cycle."""
        graph.add_dependency(
            SettingsDependency(
                source="a", target="b", dependency_type=DependencyType.REQUIRES_GREATER
            )
        )
        with pytest.raises(CycleDetectedError) as exc_info:
            graph.add_dependency(
                SettingsDependency(
                    source="b",
                    target="a",
                    dependency_type=DependencyType.REQUIRES_GREATER,
                )
            )
        assert "a" in exc_info.value.cycle
        assert "b" in exc_info.value.cycle

    def test_indirect_cycle_raises_error(self, graph):
        """A->B, B->C, C->A creates an indirect cycle."""
        graph.add_dependency(
            SettingsDependency(
                source="a", target="b", dependency_type=DependencyType.PROPORTIONAL
            )
        )
        graph.add_dependency(
            SettingsDependency(
                source="b", target="c", dependency_type=DependencyType.PROPORTIONAL
            )
        )
        with pytest.raises(CycleDetectedError):
            graph.add_dependency(
                SettingsDependency(
                    source="c",
                    target="a",
                    dependency_type=DependencyType.PROPORTIONAL,
                )
            )

    def test_self_loop_raises_error(self, graph):
        """A->A is a self-loop cycle."""
        with pytest.raises(CycleDetectedError):
            graph.add_dependency(
                SettingsDependency(
                    source="a",
                    target="a",
                    dependency_type=DependencyType.REQUIRES_GREATER,
                )
            )

    def test_detect_cycles_returns_empty_for_dag(self, graph, simple_dep):
        """detect_cycles returns empty list when graph is acyclic."""
        graph.add_dependency(simple_dep)
        assert graph.detect_cycles() == []

    def test_non_cycle_path_allowed(self, graph):
        """A->B, A->C, B->C is valid (diamond, no cycle)."""
        graph.add_dependency(
            SettingsDependency(
                source="a", target="b", dependency_type=DependencyType.PROPORTIONAL
            )
        )
        graph.add_dependency(
            SettingsDependency(
                source="a", target="c", dependency_type=DependencyType.PROPORTIONAL
            )
        )
        graph.add_dependency(
            SettingsDependency(
                source="b", target="c", dependency_type=DependencyType.PROPORTIONAL
            )
        )
        assert graph.detect_cycles() == []


class TestGetAllAffectedBehavior:
    """Verify BFS traversal in get_all_affected."""

    def test_returns_direct_dependents_at_depth_1(self, graph, simple_dep):
        """Direct dependents are at depth 1."""
        graph.add_dependency(simple_dep)
        affected = graph.get_all_affected("base")
        assert ("max_val", 1) in affected

    def test_respects_max_depth(self, graph):
        """Traversal stops at max_depth."""
        # a -> b -> c -> d
        for src, tgt in [("a", "b"), ("b", "c"), ("c", "d")]:
            graph.add_dependency(
                SettingsDependency(
                    source=src,
                    target=tgt,
                    dependency_type=DependencyType.PROPORTIONAL,
                )
            )

        affected = graph.get_all_affected("a", max_depth=2)
        targets = {t for t, _ in affected}
        assert "b" in targets
        assert "c" in targets
        assert "d" not in targets

    def test_empty_for_leaf_node(self, graph, simple_dep):
        """Leaf node has no affected parameters."""
        graph.add_dependency(simple_dep)
        assert graph.get_all_affected("max_val") == []


class TestPropagationBehavior:
    """Verify propagate() BFS cascade for all DependencyType variants."""

    def test_requires_greater_adjusts_when_violated(self, graph):
        """REQUIRES_GREATER: target adjusted to source*(1+factor) when target <= source."""
        graph.add_dependency(
            SettingsDependency(
                source="base",
                target="max_val",
                dependency_type=DependencyType.REQUIRES_GREATER,
                propagation_factor=0.1,
            )
        )
        graph.freeze()

        # base increases to 6000, but max_val is only 5000 — violates ordering
        suggested = graph.propagate(
            changes={"base": (1000.0, 6000.0)},
            current_values={"base": 1000.0, "max_val": 5000.0},
        )
        assert suggested["max_val"] == pytest.approx(6600.0)  # 6000 * 1.1

    def test_requires_greater_no_adjustment_when_satisfied(self, graph):
        """REQUIRES_GREATER: no adjustment when target already exceeds source."""
        graph.add_dependency(
            SettingsDependency(
                source="base",
                target="max_val",
                dependency_type=DependencyType.REQUIRES_GREATER,
                propagation_factor=0.1,
            )
        )
        graph.freeze()

        suggested = graph.propagate(
            changes={"base": (100.0, 200.0)},
            current_values={"base": 100.0, "max_val": 50000.0},
        )
        assert "max_val" not in suggested

    def test_requires_less_adjusts_when_violated(self, graph):
        """REQUIRES_LESS: target adjusted to source*(1-factor) when target >= source."""
        graph.add_dependency(
            SettingsDependency(
                source="max_val",
                target="base",
                dependency_type=DependencyType.REQUIRES_LESS,
                propagation_factor=0.1,
            )
        )
        graph.freeze()

        suggested = graph.propagate(
            changes={"max_val": (10000.0, 500.0)},
            current_values={"max_val": 10000.0, "base": 1000.0},
        )
        assert suggested["base"] == pytest.approx(450.0)  # 500 * 0.9

    def test_proportional_propagation(self, graph):
        """PROPORTIONAL: target changes proportionally to source delta."""
        graph.add_dependency(
            SettingsDependency(
                source="retry_count",
                target="backoff_max",
                dependency_type=DependencyType.PROPORTIONAL,
                propagation_factor=0.5,
            )
        )
        graph.freeze()

        # retry_count doubles (2 -> 4), delta_ratio = 1.0
        # backoff_max = 1000 * (1 + 1.0 * 0.5) = 1500
        suggested = graph.propagate(
            changes={"retry_count": (2.0, 4.0)},
            current_values={"retry_count": 2.0, "backoff_max": 1000.0},
        )
        assert suggested["backoff_max"] == pytest.approx(1500.0)

    def test_inverse_propagation(self, graph):
        """INVERSE: target changes inversely to source delta."""
        graph.add_dependency(
            SettingsDependency(
                source="rate_limit",
                target="jitter",
                dependency_type=DependencyType.INVERSE,
                propagation_factor=0.2,
            )
        )
        graph.freeze()

        # rate_limit halved (100 -> 50), delta_ratio = -0.5
        # jitter = 10 * (1 - (-0.5) * 0.2) = 10 * 1.1 = 11
        suggested = graph.propagate(
            changes={"rate_limit": (100.0, 50.0)},
            current_values={"rate_limit": 100.0, "jitter": 10.0},
        )
        assert suggested["jitter"] == pytest.approx(11.0)

    def test_conditional_propagation_calls_fn(self, graph):
        """CONDITIONAL: invokes condition_fn and uses its result."""
        called_with: list[tuple] = []

        def custom_fn(
            proposed: dict[str, float], current: dict[str, float]
        ) -> dict[str, float]:
            called_with.append((proposed, current))
            return {"derived": proposed.get("src", 0) * 2}

        graph.add_dependency(
            SettingsDependency(
                source="src",
                target="derived",
                dependency_type=DependencyType.CONDITIONAL,
                condition_fn=custom_fn,
            )
        )
        graph.freeze()

        suggested = graph.propagate(
            changes={"src": (1.0, 5.0)},
            current_values={"src": 1.0, "derived": 2.0},
        )
        assert suggested["derived"] == 10.0
        assert len(called_with) == 1

    def test_propagation_does_not_override_explicit_changes(self, graph):
        """Propagation skips parameters that are explicitly changed."""
        graph.add_dependency(
            SettingsDependency(
                source="a",
                target="b",
                dependency_type=DependencyType.REQUIRES_GREATER,
                propagation_factor=0.1,
            )
        )
        graph.freeze()

        suggested = graph.propagate(
            changes={"a": (100.0, 200.0), "b": (150.0, 180.0)},
            current_values={"a": 100.0, "b": 150.0},
        )
        assert "b" not in suggested

    def test_proportional_division_by_zero_safe(self, graph):
        """PROPORTIONAL: source_old=0 produces no propagation (no division error)."""
        graph.add_dependency(
            SettingsDependency(
                source="x",
                target="y",
                dependency_type=DependencyType.PROPORTIONAL,
                propagation_factor=0.5,
            )
        )
        graph.freeze()

        suggested = graph.propagate(
            changes={"x": (0.0, 5.0)},
            current_values={"x": 0.0, "y": 100.0},
        )
        assert "y" not in suggested

    def test_max_depth_limits_cascade(self, graph):
        """Cascade stops at max_depth."""
        # chain: a -> b -> c -> d
        for src, tgt in [("a", "b"), ("b", "c"), ("c", "d")]:
            graph.add_dependency(
                SettingsDependency(
                    source=src,
                    target=tgt,
                    dependency_type=DependencyType.REQUIRES_GREATER,
                    propagation_factor=0.1,
                )
            )
        graph.freeze()

        suggested = graph.propagate(
            changes={"a": (100.0, 5000.0)},
            current_values={"a": 100.0, "b": 200.0, "c": 300.0, "d": 400.0},
            max_depth=1,
        )
        assert "b" in suggested
        assert "c" not in suggested
        assert "d" not in suggested


class TestInvariantCheckBehavior:
    """Verify check_invariants and fix_invariant_violations behavior."""

    def test_check_invariants_passing(self, graph, simple_invariant):
        """Passing invariant returns (invariant, True)."""
        graph.add_invariant(simple_invariant)
        results = graph.check_invariants({"base": 5, "max_val": 10})
        assert results[0][1] is True

    def test_check_invariants_violated(self, graph, simple_invariant):
        """Violated invariant returns (invariant, False)."""
        graph.add_invariant(simple_invariant)
        results = graph.check_invariants({"base": 10, "max_val": 5})
        assert results[0][1] is False

    def test_check_invariants_missing_params_skipped(self, graph, simple_invariant):
        """Invariant with missing parameters is skipped (treated as True)."""
        graph.add_invariant(simple_invariant)
        results = graph.check_invariants({"base": 10})
        assert results[0][1] is True

    def test_fix_invariant_violations_applies_fix(self, graph, simple_invariant):
        """fix_invariant_violations applies fix function when available."""
        graph.add_invariant(simple_invariant)
        fixed, descriptions = graph.fix_invariant_violations(
            {"base": 100, "max_val": 50}
        )
        assert fixed["max_val"] >= fixed["base"]
        assert len(descriptions) == 1
        assert "Fixed:" in descriptions[0]

    def test_fix_invariant_violations_no_fix_no_change(self, graph):
        """Invariant without fix function leaves values unchanged."""
        inv = SettingsInvariant(
            name="no_fix",
            parameters=("a", "b"),
            check=lambda v: v["a"] < v["b"],
            fix=None,
        )
        graph.add_invariant(inv)
        original = {"a": 10, "b": 5}
        fixed, descriptions = graph.fix_invariant_violations(dict(original))
        assert fixed == original
        assert descriptions == []

    def test_fix_preserves_unrelated_keys(self, graph, simple_invariant):
        """Fix function does not remove keys not in invariant."""
        graph.add_invariant(simple_invariant)
        fixed, _ = graph.fix_invariant_violations(
            {"base": 100, "max_val": 50, "other": 999}
        )
        assert fixed["other"] == 999


class TestDataImmutabilityBehavior:
    """Verify input data is not mutated."""

    def test_propagate_does_not_mutate_current_values(self, graph, simple_dep):
        """propagate() does not mutate the current_values dict."""
        graph.add_dependency(simple_dep)
        graph.freeze()

        current = {"base": 100.0, "max_val": 50.0}
        original = dict(current)
        graph.propagate(changes={"base": (100.0, 6000.0)}, current_values=current)
        assert current == original

    def test_check_invariants_does_not_mutate_values(self, graph, simple_invariant):
        """check_invariants() does not mutate the values dict."""
        graph.add_invariant(simple_invariant)
        values = {"base": 100, "max_val": 5}
        original = dict(values)
        graph.check_invariants(values)
        assert values == original

    def test_fix_invariant_violations_does_not_mutate_input(
        self, graph, simple_invariant
    ):
        """fix_invariant_violations() does not mutate the input dict."""
        graph.add_invariant(simple_invariant)
        values = {"base": 100, "max_val": 5}
        original = dict(values)
        graph.fix_invariant_violations(values)
        assert values == original


class TestSingletonLifecycleBehavior:
    """Verify get_dependency_graph/reset_dependency_graph singleton pattern."""

    def setup_method(self):
        """Reset singleton before each test."""
        reset_dependency_graph()

    def teardown_method(self):
        """Reset singleton after each test."""
        reset_dependency_graph()

    def test_get_returns_frozen_graph(self):
        """Singleton graph is frozen after initialization."""
        graph = get_dependency_graph()
        assert graph.is_frozen is True

    def test_get_returns_same_instance(self):
        """get_dependency_graph returns the same object on repeated calls."""
        g1 = get_dependency_graph()
        g2 = get_dependency_graph()
        assert g1 is g2

    def test_reset_clears_singleton(self):
        """reset_dependency_graph causes next get to create new instance."""
        g1 = get_dependency_graph()
        reset_dependency_graph()
        g2 = get_dependency_graph()
        assert g1 is not g2

    def test_singleton_has_default_dependencies(self):
        """Singleton graph contains all DEFAULT_DEPENDENCIES."""
        graph = get_dependency_graph()
        dependents = graph.get_dependents("backoff_base_ms")
        targets = {d.target for d in dependents}
        assert "backoff_max_ms" in targets
        assert "timeout_ms" in targets

    def test_singleton_has_default_invariants(self):
        """Singleton graph checks all DEFAULT_INVARIANTS."""
        graph = get_dependency_graph()
        results = graph.check_invariants(
            {
                "backoff_base_ms": 10,
                "backoff_max_ms": 60000,
                "throttle_sla_warning_ms": 50,
                "throttle_sla_critical_ms": 5000,
                "timeout_ms": 30000,
                "retry_count": 3,
            }
        )
        assert len(results) == len(DEFAULT_INVARIANTS)
        assert all(passed for _, passed in results)


# ===========================================================================
# Diamond Conflict Resolution Tests (level-order BFS + _merge_proposals)
# ===========================================================================


class TestMergeProposalsDiamondContract:
    """Verify document §2.6 diamond merge example with hardcoded values."""

    def test_backoff_max_diamond_merge_matches_document_example(self):
        """Document §2.6: floor=6600, preference=7500, result=7500."""
        result = SettingsDependencyGraph._merge_proposals(
            [
                (6600.0, DependencyType.REQUIRES_GREATER),  # floor
                (7500.0, DependencyType.PROPORTIONAL),  # preference
            ]
        )
        assert result == pytest.approx(7500.0)


class TestMergeProposalsBehavior:
    """Verify _merge_proposals type-aware merge logic."""

    def test_single_proposal_returns_value_directly(self):
        """Single proposal is returned without merge logic."""
        result = SettingsDependencyGraph._merge_proposals(
            [(42.0, DependencyType.PROPORTIONAL)]
        )
        assert result == 42.0

    def test_multiple_requires_greater_takes_max(self):
        """Multiple REQUIRES_GREATER proposals: take max (most restrictive floor)."""
        result = SettingsDependencyGraph._merge_proposals(
            [
                (100.0, DependencyType.REQUIRES_GREATER),
                (200.0, DependencyType.REQUIRES_GREATER),
                (150.0, DependencyType.REQUIRES_GREATER),
            ]
        )
        assert result == 200.0

    def test_multiple_requires_less_takes_min(self):
        """Multiple REQUIRES_LESS proposals: take min (most restrictive ceiling)."""
        result = SettingsDependencyGraph._merge_proposals(
            [
                (500.0, DependencyType.REQUIRES_LESS),
                (300.0, DependencyType.REQUIRES_LESS),
                (400.0, DependencyType.REQUIRES_LESS),
            ]
        )
        assert result == 300.0

    def test_multiple_proportional_takes_max(self):
        """Multiple PROPORTIONAL proposals: take max."""
        result = SettingsDependencyGraph._merge_proposals(
            [
                (1000.0, DependencyType.PROPORTIONAL),
                (1500.0, DependencyType.PROPORTIONAL),
            ]
        )
        assert result == 1500.0

    def test_preference_clamped_to_floor(self):
        """Preference below floor is raised to floor."""
        result = SettingsDependencyGraph._merge_proposals(
            [
                (500.0, DependencyType.REQUIRES_GREATER),  # floor
                (300.0, DependencyType.PROPORTIONAL),  # preference < floor
            ]
        )
        assert result == 500.0

    def test_preference_clamped_to_ceiling(self):
        """Preference above ceiling is lowered to ceiling."""
        result = SettingsDependencyGraph._merge_proposals(
            [
                (100.0, DependencyType.REQUIRES_LESS),  # ceiling
                (200.0, DependencyType.PROPORTIONAL),  # preference > ceiling
            ]
        )
        assert result == 100.0

    def test_floor_and_ceiling_with_preference_in_range(self):
        """Preference within floor-ceiling range is kept."""
        result = SettingsDependencyGraph._merge_proposals(
            [
                (100.0, DependencyType.REQUIRES_GREATER),  # floor
                (500.0, DependencyType.REQUIRES_LESS),  # ceiling
                (300.0, DependencyType.PROPORTIONAL),  # preference in range
            ]
        )
        assert result == 300.0

    def test_floor_exceeds_ceiling_ceiling_clamps(self):
        """When floor > ceiling (contradictory constraints), ceiling clamp wins."""
        # floor=500, ceiling=200 → result=floor(500) → min(500, 200) = 200
        result = SettingsDependencyGraph._merge_proposals(
            [
                (500.0, DependencyType.REQUIRES_GREATER),
                (200.0, DependencyType.REQUIRES_LESS),
            ]
        )
        assert result == 200.0

    def test_inverse_treated_as_preference(self):
        """INVERSE proposals are treated as preferences (same as PROPORTIONAL)."""
        result = SettingsDependencyGraph._merge_proposals(
            [
                (50.0, DependencyType.INVERSE),
                (80.0, DependencyType.PROPORTIONAL),
            ]
        )
        assert result == 80.0  # max of preferences


class TestDiamondPropagationDeterminismBehavior:
    """Verify propagate() produces identical results regardless of dict key order."""

    @pytest.fixture
    def diamond_graph(self) -> SettingsDependencyGraph:
        """Graph with diamond: two sources target the same parameter."""
        g = SettingsDependencyGraph()
        g.add_dependency(
            SettingsDependency(
                source="base",
                target="target_param",
                dependency_type=DependencyType.REQUIRES_GREATER,
                propagation_factor=0.1,
            )
        )
        g.add_dependency(
            SettingsDependency(
                source="count",
                target="target_param",
                dependency_type=DependencyType.PROPORTIONAL,
                propagation_factor=0.5,
            )
        )
        g.freeze()
        return g

    def test_diamond_result_independent_of_dict_key_order(self, diamond_graph):
        """Same changes with reversed dict order produce identical result."""
        current = {"base": 100.0, "count": 3.0, "target_param": 5000.0}

        # Order A: base first
        changes_a = {"base": (100.0, 6000.0), "count": (3.0, 6.0)}
        result_a = diamond_graph.propagate(changes=changes_a, current_values=current)

        # Order B: count first (reversed insertion order)
        changes_b = {"count": (3.0, 6.0), "base": (100.0, 6000.0)}
        result_b = diamond_graph.propagate(changes=changes_b, current_values=current)

        assert result_a["target_param"] == pytest.approx(result_b["target_param"])

    def test_diamond_merge_applies_floor_and_preference(self, diamond_graph):
        """Diamond merge: REQUIRES_GREATER floor + PROPORTIONAL preference."""
        # Given
        current = {"base": 100.0, "count": 3.0, "target_param": 5000.0}
        changes = {"base": (100.0, 6000.0), "count": (3.0, 6.0)}

        # When
        suggested = diamond_graph.propagate(changes=changes, current_values=current)

        # Then — REQUIRES_GREATER: 6000*1.1=6600 (floor)
        #         PROPORTIONAL: 5000*(1+1.0*0.5)=7500 (preference)
        #         merge: max(7500, 6600) = 7500
        assert suggested["target_param"] == pytest.approx(7500.0)

    def test_diamond_with_default_dependencies_is_deterministic(self):
        """Real DEFAULT_DEPENDENCIES diamond is order-independent."""
        # Given — both backoff_base_ms and retry_count affect backoff_max_ms
        graph = SettingsDependencyGraph()
        for dep in DEFAULT_DEPENDENCIES:
            graph.add_dependency(dep)
        graph.freeze()

        current = {
            "backoff_base_ms": 100.0,
            "backoff_max_ms": 5000.0,
            "timeout_ms": 30000.0,
            "retry_count": 3.0,
            "circuit_breaker_threshold": 0.5,
            "rate_limit_rps": 1000.0,
            "jitter_range": 0.1,
            "throttle_sla_warning_ms": 100.0,
            "throttle_sla_critical_ms": 5000.0,
        }

        changes_a = {"backoff_base_ms": (100.0, 4000.0), "retry_count": (3.0, 6.0)}
        changes_b = {"retry_count": (3.0, 6.0), "backoff_base_ms": (100.0, 4000.0)}

        result_a = graph.propagate(changes=changes_a, current_values=current)
        result_b = graph.propagate(changes=changes_b, current_values=current)

        assert result_a["backoff_max_ms"] == pytest.approx(result_b["backoff_max_ms"])


class TestLevelOrderProcessingBehavior:
    """Verify level-order BFS processes same-depth sources against stable snapshot."""

    def test_processed_prevents_source_reuse_while_allowing_target_update(self):
        """Processed param is not reused as source but can be re-targeted by cascade."""
        # Given — a→b, a→c, b→c: c gets proposed at depth 0 and re-targeted at depth 1
        g = SettingsDependencyGraph()
        g.add_dependency(
            SettingsDependency(
                source="a",
                target="b",
                dependency_type=DependencyType.REQUIRES_GREATER,
                propagation_factor=0.1,
            )
        )
        g.add_dependency(
            SettingsDependency(
                source="a",
                target="c",
                dependency_type=DependencyType.REQUIRES_GREATER,
                propagation_factor=0.1,
            )
        )
        g.add_dependency(
            SettingsDependency(
                source="b",
                target="c",
                dependency_type=DependencyType.REQUIRES_GREATER,
                propagation_factor=0.1,
            )
        )
        g.freeze()

        # When
        suggested = g.propagate(
            changes={"a": (100.0, 5000.0)},
            current_values={"a": 100.0, "b": 200.0, "c": 300.0},
        )

        # Then — depth 0: b=5500, c=5500 (both from a)
        #         depth 1: b→c sees c(5500)<=b(5500) → c = 5500*1.1 = 6050
        #         c is NOT re-enqueued as source (already processed)
        assert suggested["b"] == pytest.approx(5500.0)
        assert suggested["c"] == pytest.approx(6050.0)

    def test_level_order_all_depth0_sources_see_same_effective(self):
        """All depth-0 sources compute against the same effective snapshot."""
        # Given — two sources that both read target_param's current value
        g = SettingsDependencyGraph()
        g.add_dependency(
            SettingsDependency(
                source="src_a",
                target="target_param",
                dependency_type=DependencyType.PROPORTIONAL,
                propagation_factor=0.5,
            )
        )
        g.add_dependency(
            SettingsDependency(
                source="src_b",
                target="target_param",
                dependency_type=DependencyType.PROPORTIONAL,
                propagation_factor=0.3,
            )
        )
        g.freeze()

        current = {"src_a": 10.0, "src_b": 10.0, "target_param": 1000.0}
        changes = {"src_a": (10.0, 20.0), "src_b": (10.0, 20.0)}

        # When
        suggested = g.propagate(changes=changes, current_values=current)

        # Then — both see target_param=1000 (the original, not intermediate)
        # src_a: 1000 * (1 + 1.0 * 0.5) = 1500
        # src_b: 1000 * (1 + 1.0 * 0.3) = 1300
        # merge: max(1500, 1300) = 1500
        assert suggested["target_param"] == pytest.approx(1500.0)

    def test_cascade_uses_merged_value_from_previous_depth(self):
        """Depth N+1 uses the merged result from depth N, not an intermediate."""
        # Given — chain: a → b → c
        g = SettingsDependencyGraph()
        g.add_dependency(
            SettingsDependency(
                source="a",
                target="b",
                dependency_type=DependencyType.REQUIRES_GREATER,
                propagation_factor=0.1,
            )
        )
        g.add_dependency(
            SettingsDependency(
                source="b",
                target="c",
                dependency_type=DependencyType.REQUIRES_GREATER,
                propagation_factor=0.1,
            )
        )
        g.freeze()

        # When — a increases to 5000, b (200) < 5000 → b proposed
        suggested = g.propagate(
            changes={"a": (100.0, 5000.0)},
            current_values={"a": 100.0, "b": 200.0, "c": 300.0},
        )

        # Then — b = 5000 * 1.1 = 5500 (depth 1)
        #         c should use effective[b]=5500 (merged from depth 1)
        #         c_current (300) <= 5500 → c = 5500 * 1.1 = 6050 (depth 2)
        assert suggested["b"] == pytest.approx(5500.0)
        assert suggested["c"] == pytest.approx(6050.0)
