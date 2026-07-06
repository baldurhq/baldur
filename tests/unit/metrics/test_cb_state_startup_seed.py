"""650 D1 — per-process circuit-breaker-state startup seed (bootstrap).

The ``baldur_circuit_breaker_state`` gauge is set only on an in-process
transition, so a freshly scraped serving process (admin-server / CLI / each
gunicorn worker) exposes no CB series until a real ``closed -> open`` fires and is
scraped in-process. 650 D1 adds a per-process, non-blocking startup seed
registered in ``_BACKGROUND_WORKER_STARTERS``:

- ``_seed_circuit_breaker_state()`` — the synchronous inner callable. Reuses
  ``update_circuit_breaker_gauges()`` to read every persisted breaker via
  ``get_all_states()`` and set each gauge to the breaker's *actual* current
  state (open=1, not a hardcoded 0), preserving the multi-process shared-state
  benefit. Best-effort: a repo read failure leaves the gauge empty and never
  raises (re-seeded at the next restart).
- ``_seed_circuit_breaker_state_if_enabled()`` — the scheduling wrapper. Gated by
  the ``BALDUR_CB_STATE_SEED_AUTOSTART`` test hatch, the ``is_gunicorn_master()``
  fork-safety skip, ``metrics.enabled``, and a once-per-process done-flag; then
  schedules the seed on a jittered daemon Timer.

Tests drive the inner callable synchronously (no Timer) and the wrapper's gating
branches with ``threading.Timer`` patched, so no daemon thread leaks into the
full suite (the leak the conftest autostart hatch guards against). The metrics
backend is deterministically prometheus here (conftest re-pins
``BALDUR_OBSERVABILITY_PROFILE=local`` per function), so the gauge surfaces in
``generate_latest()``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur import bootstrap
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateRepository,
)


def _exposition() -> str:
    """Current prometheus exposition text from the shared REGISTRY."""
    from prometheus_client import generate_latest

    return generate_latest().decode()


def _series_value(text: str, metric: str, **labels: str) -> float | None:
    """Return the float value of the ``metric`` sample whose label assignments
    contain all of ``labels``, or None when no such series is exposed."""
    needles = [f'{key}="{value}"' for key, value in labels.items()]
    for line in text.splitlines():
        if line.startswith(metric + "{") and all(n in line for n in needles):
            return float(line.rsplit(" ", 1)[-1])
    return None


def _mock_repo(states: list[CircuitBreakerStateData]) -> MagicMock:
    repo = MagicMock(spec=CircuitBreakerStateRepository)
    repo.get_all_states.return_value = states
    return repo


@pytest.fixture(autouse=True)
def _reset_seed_guard():
    """Reset the once-per-process done-flag around every test so the wrapper's
    idempotency branch is deterministic regardless of suite ordering."""
    bootstrap._reset_cb_state_seed()
    yield
    bootstrap._reset_cb_state_seed()


@pytest.fixture
def enable_autostart(monkeypatch):
    """Re-enable the autostart hatch (tests/conftest.py pins it to ``0`` so a
    stray ``init()`` never schedules the seed Timer)."""
    monkeypatch.setenv("BALDUR_CB_STATE_SEED_AUTOSTART", "1")


@pytest.fixture
def non_gunicorn_env(monkeypatch):
    """Strip the gunicorn env so ``is_gunicorn_master()`` returns False — the
    single-process admin-server / CLI / runserver model."""
    monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)


# =============================================================================
# Inner seed callable — reads the repo and sets the gauge to the actual state
# =============================================================================


class TestCBStateStartupSeedBehavior:
    """``_seed_circuit_breaker_state()`` seeds the gauge from the repo's actual
    current state via the reused ``update_circuit_breaker_gauges``."""

    @pytest.mark.parametrize(
        ("state", "expected_value"),
        [("closed", 0.0), ("open", 1.0), ("half_open", 2.0)],
    )
    def test_seed_sets_gauge_to_actual_state(self, state, expected_value):
        # Given a persisted breaker in `state`, under a label unique to this test.
        service = f"cb_seed_650_{state}"
        repo = _mock_repo([CircuitBreakerStateData(service_name=service, state=state)])

        # When the inner seed callable runs synchronously.
        with patch(
            "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
            return_value=repo,
        ):
            bootstrap._seed_circuit_breaker_state()

        # Then the gauge exposes the breaker's actual state (open=1, not a
        # hardcoded 0) without any in-process transition.
        value = _series_value(
            _exposition(), "baldur_circuit_breaker_state", service=service
        )
        assert value == expected_value

    def test_seed_covers_every_breaker_in_the_repo(self):
        """The seed sets a series for each persisted breaker (both-boards floor)."""
        states = [
            CircuitBreakerStateData(service_name="cb_seed_650_multi_a", state="open"),
            CircuitBreakerStateData(service_name="cb_seed_650_multi_b", state="closed"),
        ]

        with patch(
            "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
            return_value=_mock_repo(states),
        ):
            bootstrap._seed_circuit_breaker_state()

        text = _exposition()
        assert (
            _series_value(
                text, "baldur_circuit_breaker_state", service="cb_seed_650_multi_a"
            )
            == 1.0
        )
        assert (
            _series_value(
                text, "baldur_circuit_breaker_state", service="cb_seed_650_multi_b"
            )
            == 0.0
        )

    def test_seed_splits_composite_service_name_into_service_and_cell(self):
        """A composite CB key seeds the base-service + cell_id labels, mirroring
        ``update_circuit_breaker_gauges`` (the seed reuses it unchanged)."""
        from baldur.core.cb_namespace import COMPOSITE_KEY_SEPARATOR

        composite = f"cb_seed_650_payments{COMPOSITE_KEY_SEPARATOR}seoul"
        repo = _mock_repo(
            [CircuitBreakerStateData(service_name=composite, state="open")]
        )

        with patch(
            "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
            return_value=repo,
        ):
            bootstrap._seed_circuit_breaker_state()

        value = _series_value(
            _exposition(),
            "baldur_circuit_breaker_state",
            service="cb_seed_650_payments",
            cell_id="seoul",
        )
        assert value == 1.0

    def test_seed_is_fail_open_on_repo_read_failure(self):
        """R1(b): a boot-time ``get_all_states()`` failure leaves the gauge empty
        for this process and never raises (re-seeded at the next restart, and any
        transition still sets it via the original path)."""
        repo = MagicMock(spec=CircuitBreakerStateRepository)
        repo.get_all_states.side_effect = RuntimeError("redis down at boot")

        with patch(
            "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
            return_value=repo,
        ):
            bootstrap._seed_circuit_breaker_state()  # must not raise

    def test_seed_handles_empty_repo(self):
        """No persisted breakers -> the seed completes without raising."""
        with patch(
            "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
            return_value=_mock_repo([]),
        ):
            bootstrap._seed_circuit_breaker_state()  # must not raise


# =============================================================================
# Scheduling wrapper — gating branches (Timer patched, no thread leak)
# =============================================================================


class TestCBStateSeedSchedulingBehavior:
    """``_seed_circuit_breaker_state_if_enabled()`` schedules the daemon Timer
    only when every gate passes, and at most once per process."""

    @pytest.mark.parametrize("disabled_value", ["0", "false", "no"])
    def test_autostart_disabled_returns_before_scheduling(
        self, monkeypatch, disabled_value
    ):
        """An autostart escape-hatch value returns before scheduling and never
        flips the done-flag."""
        monkeypatch.setenv("BALDUR_CB_STATE_SEED_AUTOSTART", disabled_value)

        with patch("baldur.bootstrap.threading.Timer", autospec=True) as timer:
            bootstrap._seed_circuit_breaker_state_if_enabled()

        timer.assert_not_called()
        assert bootstrap._cb_state_seed_done is False

    def test_gunicorn_master_skips_scheduling(self, enable_autostart, monkeypatch):
        """Under the Gunicorn master the schedule is skipped (the Timer would not
        survive ``fork()``); the per-worker ``post_worker_init`` re-runs it."""
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)

        with patch("baldur.bootstrap.threading.Timer", autospec=True) as timer:
            bootstrap._seed_circuit_breaker_state_if_enabled()

        timer.assert_not_called()
        assert bootstrap._cb_state_seed_done is False

    def test_metrics_disabled_skips_scheduling(
        self, enable_autostart, non_gunicorn_env
    ):
        """``metrics.enabled=False`` stops before scheduling and before the flag."""
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                return_value=MagicMock(enabled=False),
            ),
            patch("baldur.bootstrap.threading.Timer", autospec=True) as timer,
        ):
            bootstrap._seed_circuit_breaker_state_if_enabled()

        timer.assert_not_called()
        assert bootstrap._cb_state_seed_done is False

    def test_enabled_schedules_daemon_timer_once_with_zero_jitter(
        self, enable_autostart, non_gunicorn_env
    ):
        """All gates pass + jitter disabled -> a single daemon Timer scheduled at
        delay 0.0 targeting the inner seed callable, and the done-flag is set."""
        settings = MagicMock(
            enabled=True, jitter_enabled=False, jitter_max_delay_seconds=30.0
        )
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings", return_value=settings
            ),
            patch("baldur.bootstrap.threading.Timer", autospec=True) as timer,
        ):
            bootstrap._seed_circuit_breaker_state_if_enabled()

        timer.assert_called_once_with(0.0, bootstrap._seed_circuit_breaker_state)
        assert timer.return_value.daemon is True
        timer.return_value.start.assert_called_once()
        assert bootstrap._cb_state_seed_done is True

    def test_enabled_jitter_delay_within_configured_bounds(
        self, enable_autostart, non_gunicorn_env
    ):
        """Jitter enabled -> the Timer delay is drawn from
        ``[0, jitter_max_delay_seconds]`` (no dedicated env var; reuses
        ``MetricsSettings.jitter_*``)."""
        settings = MagicMock(
            enabled=True, jitter_enabled=True, jitter_max_delay_seconds=10.0
        )
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings", return_value=settings
            ),
            patch("baldur.bootstrap.threading.Timer", autospec=True) as timer,
        ):
            bootstrap._seed_circuit_breaker_state_if_enabled()

        timer.assert_called_once()
        delay = timer.call_args.args[0]
        assert 0.0 <= delay <= 10.0

    def test_double_invocation_schedules_at_most_once(
        self, enable_autostart, non_gunicorn_env
    ):
        """The once-per-process done-flag makes a double invocation (Django
        ``init()`` per worker + ``post_worker_init``) schedule at most once."""
        settings = MagicMock(
            enabled=True, jitter_enabled=False, jitter_max_delay_seconds=30.0
        )
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings", return_value=settings
            ),
            patch("baldur.bootstrap.threading.Timer", autospec=True) as timer,
        ):
            bootstrap._seed_circuit_breaker_state_if_enabled()
            bootstrap._seed_circuit_breaker_state_if_enabled()

        timer.assert_called_once()

    def test_scheduling_is_fail_soft_on_settings_crash(
        self, enable_autostart, non_gunicorn_env
    ):
        """A crash inside the wrapper body is swallowed — ``init()`` must
        continue — and no Timer is scheduled."""
        with (
            patch(
                "baldur.settings.metrics.get_metrics_settings",
                side_effect=RuntimeError("settings boom"),
            ),
            patch("baldur.bootstrap.threading.Timer", autospec=True) as timer,
        ):
            bootstrap._seed_circuit_breaker_state_if_enabled()  # must not raise

        timer.assert_not_called()
