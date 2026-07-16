"""Bulkhead metrics updater starter — background-worker registry member.

Relocated with the bulkhead primitives: the starter moved from the licensed
package's startup-integration slot into the core ``_BACKGROUND_WORKER_STARTERS``
registry, so its gating matrix (AUTOSTART hatch, gunicorn-master skip,
``metrics_enabled`` flag, interval threading) is pinned here.

Every test that really starts the updater stops/resets it in teardown (daemon
thread hygiene for the parallel suite).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur import bootstrap
from baldur.settings.bulkhead import BulkheadSettings


@pytest.fixture
def enable_autostart(monkeypatch):
    """Re-enable the autostart hatch (tests/conftest.py pins it to ``0`` so a
    stray ``init()`` never spawns the poll daemon)."""
    monkeypatch.setenv("BALDUR_BULKHEAD_METRICS_AUTOSTART", "1")


@pytest.fixture
def non_gunicorn_env(monkeypatch):
    """Strip the gunicorn env so ``is_gunicorn_master()`` returns False."""
    monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)


@pytest.fixture
def gunicorn_master_env(monkeypatch):
    """Simulate the gunicorn master: ``SERVER_SOFTWARE`` set, worker flag unset."""
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)


@pytest.fixture(autouse=True)
def _reset_updater_singleton():
    """Stop + reset the updater singleton so no daemon thread leaks."""
    from baldur.services.bulkhead.metrics import reset_bulkhead_metrics

    reset_bulkhead_metrics()
    yield
    reset_bulkhead_metrics()


class TestBulkheadMetricsStarterGating:
    """The starter skips on the hatch, in the master, and when disabled."""

    def test_registered_in_background_worker_starters(self):
        """The starter is a member of the background-worker registry."""
        assert (
            bootstrap._start_bulkhead_metrics_updater_if_enabled
            in bootstrap._BACKGROUND_WORKER_STARTERS
        )

    def test_autostart_hatch_skips_before_everything(self, monkeypatch):
        """AUTOSTART=0 (the test-process default) returns before any lookup."""
        monkeypatch.setenv("BALDUR_BULKHEAD_METRICS_AUTOSTART", "0")
        with patch(
            "baldur.settings.bulkhead.get_bulkhead_settings", autospec=True
        ) as get_settings:
            bootstrap._start_bulkhead_metrics_updater_if_enabled()

        get_settings.assert_not_called()

    def test_master_skips_before_reading_settings(
        self, enable_autostart, gunicorn_master_env
    ):
        with patch(
            "baldur.settings.bulkhead.get_bulkhead_settings", autospec=True
        ) as get_settings:
            bootstrap._start_bulkhead_metrics_updater_if_enabled()

        # Master-skip returns before the settings lookup.
        get_settings.assert_not_called()

    def test_disabled_does_not_start(self, enable_autostart, non_gunicorn_env):
        with (
            patch(
                "baldur.settings.bulkhead.get_bulkhead_settings",
                return_value=MagicMock(
                    spec=BulkheadSettings,
                    metrics_enabled=False,
                    metrics_update_interval=10.0,
                ),
            ),
            patch("baldur.services.bulkhead.metrics.start_metrics_updater") as start_fn,
        ):
            bootstrap._start_bulkhead_metrics_updater_if_enabled()

        start_fn.assert_not_called()

    def test_enabled_starts_updater_honoring_interval(
        self, enable_autostart, non_gunicorn_env
    ):
        # Given a non-default interval, to prove it is threaded through.
        from baldur.services.bulkhead.metrics import get_metrics_updater

        with patch(
            "baldur.settings.bulkhead.get_bulkhead_settings",
            return_value=MagicMock(
                spec=BulkheadSettings,
                metrics_enabled=True,
                metrics_update_interval=12.0,
            ),
        ):
            # When the starter runs (the sole production first-caller of
            # get_metrics_updater, so it captures the interval).
            bootstrap._start_bulkhead_metrics_updater_if_enabled()

        # Then the updater is running and the configured interval was honored.
        updater = get_metrics_updater()
        assert updater._running is True
        assert updater._interval == 12.0
