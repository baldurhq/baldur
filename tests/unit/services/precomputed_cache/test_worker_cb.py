"""
PrecomputedCacheWorker CB/backoff/jitter/invalidation tests (doc 445).

Covers:
- Contract: __init__ defaults, cb_service property, passive_health effective_interval
- Behavior: _do_refresh CB check/recording/backoff, _schedule_refresh jitter,
  _on_cache_invalidated handler, start() lazy init + cold start jitter,
  stop→start idempotency
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.precomputed_cache.worker import PrecomputedCacheWorker


@pytest.fixture
def worker():
    return PrecomputedCacheWorker()


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.l3_cb_enabled = True
    s.l3_cb_failure_threshold = 3
    s.l3_cb_recovery_timeout = 30
    s.jitter_enabled = True
    s.refresh_interval_seconds = 10.0
    s.backoff_max_delay_seconds = 300.0
    return s


class TestWorkerInitContract:
    """Contract verification for new __init__ attributes."""

    def test_cb_service_initially_none(self, worker):
        """_cb_service is None before start()."""
        assert worker._cb_service is None

    def test_backoff_initially_none(self, worker):
        """_backoff is None before start()."""
        assert worker._backoff is None

    def test_consecutive_failures_initially_zero(self, worker):
        """_consecutive_all_failures starts at 0."""
        assert worker._consecutive_all_failures == 0

    def test_effective_interval_initially_none(self, worker):
        """_current_effective_interval is None before start()."""
        assert worker._current_effective_interval is None

    def test_cb_service_property_returns_none_before_start(self, worker):
        """cb_service property returns None before start()."""
        assert worker.cb_service is None


class TestWorkerPassiveHealth445Contract:
    """Contract for get_passive_health effective_interval_seconds field."""

    def test_effective_interval_key_present(self, worker):
        """get_passive_health() includes effective_interval_seconds."""
        health = worker.get_passive_health()
        assert "effective_interval_seconds" in health

    def test_effective_interval_defaults_to_refresh_interval(self, worker):
        """Before start, effective_interval falls back to refresh_interval."""
        health = worker.get_passive_health()
        assert (
            health["effective_interval_seconds"] == health["refresh_interval_seconds"]
        )


class TestWorkerStartBehavior:
    """Behavior verification for start() lazy initialization."""

    def test_start_creates_cb_service_when_enabled(self, worker, mock_settings):
        """start() initializes _cb_service when l3_cb_enabled=True."""
        with (
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                return_value=mock_settings,
            ),
            patch.object(worker, "_schedule_refresh"),
            patch.object(worker, "_register_invalidation_handler"),
        ):
            worker.start()

        assert worker._cb_service is not None
        assert worker._backoff is not None

    def test_start_skips_cb_when_disabled(self, worker, mock_settings):
        """start() does not create CB when l3_cb_enabled=False."""
        mock_settings.l3_cb_enabled = False
        mock_settings.jitter_enabled = False

        with (
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                return_value=mock_settings,
            ),
            patch.object(worker, "_schedule_refresh"),
            patch.object(worker, "_register_invalidation_handler"),
        ):
            worker.start()

        assert worker._cb_service is None
        assert worker._backoff is None

    def test_stop_start_resets_mutable_state(self, worker, mock_settings):
        """stop() → start() resets consecutive_all_failures and effective_interval."""
        worker._consecutive_all_failures = 5
        worker._current_effective_interval = 120.0

        with (
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                return_value=mock_settings,
            ),
            patch.object(worker, "_schedule_refresh"),
            patch.object(worker, "_register_invalidation_handler"),
        ):
            worker.start()

        assert worker._consecutive_all_failures == 0
        assert worker._current_effective_interval is None

    def test_start_cold_start_jitter_range(self, worker, mock_settings):
        """start() passes initial_delay in [0, refresh_interval) to _schedule_refresh."""
        captured_delays = []

        def capture_schedule(delay=None):
            captured_delays.append(delay)

        with (
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                return_value=mock_settings,
            ),
            patch.object(worker, "_schedule_refresh", side_effect=capture_schedule),
            patch.object(worker, "_register_invalidation_handler"),
        ):
            worker.start()

        assert len(captured_delays) == 1
        delay = captured_delays[0]
        assert delay is not None
        assert 0 <= delay < mock_settings.refresh_interval_seconds


class TestDoRefreshCBBehavior:
    """Behavior verification for _do_refresh CB integration."""

    def test_cb_open_skips_all_keys(self, worker):
        """When CB is OPEN, _do_refresh skips all compute functions."""
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = False
        worker._cb_service = mock_cb
        worker._running = True
        worker._compute_functions = {"k1": lambda: {"val": 1}}

        with patch.object(worker, "_schedule_refresh"):
            worker._do_refresh()

        mock_cb.should_allow.assert_called_once_with("precomputed_cache_compute")
        assert worker._consecutive_all_failures == 1

    def test_success_records_cb_success(self, worker):
        """Successful compute_fn records CB success."""
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = True
        worker._cb_service = mock_cb
        worker._running = True
        worker._compute_functions = {"k1": lambda: {"val": 1}}

        with (
            patch.object(worker, "_schedule_refresh"),
            patch("baldur.services.precomputed_cache.worker.check_l1_l2_drift"),
            patch("baldur.services.precomputed_cache.worker.record_cache_refresh"),
        ):
            worker._do_refresh()

        mock_cb.record_success.assert_called_once_with("precomputed_cache_compute")

    def test_failure_records_cb_failure(self, worker):
        """Failed compute_fn records CB failure."""
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = True
        worker._cb_service = mock_cb
        worker._running = True
        worker._compute_functions = {
            "k1": MagicMock(side_effect=RuntimeError("db down"))
        }

        with (
            patch.object(worker, "_schedule_refresh"),
            patch("baldur.services.precomputed_cache.worker.check_l1_l2_drift"),
            patch("baldur.services.precomputed_cache.worker.record_cache_refresh"),
        ):
            worker._do_refresh()

        mock_cb.record_failure.assert_called_once_with("precomputed_cache_compute")

    def test_all_failure_increments_consecutive_counter(self, worker):
        """All keys failing increments _consecutive_all_failures."""
        worker._running = True
        worker._compute_functions = {"k1": MagicMock(side_effect=RuntimeError("fail"))}

        with (
            patch.object(worker, "_schedule_refresh"),
            patch("baldur.services.precomputed_cache.worker.check_l1_l2_drift"),
            patch("baldur.services.precomputed_cache.worker.record_cache_refresh"),
        ):
            worker._do_refresh()

        assert worker._consecutive_all_failures == 1

    def test_partial_success_resets_consecutive_counter(self, worker):
        """At least one success resets _consecutive_all_failures to 0."""
        worker._running = True
        worker._consecutive_all_failures = 3

        worker._compute_functions = {
            "fail_key": MagicMock(side_effect=RuntimeError("fail")),
            "ok_key": MagicMock(return_value={"val": 1}),
        }

        with (
            patch.object(worker, "_schedule_refresh"),
            patch("baldur.services.precomputed_cache.worker.check_l1_l2_drift"),
            patch("baldur.services.precomputed_cache.worker.record_cache_refresh"),
        ):
            worker._do_refresh()

        assert worker._consecutive_all_failures == 0


class TestDoRefreshBackoffBehavior:
    """Behavior verification for _do_refresh backoff scheduling."""

    def test_all_failure_with_backoff_uses_calculated_delay(self, worker):
        """All-failure with backoff passes calculated delay to _schedule_refresh."""
        mock_backoff = MagicMock()
        mock_backoff.calculate.return_value = 42.0
        worker._backoff = mock_backoff
        worker._running = True
        worker._compute_functions = {"k1": MagicMock(side_effect=RuntimeError("fail"))}

        captured_delays = []

        def capture(delay=None):
            captured_delays.append(delay)

        with (
            patch.object(worker, "_schedule_refresh", side_effect=capture),
            patch("baldur.services.precomputed_cache.worker.check_l1_l2_drift"),
            patch("baldur.services.precomputed_cache.worker.record_cache_refresh"),
        ):
            worker._do_refresh()

        assert captured_delays == [42.0]
        mock_backoff.calculate.assert_called_once_with(1)

    def test_success_after_backoff_resets_backoff(self, worker):
        """Success after backoff calls backoff.reset()."""
        mock_backoff = MagicMock()
        worker._backoff = mock_backoff
        worker._running = True
        worker._consecutive_all_failures = 2
        worker._compute_functions = {"k1": MagicMock(return_value={"val": 1})}

        with (
            patch.object(worker, "_schedule_refresh"),
            patch("baldur.services.precomputed_cache.worker.check_l1_l2_drift"),
            patch("baldur.services.precomputed_cache.worker.record_cache_refresh"),
        ):
            worker._do_refresh()

        mock_backoff.reset.assert_called_once()

    def test_success_without_prior_backoff_does_not_log_recovered(self, worker):
        """Success when not in backoff state does not log backoff_recovered."""
        worker._running = True
        worker._consecutive_all_failures = 0
        worker._compute_functions = {"k1": MagicMock(return_value={"val": 1})}

        with (
            patch.object(worker, "_schedule_refresh"),
            patch("baldur.services.precomputed_cache.worker.check_l1_l2_drift"),
            patch("baldur.services.precomputed_cache.worker.record_cache_refresh"),
            patch("baldur.services.precomputed_cache.worker.logger") as mock_logger,
        ):
            worker._do_refresh()

        info_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c[0][0] == "precomputed_cache.backoff_recovered"
        ]
        assert len(info_calls) == 0


class TestDoRefreshCBOpenBackoffCapBehavior:
    """Behavior verification for CB OPEN backoff delay capped at recovery_timeout."""

    def test_backoff_delay_capped_at_recovery_timeout(self, worker, mock_settings):
        """When backoff delay > recovery_timeout, delay is capped at recovery_timeout."""
        # Given
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = False
        mock_backoff = MagicMock()
        mock_backoff.calculate.return_value = 300.0  # far exceeds recovery_timeout=30
        worker._cb_service = mock_cb
        worker._backoff = mock_backoff
        worker._running = True
        worker._compute_functions = {"k1": lambda: {"val": 1}}

        captured_delays = []

        def capture(delay=None):
            captured_delays.append(delay)

        # When
        with (
            patch.object(worker, "_schedule_refresh", side_effect=capture),
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                return_value=mock_settings,
            ),
        ):
            worker._do_refresh()

        # Then — delay capped at recovery_timeout (30)
        assert len(captured_delays) == 1
        assert captured_delays[0] == float(mock_settings.l3_cb_recovery_timeout)

    def test_backoff_delay_below_recovery_timeout_unchanged(
        self, worker, mock_settings
    ):
        """When backoff delay < recovery_timeout, delay is used as-is."""
        # Given
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = False
        mock_backoff = MagicMock()
        mock_backoff.calculate.return_value = 15.0  # below recovery_timeout=30
        worker._cb_service = mock_cb
        worker._backoff = mock_backoff
        worker._running = True
        worker._compute_functions = {"k1": lambda: {"val": 1}}

        captured_delays = []

        def capture(delay=None):
            captured_delays.append(delay)

        # When
        with (
            patch.object(worker, "_schedule_refresh", side_effect=capture),
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                return_value=mock_settings,
            ),
        ):
            worker._do_refresh()

        # Then — original delay preserved
        assert len(captured_delays) == 1
        assert captured_delays[0] == 15.0

    def test_backoff_delay_equal_to_recovery_timeout(self, worker, mock_settings):
        """When backoff delay == recovery_timeout, delay equals recovery_timeout."""
        mock_cb = MagicMock()
        mock_cb.should_allow.return_value = False
        mock_backoff = MagicMock()
        mock_backoff.calculate.return_value = 30.0  # exactly recovery_timeout
        worker._cb_service = mock_cb
        worker._backoff = mock_backoff
        worker._running = True
        worker._compute_functions = {"k1": lambda: {"val": 1}}

        captured_delays = []

        def capture(delay=None):
            captured_delays.append(delay)

        with (
            patch.object(worker, "_schedule_refresh", side_effect=capture),
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                return_value=mock_settings,
            ),
        ):
            worker._do_refresh()

        assert captured_delays[0] == 30.0


class TestScheduleRefreshBehavior:
    """Behavior verification for _schedule_refresh jitter."""

    def test_explicit_delay_sets_effective_interval(self, worker):
        """_schedule_refresh(delay=X) sets _current_effective_interval to X."""
        worker._running = True

        with patch(
            "baldur.services.precomputed_cache.worker.threading.Timer"
        ) as mock_timer:
            mock_timer.return_value = MagicMock()
            worker._schedule_refresh(delay=25.0)

        assert worker._current_effective_interval == 25.0

    def test_none_delay_adds_jitter(self, worker):
        """_schedule_refresh(delay=None) adds jitter to base interval."""
        worker._running = True

        with (
            patch(
                "baldur.services.precomputed_cache.worker.threading.Timer"
            ) as mock_timer,
            patch(
                "baldur.services.precomputed_cache.worker._get_refresh_interval",
                return_value=10.0,
            ),
            patch(
                "baldur.core.adaptive_jitter.AdaptiveJitter.calculate",
                return_value=0.05,
            ),
        ):
            mock_timer.return_value = MagicMock()
            worker._schedule_refresh()

        assert worker._current_effective_interval == pytest.approx(10.05, abs=0.01)

    def test_not_running_skips_scheduling(self, worker):
        """_schedule_refresh does nothing when not running."""
        worker._running = False
        worker._schedule_refresh(delay=10.0)
        assert worker._timer is None

    def test_non_import_error_logs_debug(self, worker):
        """Non-ImportError from AdaptiveJitter.calculate() logs debug and falls back to 0."""
        worker._running = True

        with (
            patch(
                "baldur.services.precomputed_cache.worker.threading.Timer"
            ) as mock_timer,
            patch(
                "baldur.services.precomputed_cache.worker._get_refresh_interval",
                return_value=10.0,
            ),
            patch(
                "baldur.core.adaptive_jitter.AdaptiveJitter.calculate",
                side_effect=ZeroDivisionError("bug"),
            ),
            patch("baldur.services.precomputed_cache.worker.logger") as mock_logger,
        ):
            mock_timer.return_value = MagicMock()
            worker._schedule_refresh()

        # Jitter falls back to 0 → delay = base (10.0)
        assert worker._current_effective_interval == pytest.approx(10.0)
        debug_calls = [
            c
            for c in mock_logger.debug.call_args_list
            if c[0][0] == "precomputed_cache.jitter_calculation_failed"
        ]
        assert len(debug_calls) == 1

    def test_import_error_silent_fallback(self, worker):
        """ImportError from AdaptiveJitter does not log jitter_calculation_failed."""
        worker._running = True

        with (
            patch(
                "baldur.services.precomputed_cache.worker.threading.Timer"
            ) as mock_timer,
            patch(
                "baldur.services.precomputed_cache.worker._get_refresh_interval",
                return_value=10.0,
            ),
            patch(
                "baldur.core.adaptive_jitter.AdaptiveJitter.calculate",
                side_effect=ImportError("no module"),
            ),
            patch("baldur.services.precomputed_cache.worker.logger") as mock_logger,
        ):
            mock_timer.return_value = MagicMock()
            worker._schedule_refresh()

        assert worker._current_effective_interval == pytest.approx(10.0)
        debug_calls = [
            c
            for c in mock_logger.debug.call_args_list
            if c[0][0] == "precomputed_cache.jitter_calculation_failed"
        ]
        assert len(debug_calls) == 0


class TestOnCacheInvalidatedBehavior:
    """Behavior verification for _on_cache_invalidated handler."""

    def test_specific_key_invalidation(self, worker):
        """Event with cache_key invalidates only that key."""
        from baldur.services.precomputed_cache.l1_cache import L1Cache

        mock_l1 = MagicMock(spec=L1Cache)
        event = MagicMock()
        event.data = {"cache_key": "stats_cache"}

        with patch("baldur.services.precomputed_cache.worker._l1_cache", mock_l1):
            worker._on_cache_invalidated(event)

        mock_l1.invalidate.assert_called_once_with("stats_cache")

    def test_no_cache_key_clears_all(self, worker):
        """Event with cache_key=None clears entire L1."""
        from baldur.services.precomputed_cache.l1_cache import L1Cache

        mock_l1 = MagicMock(spec=L1Cache)
        event = MagicMock()
        event.data = {"cache_key": None}

        with patch("baldur.services.precomputed_cache.worker._l1_cache", mock_l1):
            worker._on_cache_invalidated(event)

        mock_l1.clear.assert_called_once()

    def test_empty_dict_data_clears_all(self, worker):
        """Event with empty data dict (no cache_key key) clears entire L1."""
        from baldur.services.precomputed_cache.l1_cache import L1Cache

        mock_l1 = MagicMock(spec=L1Cache)
        event = MagicMock()
        event.data = {}

        with patch("baldur.services.precomputed_cache.worker._l1_cache", mock_l1):
            worker._on_cache_invalidated(event)

        mock_l1.clear.assert_called_once()

    def test_empty_string_cache_key_invalidates_not_clears(self, worker):
        """Event with cache_key='' invalidates that key, does not clear all."""
        from baldur.services.precomputed_cache.l1_cache import L1Cache

        mock_l1 = MagicMock(spec=L1Cache)
        event = MagicMock()
        event.data = {"cache_key": ""}

        with patch("baldur.services.precomputed_cache.worker._l1_cache", mock_l1):
            worker._on_cache_invalidated(event)

        mock_l1.invalidate.assert_called_once_with("")
        mock_l1.clear.assert_not_called()

    def test_event_without_data_attr_clears_all(self, worker):
        """Event without data attribute clears entire L1."""
        from baldur.services.precomputed_cache.l1_cache import L1Cache

        mock_l1 = MagicMock(spec=L1Cache)
        event = MagicMock(spec=[])  # no data attr

        with patch("baldur.services.precomputed_cache.worker._l1_cache", mock_l1):
            worker._on_cache_invalidated(event)

        mock_l1.clear.assert_called_once()

    def test_handler_exception_does_not_propagate(self, worker):
        """Exception in handler is caught and logged."""
        event = MagicMock()
        event.data = {"cache_key": "k1"}

        with patch(
            "baldur.services.precomputed_cache.worker._l1_cache",
        ) as mock_l1:
            mock_l1.invalidate.side_effect = RuntimeError("boom")
            worker._on_cache_invalidated(event)
