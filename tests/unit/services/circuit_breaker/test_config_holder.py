"""Unit tests for the process-shared circuit-breaker config holder and the
per-instance pinning that opts out of it (744 D15).

Targets:
  - ``current_circuit_breaker_config`` / ``invalidate_circuit_breaker_config`` /
    ``reset_circuit_breaker_config`` — one configuration object per process,
    swapped by an eager rebuild rather than rebuilt per service.
  - ``CircuitBreakerService.config`` property + setter — an explicitly injected
    configuration is pinned to its instance and never follows a runtime edit;
    everything else reads the shared holder.

Verification techniques (§8):
  - Singleton/lifecycle — repeated reads return one object; reset rebuilds
  - State transition — an invalidation is observed by shared-config instances
    and ignored by pinned ones
  - Exception/edge — a failing rebuild leaves the previous configuration in
    force rather than clearing it
  - Side effects — in-flight rate evidence and mesh overrides survive a swap

The runtime-config manager slot is stubbed out for the whole module: these
tests measure the holder, so the config source has to be the deterministic
static-settings branch rather than whichever provider happens to be registered.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.services.circuit_breaker.config import (
    CircuitBreakerConfig,
    current_circuit_breaker_config,
    invalidate_circuit_breaker_config,
    reset_circuit_breaker_config,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService


@pytest.fixture(autouse=True)
def _static_settings_source():
    """Force the static-settings branch and isolate holder + settings state.

    Isolation is applied at the registry slot ``from_settings`` actually
    consults: blocking the import only forces the OSS branch until something
    else has resolved the provider, after which the registry serves the cached
    instance and the manager branch wins again.
    """
    from baldur.factory.registry import ProviderRegistry
    from baldur.settings.circuit_breaker import reset_circuit_breaker_settings
    from baldur.settings.root import reset_config

    with patch.object(
        ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
    ):
        reset_circuit_breaker_settings()
        reset_config()
        reset_circuit_breaker_config()
        yield
        reset_circuit_breaker_config()
        reset_circuit_breaker_settings()
        reset_config()


def _reload_settings(monkeypatch, env_var: str, value: str) -> None:
    """Apply a ``BALDUR_CB_*`` override and drop the cached settings for it."""
    from baldur.settings.circuit_breaker import reset_circuit_breaker_settings
    from baldur.settings.root import reset_config

    monkeypatch.setenv(env_var, value)
    reset_circuit_breaker_settings()
    reset_config()


# =============================================================================
# Holder lifecycle + reload (Behavior)
# =============================================================================


class TestCircuitBreakerConfigHolderBehavior:
    """One configuration object per process, swapped on invalidation."""

    def test_repeated_reads_return_the_same_object(self):
        """The point of the holder: a read is a pointer read, not a build."""
        first = current_circuit_breaker_config()
        second = current_circuit_breaker_config()

        assert first is second

    def test_first_read_builds_from_settings_when_nothing_is_seeded(self):
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        config = current_circuit_breaker_config()

        assert isinstance(config, CircuitBreakerConfig)
        assert (
            config.sliding_window_size == CircuitBreakerSettings().sliding_window_size
        )

    def test_invalidate_swaps_in_a_new_object_and_returns_it(self):
        before = current_circuit_breaker_config()

        rebuilt = invalidate_circuit_breaker_config()

        assert rebuilt is not before
        assert current_circuit_breaker_config() is rebuilt

    def test_invalidate_is_idempotent_in_value(self):
        """Two invalidations with no settings change in between produce equal
        configurations — the swap carries no accumulating state."""
        first = invalidate_circuit_breaker_config()
        second = invalidate_circuit_breaker_config()

        assert first is not second
        assert first == second

    def test_reset_drops_the_holder_so_the_next_read_rebuilds(self):
        before = current_circuit_breaker_config()

        reset_circuit_breaker_config()

        assert current_circuit_breaker_config() is not before

    def test_failed_rebuild_keeps_the_previous_configuration(self):
        """A transient config-source failure must never leave the process
        without a configuration — the breaker would have nothing to decide on."""
        # Given — a holder in force
        before = current_circuit_breaker_config()

        # When — the rebuild blows up
        with patch.object(
            CircuitBreakerConfig,
            "from_settings",
            side_effect=RuntimeError("config source down"),
        ):
            returned = invalidate_circuit_breaker_config()

        # Then — the previous configuration is both returned and still in force
        assert returned is before
        assert current_circuit_breaker_config() is before

    def test_failed_rebuild_on_an_empty_holder_returns_none(self):
        """Nothing to fall back to reports itself rather than inventing a
        configuration."""
        reset_circuit_breaker_config()

        with patch.object(
            CircuitBreakerConfig,
            "from_settings",
            side_effect=RuntimeError("config source down"),
        ):
            assert invalidate_circuit_breaker_config() is None

    @pytest.mark.parametrize(
        ("env_var", "field", "raw", "expected"),
        [
            ("BALDUR_CB_FAILURE_THRESHOLD", "failure_threshold", "9", 9),
            ("BALDUR_CB_RECOVERY_TIMEOUT", "recovery_timeout", "45", 45),
            ("BALDUR_CB_SUCCESS_THRESHOLD", "success_threshold", "7", 7),
            ("BALDUR_CB_SLIDING_WINDOW_SIZE", "sliding_window_size", "250", 250),
            ("BALDUR_CB_MINIMUM_CALLS", "minimum_calls", "3", 3),
            (
                "BALDUR_CB_FAILURE_RATE_THRESHOLD",
                "failure_rate_threshold",
                "42.5",
                42.5,
            ),
            ("BALDUR_CB_HALF_OPEN_MAX_CALLS", "half_open_max_calls", "5", 5),
            (
                "BALDUR_CB_HALF_OPEN_STUCK_TIMEOUT_SECONDS",
                "half_open_stuck_timeout_seconds",
                "120",
                120,
            ),
            (
                "BALDUR_CB_MANUAL_OVERRIDE_TTL_MINUTES",
                "manual_override_ttl_minutes",
                "30",
                30,
            ),
            (
                "BALDUR_CB_RATE_LIMIT_CASCADE_WINDOW_SECONDS",
                "rate_limit_cascade_window_seconds",
                "90",
                90,
            ),
            ("BALDUR_CB_SELF_DDOS_RPS_LIMIT", "self_ddos_rps_limit", "500", 500),
        ],
    )
    def test_invalidation_reloads_every_field_with_no_special_case(
        self, monkeypatch, env_var, field, raw, expected
    ):
        """One row per representative field — including
        ``manual_override_ttl_minutes``, which no field-specific branch may
        skip. A field that stopped reloading would fail only its own row."""
        # Given — a holder built before the change
        before = current_circuit_breaker_config()
        assert getattr(before, field) != expected

        # When — the settings change and the holder is invalidated
        _reload_settings(monkeypatch, env_var, raw)
        invalidate_circuit_breaker_config()

        # Then
        assert getattr(current_circuit_breaker_config(), field) == expected

    def test_service_without_a_pinned_config_observes_the_swap(self, monkeypatch):
        service = CircuitBreakerService()
        assert service.config.failure_threshold != 9

        _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "9")
        invalidate_circuit_breaker_config()

        assert service.config.failure_threshold == 9

    def test_two_default_services_share_one_configuration_object(self):
        """The whole reason for the holder: N breakers hold no N snapshots."""
        first = CircuitBreakerService()
        second = CircuitBreakerService()

        assert first.config is second.config

    def test_outcome_window_survives_an_invalidation(self, monkeypatch):
        """In-flight rate evidence is per instance and must not be discarded by
        a configuration swap — discarding it suspends the rate trigger for a
        whole window's worth of calls."""
        # Given — a service with recorded outcomes
        service = CircuitBreakerService()
        window = service._outcome_window
        window.record_failure("payments", 10)
        window.record_success("payments", 10)

        # When
        _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "9")
        invalidate_circuit_breaker_config()

        # Then — same object, same recorded evidence
        assert service._outcome_window is window
        assert service._outcome_window.read("payments") == (1, 2)

    def test_threshold_overrides_survive_an_invalidation(self, monkeypatch):
        """Mesh overrides are produced in process and keyed per instance; a
        config swap is not a reason to drop them."""
        service = CircuitBreakerService()
        overrides = service._threshold_overrides

        _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "9")
        invalidate_circuit_breaker_config()

        assert service._threshold_overrides is overrides

    def test_growing_the_window_size_preserves_recorded_outcomes(self):
        """A ``sliding_window_size`` edit resizes the ring rather than emptying
        it, so the evidence recorded before the edit still counts."""
        service = CircuitBreakerService()
        for _ in range(3):
            service._outcome_window.record_failure("payments", 5)

        service._outcome_window.record_success("payments", 10)

        assert service._outcome_window.read("payments") == (3, 4)

    def test_shrinking_the_window_size_keeps_the_most_recent_outcomes(self):
        service = CircuitBreakerService()
        for _ in range(4):
            service._outcome_window.record_failure("payments", 10)
        service._outcome_window.record_success("payments", 10)

        service._outcome_window.record_success("payments", 2)

        # The rightmost two survive: the success recorded at size 10, plus the
        # one that triggered the resize.
        assert service._outcome_window.read("payments") == (0, 2)


