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
  - Concurrency — an out-of-order rebuild never becomes the configuration in
    force, and the holder lock is a leaf so a first-ever read cannot deadlock
    against a configuration write

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

#: Bound on the two-thread lock-order regression: a real inversion hangs
#: forever, so the failure has to be a timeout rather than a stuck run.
_DEADLOCK_TIMEOUT_SECONDS = 10.0


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


# =============================================================================
# Ordered swap + leaf lock (Behavior)
# =============================================================================


class TestSourceGenerationResolutionBehavior:
    """Reading the config source's install counter never breaks a rebuild.

    ``None`` means nothing can order this build, and every branch that produces
    it is a property of the process rather than a transient state: no source
    registered, or a source that predates the counter.
    """

    def _read_generation(self):
        from baldur.services.circuit_breaker.config import _source_install_generation

        return _source_install_generation()

    def test_no_registered_source_yields_none(self):
        assert self._read_generation() is None

    def test_a_source_without_the_accessor_yields_none(self):
        """The normal state between this release and the next one of the package
        that provides the source — the two release independently."""
        from unittest.mock import MagicMock

        from baldur.factory.registry import ProviderRegistry

        legacy_source = MagicMock(spec=[])
        with patch.object(
            ProviderRegistry.runtime_config_manager,
            "safe_get",
            return_value=legacy_source,
        ):
            assert self._read_generation() is None

    def test_a_raising_source_yields_none_rather_than_propagating(self):
        from unittest.mock import MagicMock

        from baldur.factory.registry import ProviderRegistry
        from baldur.interfaces.runtime_config import RuntimeConfigManager

        source = MagicMock(spec=RuntimeConfigManager)
        source.get_section_generation.side_effect = RuntimeError("source down")
        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=source
        ):
            assert self._read_generation() is None

    def test_a_registered_source_yields_its_counter_for_this_section(self):
        from unittest.mock import MagicMock

        from baldur.factory.registry import ProviderRegistry
        from baldur.interfaces.runtime_config import RuntimeConfigManager

        source = MagicMock(spec=RuntimeConfigManager)
        source.get_section_generation.return_value = 4
        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=source
        ):
            assert self._read_generation() == 4

        source.get_section_generation.assert_called_with("circuit_breaker")


def _with_generation(value):
    """Pin what the config source's install counter reports for this build."""
    return patch(
        "baldur.services.circuit_breaker.config._source_install_generation",
        return_value=value,
    )


class TestConfigHolderOrderingBehavior:
    """The swap is ordered, not last-writer-wins.

    The replacement is built outside the holder lock, so two invalidations that
    read different values can finish in the opposite order — and an
    unconditional assignment would leave the older configuration in force with
    nothing to correct it: the counter has already moved past it, so no later
    poll delivers anything.
    """

    def test_a_build_from_an_older_counter_does_not_win(self, monkeypatch):
        # Given: a configuration built from counter 2 is in force
        with _with_generation(2):
            _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "9")
            newer = invalidate_circuit_breaker_config()
        assert newer.failure_threshold == 9

        # When: a rebuild that started from counter 1 completes afterwards
        with _with_generation(1):
            _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "3")
            returned = invalidate_circuit_breaker_config()

        # Then: the newer configuration is still in force, and the caller is
        # told what is in force rather than what it built
        assert current_circuit_breaker_config() is newer
        assert current_circuit_breaker_config().failure_threshold == 9
        assert returned is newer

    def test_a_build_from_the_same_counter_installs(self, monkeypatch):
        """Only a strictly older build is discarded: two rebuilds at one counter
        are the ordinary shape of a settings-only change."""
        with _with_generation(2):
            invalidate_circuit_breaker_config()
            _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "3")
            rebuilt = invalidate_circuit_breaker_config()

        assert current_circuit_breaker_config() is rebuilt
        assert rebuilt.failure_threshold == 3

    def test_a_build_from_a_newer_counter_installs(self, monkeypatch):
        with _with_generation(1):
            invalidate_circuit_breaker_config()
        with _with_generation(2):
            _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "3")
            rebuilt = invalidate_circuit_breaker_config()

        assert current_circuit_breaker_config() is rebuilt

    def test_a_build_carrying_no_counter_installs_unconditionally(self, monkeypatch):
        """An environment-sourced build has no ordering to preserve, so it must
        not be refused by a rule written for stored values."""
        with _with_generation(5):
            invalidate_circuit_breaker_config()

        with _with_generation(None):
            _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "3")
            rebuilt = invalidate_circuit_breaker_config()

        assert current_circuit_breaker_config() is rebuilt

    def test_a_counterless_install_clears_the_record_so_the_next_build_is_admitted(
        self, monkeypatch
    ):
        """The cold-boot ordering. A first reader that builds before the source
        is registered installs an environment-sourced configuration; the very
        next invalidation is the first one that *can* read stored values, and a
        naive ``>=`` against a stale record would reject it.
        """
        with _with_generation(None):
            invalidate_circuit_breaker_config()

        with _with_generation(1):
            _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "3")
            rebuilt = invalidate_circuit_breaker_config()

        assert current_circuit_breaker_config() is rebuilt

    def test_an_absent_record_admits_any_counter(self):
        """The state at the first invalidation of a process's life, and after
        every reset."""
        reset_circuit_breaker_config()

        with _with_generation(7):
            rebuilt = invalidate_circuit_breaker_config()

        assert current_circuit_breaker_config() is rebuilt

    def test_reset_clears_the_recorded_counter_too(self, monkeypatch):
        with _with_generation(9):
            invalidate_circuit_breaker_config()

        reset_circuit_breaker_config()
        with _with_generation(1):
            _reload_settings(monkeypatch, "BALDUR_CB_FAILURE_THRESHOLD", "3")
            rebuilt = invalidate_circuit_breaker_config()

        assert current_circuit_breaker_config() is rebuilt

    def test_the_counter_is_read_before_the_build(self):
        """What keeps the recorded number from ever overstating the freshness of
        what it labels — and therefore what makes the only rejectable build one
        that a same-or-newer build has already superseded."""
        order = []

        def _read_generation():
            order.append("read_generation")
            return 1

        def _build():
            order.append("build")
            return CircuitBreakerConfig()

        with (
            patch(
                "baldur.services.circuit_breaker.config._source_install_generation",
                side_effect=_read_generation,
            ),
            patch.object(CircuitBreakerConfig, "from_settings", side_effect=_build),
        ):
            invalidate_circuit_breaker_config()

        assert order == ["read_generation", "build"]

    def test_a_failed_build_leaves_the_previous_configuration_and_its_record(
        self, monkeypatch
    ):
        with _with_generation(3):
            in_force = invalidate_circuit_breaker_config()

        with (
            _with_generation(4),
            patch.object(
                CircuitBreakerConfig,
                "from_settings",
                side_effect=RuntimeError("settings unavailable"),
            ),
        ):
            returned = invalidate_circuit_breaker_config()

        assert returned is in_force
        assert current_circuit_breaker_config() is in_force


