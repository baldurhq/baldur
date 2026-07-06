"""Contract tests for disk-buffer chained-closure chain-walk markers.

The SIGTERM/SIGINT closures installed by
``audit/persistence/disk_buffer_shutdown._register_signal_handlers``
must expose the captured prior disposition via the
``_baldur_chained_original`` attribute — the marker the shutdown
coordinator's disposition classification walks so a buffer handler
registered BEFORE the coordinator cannot flip the chain/defer verdict.
"""
# Marker attribute coverage: 597 D8 (consumed by the D2 chain-walk).

from __future__ import annotations

import signal
import sys
from unittest.mock import patch

import baldur.audit.persistence.disk_buffer_shutdown as shutdown_module


def _register_with_dispositions(monkeypatch, dispositions):
    """Run _register_signal_handlers on the POSIX path against a fake
    per-signal pre-disposition map; returns signum → installed handler."""
    monkeypatch.setattr(sys, "platform", "linux")
    with (
        patch(
            "baldur.core.process_utils.is_under_gunicorn",
            return_value=False,
        ),
        patch("signal.getsignal", side_effect=lambda sig: dispositions[sig]),
        patch("signal.signal") as mock_signal,
    ):
        shutdown_module._register_signal_handlers()
    return {args[0][0]: args[0][1] for args in mock_signal.call_args_list}


class TestDiskBufferChainMarkersContract:
    """Chained closures carry the captured original as the chain-walk marker."""

    def test_installed_handlers_carry_captured_original_as_marker(self, monkeypatch):
        """Each signal's closure marker IS that signal's captured disposition."""

        # Given — distinct pre-installed dispositions per signal
        def _prior_sigterm(signum, frame):
            pass

        def _prior_sigint(signum, frame):
            pass

        dispositions = {
            signal.SIGTERM: _prior_sigterm,
            signal.SIGINT: _prior_sigint,
        }

        # When
        installed = _register_with_dispositions(monkeypatch, dispositions)

        # Then
        assert set(installed) == {signal.SIGTERM, signal.SIGINT}
        assert installed[signal.SIGTERM]._baldur_chained_original is _prior_sigterm
        assert installed[signal.SIGINT]._baldur_chained_original is _prior_sigint

    def test_marker_preserves_non_callable_original(self, monkeypatch):
        """A SIG_DFL capture stays visible through the marker — this is what
        lets the coordinator classify a buffer-headed chain as defer-exit."""
        dispositions = {
            signal.SIGTERM: signal.SIG_DFL,
            signal.SIGINT: signal.SIG_DFL,
        }

        installed = _register_with_dispositions(monkeypatch, dispositions)

        assert installed[signal.SIGTERM]._baldur_chained_original is signal.SIG_DFL
        assert installed[signal.SIGINT]._baldur_chained_original is signal.SIG_DFL
