"""Domain-gauge collector starter — background-worker registry member.

Membership in ``_BACKGROUND_WORKER_STARTERS`` is what makes the gauge refresh
per-process: ``init()`` drives that tuple on the single-process shapes and the
gunicorn ``post_worker_init`` hook drives it again inside every forked worker.
A scheduler entry or a beat task would refresh exactly one process's scrape
surface, because the Prometheus registry is process-local — so the membership
below is a design contract, not an implementation detail.

The gating matrix is pinned here as well: the AUTOSTART hatch, the
gunicorn-master skip, the ``metrics.enabled`` flag and the Prometheus-client
availability gate. The last one matters most in the negative: without the
client the collector module raises at import, and the starter's own fail-soft
catch would swallow that into a thread that silently never exists — taking the
DLQ paging series with it.

Every test that really starts the collector resets the singleton in teardown
(daemon-thread hygiene for the parallel suite).

Reference:
    src/baldur/bootstrap.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur import bootstrap
from baldur.settings.metrics import MetricsSettings


@pytest.fixture
def enable_autostart(monkeypatch):
    """Re-enable the autostart hatch (tests/conftest.py pins it to ``0`` so a
    stray ``init()`` never spawns the collector daemon)."""
    monkeypatch.setenv("BALDUR_DOMAIN_GAUGE_UPDATER_AUTOSTART", "1")


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
    """Stop + drop the collector singleton so no daemon thread leaks."""
    from baldur.services.metrics.periodic_updater import reset_domain_gauge_updater

    reset_domain_gauge_updater()
    yield
    reset_domain_gauge_updater()


def _metrics_settings(**overrides) -> MagicMock:
    """A settings double carrying the fields the starter reads."""
    values = {
        "enabled": True,
        "collection_interval_seconds": 12.0,
        "jitter_enabled": False,
        "jitter_max_delay_seconds": 60.0,
    }
    values.update(overrides)
    return MagicMock(spec=MetricsSettings, **values)


def _debug_events(mock_logger) -> list[str]:
    """Event names the starter emitted at DEBUG."""
    return [call.args[0] for call in mock_logger.debug.call_args_list if call.args]


class TestDomainGaugeUpdaterStarterContract:
    """The starter is a member of the per-process background-worker registry."""

    def test_starter_registered_in_background_worker_starters(self):
        """Registry membership is what reaches every forked worker."""
        assert (
            bootstrap._start_domain_gauge_updater_if_enabled
            in bootstrap._BACKGROUND_WORKER_STARTERS
        )


class TestDomainGaugeUpdaterStarterGating:
    """The starter skips on the hatch, in the master, and when unusable."""

    def test_starter_autostart_hatch_skips_before_everything(self, monkeypatch):
        """AUTOSTART=0 (the test-process default) returns before any lookup."""
        monkeypatch.setenv("BALDUR_DOMAIN_GAUGE_UPDATER_AUTOSTART", "0")
        with patch(
            "baldur.settings.metrics.get_metrics_settings", autospec=True
        ) as get_settings:
            bootstrap._start_domain_gauge_updater_if_enabled()

        get_settings.assert_not_called()

    def test_starter_gunicorn_master_skips_before_reading_settings(
        self, enable_autostart, gunicorn_master_env
    ):
        """The master's thread would not survive ``fork()`` anyway."""
        with patch(
            "baldur.settings.metrics.get_metrics_settings", autospec=True
        ) as get_settings:
            bootstrap._start_domain_gauge_updater_if_enabled()

        get_settings.assert_not_called()

    def test_starter_skips_when_metrics_are_disabled(
        self, enable_autostart, non_gunicorn_env
    ):
        """A metrics-disabled deployment gets no collector and no thread."""
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=_metrics_settings(enabled=False),
            ),
            patch(
                "baldur.services.metrics.periodic_updater.start_domain_gauge_updater"
            ) as start_fn,
        ):
            bootstrap._start_domain_gauge_updater_if_enabled()

        start_fn.assert_not_called()

    def test_starter_prometheus_unavailable_skips_with_a_debug_record_only(
        self, enable_autostart, non_gunicorn_env
    ):
        """No metric surface to update is an honest non-start, not an error.

        Without this gate the collector module's import-time ``ImportError``
        lands in the starter's fail-soft catch and the thread silently never
        exists.
        """
        with (
            patch("baldur.metrics.registry.PROMETHEUS_AVAILABLE", False),
            patch(
                "baldur.services.metrics.periodic_updater.start_domain_gauge_updater"
            ) as start_fn,
            patch("baldur.bootstrap.logger") as mock_logger,
        ):
            bootstrap._start_domain_gauge_updater_if_enabled()

        start_fn.assert_not_called()
        assert "domain_gauge_updater.start_skipped" in _debug_events(mock_logger)
        mock_logger.error.assert_not_called()
        mock_logger.warning.assert_not_called()

    def test_starter_starts_the_collector_honoring_the_configured_interval(
        self, enable_autostart, non_gunicorn_env
    ):
        """``BALDUR_METRICS_COLLECTION_INTERVAL_SECONDS`` reaches the thread.

        The starter is the sole production first-caller of the singleton
        accessor, which captures the cadence at that first call.
        """
        from baldur.services.metrics.periodic_updater import get_domain_gauge_updater

        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=_metrics_settings(collection_interval_seconds=12.0),
            ),
            # The collector's tick body reads the repository; the starter's
            # contract is the thread and its cadence, not what one tick writes.
            patch("baldur.services.metrics.updaters.collect_all_metrics"),
        ):
            bootstrap._start_domain_gauge_updater_if_enabled()

        updater = get_domain_gauge_updater()
        assert updater._running is True
        assert updater._interval == 12.0

    def test_starter_applies_no_jitter_when_jitter_is_disabled(
        self, enable_autostart, non_gunicorn_env
    ):
        """Jitter reuses the existing metrics settings — no dedicated knob."""
        from baldur.services.metrics.periodic_updater import get_domain_gauge_updater

        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=_metrics_settings(jitter_enabled=False),
            ),
            patch("baldur.services.metrics.updaters.collect_all_metrics"),
        ):
            bootstrap._start_domain_gauge_updater_if_enabled()

        assert get_domain_gauge_updater()._jitter_seconds == 0.0

    def test_starter_bounds_the_jitter_by_the_configured_maximum(
        self, enable_autostart, non_gunicorn_env
    ):
        """A multi-server restart spreads over the configured window only."""
        from baldur.services.metrics.periodic_updater import get_domain_gauge_updater

        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=_metrics_settings(
                    jitter_enabled=True, jitter_max_delay_seconds=30.0
                ),
            ),
            patch("baldur.services.metrics.updaters.collect_all_metrics"),
        ):
            bootstrap._start_domain_gauge_updater_if_enabled()

        assert 0.0 <= get_domain_gauge_updater()._jitter_seconds <= 30.0

    def test_starter_still_captures_the_interval_after_shutdown_registration(
        self, enable_autostart, non_gunicorn_env
    ):
        """``init()`` registers shutdown handlers first — the cadence must survive.

        The singleton accessor captures interval and jitter at its FIRST call,
        so any earlier ``init()`` step that resolved the collector would pin it
        to the fallback cadence and make the settings field inert.
        """
        from baldur.services.metrics.periodic_updater import get_domain_gauge_updater

        bootstrap._register_shutdown_handlers()

        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=_metrics_settings(collection_interval_seconds=12.0),
            ),
            patch("baldur.services.metrics.updaters.collect_all_metrics"),
        ):
            bootstrap._start_domain_gauge_updater_if_enabled()

        assert get_domain_gauge_updater()._interval == 12.0

    def test_starter_swallows_a_start_failure_without_raising(
        self, enable_autostart, non_gunicorn_env
    ):
        """A collector that cannot start must not take ``init()`` down with it."""
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=_metrics_settings(),
            ),
            patch(
                "baldur.services.metrics.periodic_updater.start_domain_gauge_updater",
                side_effect=RuntimeError("thread refused"),
            ),
            patch("baldur.bootstrap.logger") as mock_logger,
        ):
            bootstrap._start_domain_gauge_updater_if_enabled()

        mock_logger.warning.assert_called_once()
        assert (
            mock_logger.warning.call_args.args[0]
            == "baldur.domain_gauge_updater_start_failed"
        )