class TestConfigHolderLeafLockBehavior:
    """The holder lock acquires nothing, so it cannot invert against any other.

    The lazy read path used to build inside the holder lock, and the build takes
    the config source's own lock underneath it — while the eager invalidation
    path takes the holder lock underneath *that* one. That is an ABBA deadlock
    between a configuration write and a first-ever read of this holder, latent
    only while nothing else can invalidate concurrently.
    """

    def test_the_lazy_path_does_not_build_while_holding_the_holder_lock(self):
        """Negative assertion: the old shape must not come back."""
        import baldur.services.circuit_breaker.config as config_module

        observed = []

        def _observing_build():
            observed.append(config_module._current_config_lock.locked())
            return CircuitBreakerConfig()

        reset_circuit_breaker_config()
        with patch.object(
            CircuitBreakerConfig, "from_settings", side_effect=_observing_build
        ):
            current_circuit_breaker_config()

        assert observed == [False]

    def test_a_first_read_and_a_concurrent_rebuild_both_complete(self):
        """The regression itself: two real threads crossing the two lock orders.

        The holder is cleared on every iteration — a seeded holder makes the
        lazy path unreachable and the test vacuous.
        """
        import threading

        reset_circuit_breaker_config()
        done = []
        barrier = threading.Barrier(2, timeout=_DEADLOCK_TIMEOUT_SECONDS)

        def _first_reader():
            barrier.wait()
            for _ in range(50):
                reset_circuit_breaker_config()
                current_circuit_breaker_config()
            done.append("reader")

        def _invalidator():
            barrier.wait()
            for _ in range(50):
                invalidate_circuit_breaker_config()
            done.append("invalidator")

        threads = [
            threading.Thread(target=_first_reader, name="cb-holder-reader"),
            threading.Thread(target=_invalidator, name="cb-holder-invalidator"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_DEADLOCK_TIMEOUT_SECONDS)

        assert [t.name for t in threads if t.is_alive()] == []
        assert sorted(done) == ["invalidator", "reader"]

    def test_the_lazy_path_returns_what_is_actually_in_force(self):
        """A concurrent herd may each build; one wins, and every caller is told
        the winner rather than its own object."""
        reset_circuit_breaker_config()

        first = current_circuit_breaker_config()
        second = current_circuit_breaker_config()

        assert first is second

    def test_the_lazy_path_still_yields_a_configuration_when_the_swap_returns_none(
        self,
    ):
        """``invalidate`` returns ``None`` only when the build itself failed and
        the holder is still empty; the accessor then lets that failure reach the
        caller rather than returning a fabricated configuration."""
        reset_circuit_breaker_config()
        with patch(
            "baldur.services.circuit_breaker.config.invalidate_circuit_breaker_config",
            return_value=None,
        ):
            assert isinstance(current_circuit_breaker_config(), CircuitBreakerConfig)