# =============================================================================
# Per-instance pinning (Behavior)
# =============================================================================


class TestCircuitBreakerConfigPinningBehavior:
    """An explicitly injected configuration belongs to its instance."""

    def test_injected_config_is_returned_verbatim(self):
        pinned = CircuitBreakerConfig(failure_threshold=42)

        service = CircuitBreakerService(config=pinned)

        assert service.config is pinned

    def test_injected_config_never_follows_an_invalidation(self, monkeypatch):
        """The precomputed-cache worker builds a bespoke configuration; a
        console edit must not silently retune it."""
        pinned = CircuitBreakerConfig(failure_threshold=42)
        service = CircuitBreakerService(config=pinned)

        _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "9")
        invalidate_circuit_breaker_config()

        assert service.config is pinned
        assert service.config.failure_threshold == 42

    def test_setter_pins_a_previously_shared_instance(self, monkeypatch):
        service = CircuitBreakerService()
        assert service.config is current_circuit_breaker_config()

        service.config = CircuitBreakerConfig(failure_threshold=42)

        _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "9")
        invalidate_circuit_breaker_config()

        assert service.config.failure_threshold == 42

    def test_pinning_one_instance_leaves_the_others_on_the_shared_holder(self):
        pinned = CircuitBreakerService(
            config=CircuitBreakerConfig(failure_threshold=42)
        )
        shared = CircuitBreakerService()

        assert pinned.config is not shared.config
        assert shared.config is current_circuit_breaker_config()

    def test_construction_without_a_config_does_not_build_one(self):
        """The guard against the ``config or from_settings()`` regression:
        building at construction puts the config-source lock — held across an
        administrative write's backend round trip — on a request thread."""
        with patch.object(
            CircuitBreakerConfig, "from_settings", autospec=True
        ) as mock_from_settings:
            CircuitBreakerService()

        mock_from_settings.assert_not_called()

    def test_construction_with_a_config_does_not_build_one_either(self):
        with patch.object(
            CircuitBreakerConfig, "from_settings", autospec=True
        ) as mock_from_settings:
            CircuitBreakerService(config=CircuitBreakerConfig())

        mock_from_settings.assert_not_called()

    def test_is_enabled_reads_through_the_pinned_config(self):
        """``is_enabled`` is the admission fast path; it must see the pin."""
        service = CircuitBreakerService(config=CircuitBreakerConfig(enabled=False))

        assert service.is_enabled is False

    def test_get_effective_config_returns_the_shared_config_without_overrides(self):
        service = CircuitBreakerService()
        shared = current_circuit_breaker_config()

        assert service.get_effective_config("payments") is shared
