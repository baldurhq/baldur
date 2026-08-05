"""Unit tests for the config-invalidation target registry and the runtime-apply
declaration derived from it (744 D18/D11).

Targets:
  - ``register_config_invalidation_target`` / ``get_config_invalidation_targets``
    / ``registered_config_invalidation_types`` — the per-process registry that
    records which domains have a consumer able to refresh itself.
  - ``invoke_config_invalidation_targets`` — fan-out with per-target failure
    isolation.
  - ``set_config_delivery_armed`` / ``config_delivery_armed`` /
    ``config_delivery_convergence_seconds`` — the second, orthogonal axis:
    whether the mechanism that *calls* those targets is running here.
  - ``describe_config_runtime_apply`` — the projection of those two axes that
    every operator-readable configuration response carries. It is derived, never
    authored per domain, which is what stops it drifting from the wiring.

Verification techniques (§8):
  - Idempotency — a starter that runs twice registers once
  - State transition — the three (registered, armed) combinations map to the
    three modes, and only those
  - Exception/edge — a raising target does not suppress the ones after it
  - Serialization — ``to_dict()`` renders the three response keys
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from baldur.core.config_invalidation import (
    RuntimeApplyDeclaration,
    RuntimeApplyMode,
    config_delivery_armed,
    config_delivery_convergence_seconds,
    describe_config_runtime_apply,
    get_config_invalidation_targets,
    invoke_config_invalidation_targets,
    register_config_invalidation_target,
    registered_config_invalidation_types,
    reset_config_invalidation_targets,
    set_config_delivery_armed,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """The registry is process-global; isolate every test from its neighbours."""
    reset_config_invalidation_targets()
    yield
    reset_config_invalidation_targets()


# =============================================================================
# Registry (Behavior)
# =============================================================================


class TestConfigInvalidationRegistryBehavior:
    """Registration, membership dedup, read isolation and fan-out."""

    def test_registered_target_is_returned_for_its_config_type(self):
        def refresh():
            return None

        register_config_invalidation_target("circuit_breaker", refresh)

        assert get_config_invalidation_targets("circuit_breaker") == [refresh]

    def test_unregistered_config_type_returns_empty_list(self):
        register_config_invalidation_target("circuit_breaker", lambda: None)

        assert get_config_invalidation_targets("retry") == []

    def test_same_target_registered_twice_is_stored_once(self):
        """A starter that runs twice (framework init + a post-fork hook) must
        not make one invalidation call the same refresh twice."""

        def refresh():
            return None

        register_config_invalidation_target("circuit_breaker", refresh)
        register_config_invalidation_target("circuit_breaker", refresh)

        assert get_config_invalidation_targets("circuit_breaker") == [refresh]

    def test_distinct_targets_for_one_config_type_are_both_kept(self):
        def first():
            return None

        def second():
            return None

        register_config_invalidation_target("circuit_breaker", first)
        register_config_invalidation_target("circuit_breaker", second)

        assert get_config_invalidation_targets("circuit_breaker") == [first, second]

    def test_returned_target_list_is_a_copy(self):
        """A caller mutating the returned list must not de-register anything."""

        def refresh():
            return None

        register_config_invalidation_target("circuit_breaker", refresh)

        got = get_config_invalidation_targets("circuit_breaker")
        got.clear()

        assert get_config_invalidation_targets("circuit_breaker") == [refresh]

    def test_registered_types_reports_every_registered_config_type(self):
        register_config_invalidation_target("circuit_breaker", lambda: None)
        register_config_invalidation_target("retry", lambda: None)

        assert registered_config_invalidation_types() == {"circuit_breaker", "retry"}

    def test_registered_types_is_empty_when_nothing_is_registered(self):
        assert registered_config_invalidation_types() == set()

    def test_registered_types_is_order_independent(self):
        """Registration order is an accident of import order; the reported set
        must not depend on it."""

        def a():
            return None

        def b():
            return None

        register_config_invalidation_target("retry", a)
        register_config_invalidation_target("circuit_breaker", b)
        first_order = registered_config_invalidation_types()

        reset_config_invalidation_targets()
        register_config_invalidation_target("circuit_breaker", b)
        register_config_invalidation_target("retry", a)

        assert registered_config_invalidation_types() == first_order

    def test_reset_drops_targets_and_armed_markers(self):
        register_config_invalidation_target("circuit_breaker", lambda: None)
        set_config_delivery_armed("circuit_breaker", True)

        reset_config_invalidation_targets()

        assert get_config_invalidation_targets("circuit_breaker") == []
        assert config_delivery_armed("circuit_breaker") is False

    def test_invoke_runs_every_registered_target_and_counts_them(self):
        calls: list[str] = []
        register_config_invalidation_target(
            "circuit_breaker", lambda: calls.append("first")
        )
        register_config_invalidation_target(
            "circuit_breaker", lambda: calls.append("second")
        )

        succeeded = invoke_config_invalidation_targets("circuit_breaker")

        assert calls == ["first", "second"]
        assert succeeded == 2

    def test_invoke_isolates_a_raising_target_from_the_rest(self):
        """A domain whose refresh blows up must not silence the domains
        registered after it, and must not propagate to the dispatcher."""
        # Given — three targets, the middle one raising
        calls: list[str] = []

        def first():
            calls.append("first")

        def middle():
            calls.append("middle")
            raise RuntimeError("refresh failed")

        def last():
            calls.append("last")

        for target in (first, middle, last):
            register_config_invalidation_target("circuit_breaker", target)

        # When
        succeeded = invoke_config_invalidation_targets("circuit_breaker")

        # Then — all three ran, two of them successfully
        assert calls == ["first", "middle", "last"]
        assert succeeded == 2

    def test_invoke_on_an_unregistered_config_type_reports_zero(self):
        """``0`` is the caller's signal that nothing ran — the same answer an
        all-failed fan-out gives, deliberately."""
        assert invoke_config_invalidation_targets("retry") == 0


# =============================================================================
# Delivery-armed marker (Behavior)
# =============================================================================


class TestConfigDeliveryArmedBehavior:
    """The armed marker is the second axis: registration says a domain *can* be
    refreshed here, armed says something is running that will do it."""

    def test_delivery_is_unarmed_by_default(self):
        register_config_invalidation_target("circuit_breaker", lambda: None)

        assert config_delivery_armed("circuit_breaker") is False
        assert config_delivery_convergence_seconds("circuit_breaker") is None

    def test_arming_a_config_type_is_reported_back(self):
        set_config_delivery_armed("circuit_breaker", True)

        assert config_delivery_armed("circuit_breaker") is True

    def test_arming_records_the_declared_convergence_bound(self):
        set_config_delivery_armed("circuit_breaker", True, converges_within_seconds=30)

        assert config_delivery_convergence_seconds("circuit_breaker") == 30

    def test_arming_without_a_bound_reports_none_rather_than_inventing_one(self):
        set_config_delivery_armed("circuit_breaker", True)

        assert config_delivery_armed("circuit_breaker") is True
        assert config_delivery_convergence_seconds("circuit_breaker") is None

    def test_disarming_clears_the_marker_and_its_bound(self):
        set_config_delivery_armed("circuit_breaker", True, converges_within_seconds=30)

        set_config_delivery_armed("circuit_breaker", False)

        assert config_delivery_armed("circuit_breaker") is False
        assert config_delivery_convergence_seconds("circuit_breaker") is None

    def test_arming_one_config_type_leaves_the_others_unarmed(self):
        set_config_delivery_armed("circuit_breaker", True)

        assert config_delivery_armed("retry") is False


# =============================================================================
# Runtime-apply derivation (Behavior)
# =============================================================================


class TestRuntimeApplyDerivationBehavior:
    """The declaration is a projection of (registered, armed) — never a literal
    authored per domain."""

    def test_no_registered_target_reports_unverified(self):
        """The shipped default: 744 registers nothing, so every domain says so."""
        declaration = describe_config_runtime_apply("circuit_breaker")

        assert declaration.mode is RuntimeApplyMode.UNVERIFIED
        assert declaration.converges_within_seconds is None

    def test_registered_but_unarmed_reports_stored_only(self):
        register_config_invalidation_target("circuit_breaker", lambda: None)

        declaration = describe_config_runtime_apply("circuit_breaker")

        assert declaration.mode is RuntimeApplyMode.STORED_ONLY
        assert declaration.converges_within_seconds is None

    def test_registered_and_armed_reports_live(self):
        register_config_invalidation_target("circuit_breaker", lambda: None)
        set_config_delivery_armed("circuit_breaker", True)

        declaration = describe_config_runtime_apply("circuit_breaker")

        assert declaration.mode is RuntimeApplyMode.LIVE

    def test_live_carries_the_convergence_bound_the_delivery_declared(self):
        register_config_invalidation_target("circuit_breaker", lambda: None)
        set_config_delivery_armed("circuit_breaker", True, converges_within_seconds=30)

        assert (
            describe_config_runtime_apply("circuit_breaker").converges_within_seconds
            == 30
        )

    def test_armed_without_a_registered_target_still_reports_unverified(self):
        """Arming alone proves nothing: with no target registered there is
        nothing for the delivery to call."""
        set_config_delivery_armed("circuit_breaker", True, converges_within_seconds=30)

        declaration = describe_config_runtime_apply("circuit_breaker")

        assert declaration.mode is RuntimeApplyMode.UNVERIFIED
        assert declaration.converges_within_seconds is None

    @pytest.mark.parametrize(
        "config_type",
        ["circuit_breaker", "retry", "dlq", "rate_limit", "a_domain_invented_here"],
    )
    def test_every_domain_follows_the_same_derivation(self, config_type):
        """Negative assertion against a per-domain literal: an unknown domain
        goes through the identical registered→armed ladder, so no domain can
        carry a hand-authored answer."""
        assert (
            describe_config_runtime_apply(config_type).mode
            is RuntimeApplyMode.UNVERIFIED
        )

        register_config_invalidation_target(config_type, lambda: None)
        assert (
            describe_config_runtime_apply(config_type).mode
            is RuntimeApplyMode.STORED_ONLY
        )

        set_config_delivery_armed(config_type, True)
        assert describe_config_runtime_apply(config_type).mode is RuntimeApplyMode.LIVE

    def test_each_mode_carries_its_own_detail_sentence(self):
        """The operator-facing sentence must distinguish the three modes — a
        shared string would make the badge unreadable."""
        unverified = describe_config_runtime_apply("circuit_breaker").detail

        register_config_invalidation_target("circuit_breaker", lambda: None)
        stored_only = describe_config_runtime_apply("circuit_breaker").detail

        set_config_delivery_armed("circuit_breaker", True)
        live = describe_config_runtime_apply("circuit_breaker").detail

        assert len({unverified, stored_only, live}) == 3
        assert all(sentence.strip() for sentence in (unverified, stored_only, live))


# =============================================================================
# Runtime-apply response shape (Contract)
# =============================================================================


class TestRuntimeApplyContract:
    """The three mode strings and the three response keys are consumed by the
    console badge and by PRO's response builder, so they are pinned here."""

    def test_mode_string_values(self):
        assert RuntimeApplyMode.LIVE.value == "live"
        assert RuntimeApplyMode.STORED_ONLY.value == "stored_only"
        assert RuntimeApplyMode.UNVERIFIED.value == "unverified"

    def test_mode_set_is_exactly_three_members(self):
        assert {mode.value for mode in RuntimeApplyMode} == {
            "live",
            "stored_only",
            "unverified",
        }

    def test_mode_is_json_serializable_as_a_string(self):
        assert RuntimeApplyMode.LIVE == "live"

    def test_to_dict_renders_the_three_response_keys(self):
        declaration = RuntimeApplyDeclaration(
            mode=RuntimeApplyMode.LIVE,
            converges_within_seconds=30,
            detail="Delivered.",
        )

        assert declaration.to_dict() == {
            "mode": "live",
            "converges_within_seconds": 30,
            "detail": "Delivered.",
        }

    def test_to_dict_renders_the_mode_as_its_string_value(self):
        """The response is serialized to JSON, so the enum member itself must
        not leak into the body."""
        rendered = RuntimeApplyDeclaration(
            mode=RuntimeApplyMode.UNVERIFIED,
            converges_within_seconds=None,
            detail="Not verified.",
        ).to_dict()

        assert rendered["mode"] == "unverified"
        assert rendered["converges_within_seconds"] is None

    def test_declaration_is_frozen(self):
        """The declaration is handed to response builders; none of them may
        edit the statement after it is derived."""
        declaration = describe_config_runtime_apply("circuit_breaker")

        with pytest.raises(FrozenInstanceError):
            declaration.mode = RuntimeApplyMode.LIVE  # type: ignore[misc]
