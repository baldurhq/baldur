"""#661 — Load-shedding tier reconciliation regression.

After #661, "load shedding" reconciles to a single authoritative tier (Deferred).
The dead Core flag (`ScalingSettings.load_shedding_enabled` /
`BALDUR_SCALING_LOAD_SHEDDING_ENABLED`) is removed; the sole request-path gate is
`BackpressureSettings.backpressure_enabled` (Deferred, default-off), and
`RateController.should_process()` records no counters while that gate is off.

Guards against:
- Reintroducing the dead scaling settings module / class / env var.
- The Deferred backpressure gate silently defaulting to enabled.
- `should_process()` recording process/drop counters when the gate is off,
  which would make the daily-report "Load Shedding" section render on data
  presence (the section-tier verdict supplied to #660).
"""

from __future__ import annotations

import pytest

from baldur.scaling.rate_controller import RateController
from baldur.settings.backpressure import BackpressureSettings


class TestDeadScalingFlagRemovedContract:
    """SC1/SC2 — the dead `ScalingSettings` module/class/env var stay gone."""

    def test_scaling_settings_module_not_importable(self):
        """`baldur.settings.scaling` no longer exists (ModuleNotFoundError)."""
        with pytest.raises(ImportError):
            import baldur.settings.scaling  # noqa: F401


class TestBackpressureIsSoleDeferredGateContract:
    """SC3 — `backpressure_enabled` is the single authoritative gate, default-off."""

    def test_backpressure_enabled_defaults_false(self, monkeypatch):
        """The Deferred gate defaults to disabled; ambient env must not flip it."""
        monkeypatch.delenv("BALDUR_BACKPRESSURE_BACKPRESSURE_ENABLED", raising=False)

        assert BackpressureSettings().backpressure_enabled is False


class TestNoCountersWhenDisabledBehavior:
    """SC4 — `should_process()` records nothing while the gate is off."""

    def test_should_process_records_no_counters_when_disabled(self):
        """Disabled gate → request passes, but no process/drop counter moves.

        Constructor injection (`RateController(settings=...)`) avoids any global
        settings monkeypatch. With the gate off, `should_process()` early-returns
        without recording, so `get_state()` counts stay 0 — which is what keeps
        the daily-report "Load Shedding" section empty (False-Zero guard).
        """
        controller = RateController(
            settings=BackpressureSettings(backpressure_enabled=False)
        )

        for priority in ("critical", "standard", "non_essential"):
            assert controller.should_process(priority) is True

        state = controller.get_state()
        assert state.processed_count == 0
        assert state.dropped_count == 0
