"""Unit tests for ``baldur.bootstrap._init_bridge_instrumentation`` (impl 451 D8).

Scope:
- Gating: instrument only when both ``tenacity_enabled`` AND ``tenacity_instrument`` are True.
- Settings unavailable → silent skip.
- ImportError from bridges package → DEBUG log, no abort.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_bridge_settings():
    from baldur.settings.bridge import reset_bridge_settings

    reset_bridge_settings()
    yield
    reset_bridge_settings()


@pytest.fixture(autouse=True)
def _reset_tenacity_instrument():
    from baldur.bridges.tenacity.instrument import _reset_instrument_for_testing

    _reset_instrument_for_testing()
    yield
    _reset_instrument_for_testing()


# =============================================================================
# Behavior — gating truth table
# =============================================================================


class TestInitBridgeInstrumentationBehavior:
    """``_init_bridge_instrumentation`` only instruments when both flags are on."""

    @pytest.mark.parametrize(
        ("enabled", "instrument", "should_call"),
        [
            (False, False, False),
            (False, True, False),
            (True, False, False),
            (True, True, True),
        ],
        ids=["both_off", "instrument_only", "enabled_only", "both_on"],
    )
    def test_only_calls_instrument_when_both_flags_true(
        self, monkeypatch, enabled, instrument, should_call
    ):
        """Instrumentation runs only when both gates are True."""
        monkeypatch.setenv(
            "BALDUR_BRIDGE_TENACITY_ENABLED", "true" if enabled else "false"
        )
        monkeypatch.setenv(
            "BALDUR_BRIDGE_TENACITY_INSTRUMENT", "true" if instrument else "false"
        )
        # Reset settings cache so env vars are picked up.
        from baldur.settings.bridge import reset_bridge_settings

        reset_bridge_settings()

        from baldur import bootstrap

        with patch(
            "baldur.bridges.tenacity.instrument_tenacity", autospec=True
        ) as mock_instrument:
            bootstrap._init_bridge_instrumentation()

        if should_call:
            mock_instrument.assert_called_once()
        else:
            mock_instrument.assert_not_called()

    def test_settings_unavailable_swallowed(self, monkeypatch):
        """If ``get_bridge_settings`` raises, function returns silently."""
        from baldur import bootstrap

        def _raise(*_a, **_kw):
            raise RuntimeError("settings broken")

        with patch("baldur.settings.bridge.get_bridge_settings", side_effect=_raise):
            # Must not propagate the RuntimeError.
            bootstrap._init_bridge_instrumentation()
