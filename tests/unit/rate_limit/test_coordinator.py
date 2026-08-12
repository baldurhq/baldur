"""
RateLimitCoordinator unit tests.

Covers:
- 429 event emission
- Exponential backoff
- Debounce window
- Canary request mode
- Cooldown state
- retry_after header precedence
- Fail-open behavior
- Metric recording (429 counter, cooldown values, wait/deferral decision)
- rate_limit_aware decorator
- on_success, _schedule_cooldown_end scheduling
"""

from __future__ import annotations

import itertools
import math
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time
from structlog.testing import capture_logs

from tests.factories.time_helpers import mock_sleep
from tests.unit.rate_limit.conftest import (
    DEFAULT_BACKOFF_MULTIPLIER,
    DEFAULT_BASE_DELAY,
    DEFAULT_DEBOUNCE_WINDOW,
    DEFAULT_MAX_DELAY,
    DEFAULT_RETRY_AFTER,
    MockInMemoryRateLimitStorage,
    make_mock_event_bus,
)

# =============================================================================
# Metric-reading helpers
# =============================================================================
# The rate-limit series are module-level collectors on the process-global
# prometheus REGISTRY, so their values survive every test in the same worker.
# Each metric assertion below therefore records under a key nothing else has
# touched, which turns the read into an absolute value instead of a delta and
# lets a negative assertion distinguish "never recorded" (absent sample) from
# "recorded zero".

_METRIC_KEY_SEQUENCE = itertools.count()


def _unique_key(prefix: str) -> str:
    """A rate-limit key nothing else in this worker has recorded under."""
    return f"{prefix}_{next(_METRIC_KEY_SEQUENCE)}"


def _sample(name: str, **labels: str) -> float | None:
    """Read one prometheus sample, or None when that series was never recorded."""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels)


class _BrokenMetric:
    """A metric double whose label lookup raises, modelling a registry fault.

    A plain object rather than a Mock: the fail-open contract is "the helper
    returns instead of propagating", so the double only has to be able to raise
    from the one attribute the helper touches.
    """

    def labels(self, **_kwargs: str) -> None:
        raise RuntimeError("metric registry corrupted")


# =============================================================================
# Event emission
# =============================================================================


class TestRateLimitCoordinatorEventEmission:
    """RateLimitCoordinator event emission tests."""

    def test_on_rate_limited_emits_429_event(self, mock_storage):
        """on_rate_limited() emits a RATE_LIMIT_429 event."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            base_delay=DEFAULT_RETRY_AFTER,
            debounce_window_seconds=DEFAULT_DEBOUNCE_WINDOW,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        mock_bus, emitted_events = make_mock_event_bus()

        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_get_bus.return_value = mock_bus
            coordinator.on_rate_limited("payment_api", retry_after=5)

        rate_limit_events = [
            e for e in emitted_events if "RATE_LIMIT_429" in e["event_type"]
        ]
        assert len(rate_limit_events) >= 1

        event_data = rate_limit_events[0]["data"]
        assert event_data["key"] == "payment_api"
        assert event_data["consecutive_429s"] == 1

    @pytest.mark.parametrize(
        ("call_index", "expected_multiplier"),
        [
            (0, 1),  # 2^0 = 1
            (1, 2),  # 2^1 = 2
            (2, 4),  # 2^2 = 4
        ],
        ids=["first-429", "second-429", "third-429"],
    )
    def test_on_rate_limited_calculates_exponential_backoff(
        self, mock_storage, call_index, expected_multiplier
    ):
        """Consecutive 429s escalate the cooldown exponentially."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        base = DEFAULT_BASE_DELAY
        config = RateLimitCoordinatorConfig(
            base_delay=base,
            default_retry_after=base,
            backoff_multiplier=DEFAULT_BACKOFF_MULTIPLIER,
            max_delay=DEFAULT_MAX_DELAY,
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        delay = None
        for _ in range(call_index + 1):
            delay = coordinator.on_rate_limited("test_api")

        expected = base * expected_multiplier
        assert delay == pytest.approx(expected, rel=0.1)


# =============================================================================
# Debouncing
# =============================================================================


class TestRateLimitCoordinatorDebouncing:
    """RateLimitCoordinator debouncing tests."""

    @freeze_time("2026-02-06 12:00:00")
    def test_debounce_window_prevents_duplicate_events(self, mock_storage):
        """Duplicate events within the window are suppressed."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            debounce_window_seconds=DEFAULT_DEBOUNCE_WINDOW
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        assert coordinator._should_emit_event("test_api") is True
        assert coordinator._should_emit_event("test_api") is False

    @freeze_time("2026-02-06 12:00:00")
    def test_debounce_window_expires_after_timeout(self, mock_storage):
        """Emission is allowed again once the window expires."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        window = DEFAULT_DEBOUNCE_WINDOW
        config = RateLimitCoordinatorConfig(debounce_window_seconds=window)
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        assert coordinator._should_emit_event("test_api") is True

        expired_time = f"2026-02-06 12:00:{int(window) + 1:02d}"
        with freeze_time(expired_time):
            assert coordinator._should_emit_event("test_api") is True

    @freeze_time("2026-02-06 12:00:00")
    def test_debounce_tracks_keys_independently(self, mock_storage):
        """Each key is debounced independently."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            debounce_window_seconds=DEFAULT_DEBOUNCE_WINDOW
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        assert coordinator._should_emit_event("api_a") is True
        assert coordinator._should_emit_event("api_b") is True
        assert coordinator._should_emit_event("api_a") is False

    def test_debounce_skips_event(self, mock_storage):
        """A second 429 inside the window emits no event."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            debounce_window_seconds=10.0,
            jitter_percent=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        emit_count = 0

        def count_emit(event_type, data, source, priority):
            nonlocal emit_count
            emit_count += 1
            return 1

        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_bus.emit = count_emit
            mock_get_bus.return_value = mock_bus

            coordinator.on_rate_limited("test_api")
            first_count = emit_count
            coordinator.on_rate_limited("test_api")

        assert emit_count == first_count

    @pytest.mark.parametrize("burst_size", [1, 3], ids=["single", "burst"])
    def test_debounce_suppresses_the_event_but_the_metric_counts_every_429(
        self, mock_storage, burst_size
    ):
        """N 429s inside one window: N counter increments, exactly one event.

        The counter is deliberately NOT debounced. Debouncing it would flatten a
        storm into a single tick, and a flattened counter is indistinguishable
        from a storm abating — the opposite conclusion.
        """
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        # Given a window wide enough that the whole burst lands inside it
        key = _unique_key("debounce_metric")
        config = RateLimitCoordinatorConfig(
            debounce_window_seconds=DEFAULT_DEBOUNCE_WINDOW,
            jitter_percent=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)
        mock_bus, emitted_events = make_mock_event_bus()

        # When the burst arrives (cooldown-end scheduling stubbed out so the
        # emit path leaves no live Timer thread behind)
        with (
            patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus),
            patch.object(coordinator, "_schedule_cooldown_end_event"),
        ):
            for _ in range(burst_size):
                coordinator.on_rate_limited(key)

        # Then every 429 is counted ...
        assert _sample(
            "baldur_rate_limit_429_total", key=key, status_code="429"
        ) == float(burst_size)
        # ... while the window emits exactly one event regardless of burst size
        assert len(emitted_events) == 1


# =============================================================================
# Canary requests
# =============================================================================


class TestRateLimitCoordinatorCanary:
    """RateLimitCoordinator canary request tests."""

    def test_wait_if_needed_returns_canary_after_429(self, mock_storage):
        """The first request after a 429 runs in canary mode."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        mock_storage.increment_consecutive_429s("test_api")
        result = coordinator.wait_if_needed("test_api")
        assert result.is_canary is True

    def test_on_success_clears_canary_state(self, mock_storage):
        """Canary state is cleared after a success."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        mock_storage.increment_consecutive_429s("test_api")
        result1 = coordinator.wait_if_needed("test_api")
        assert result1.is_canary is True

        coordinator.on_success("test_api")

        result2 = coordinator.wait_if_needed("test_api")
        assert result2.is_canary is False


# =============================================================================
# Cooldown state
# =============================================================================


class TestRateLimitCoordinatorCooldown:
    """RateLimitCoordinator cooldown tests."""

    def test_cooldown_state_detection(self):
        """An active cooldown is detected with its remaining time."""
        storage = MockInMemoryRateLimitStorage()

        cooldown_duration = 10.0
        cooldown_until = time.time() + cooldown_duration
        storage.set_cooldown("test_api", cooldown_until)
        storage.increment_consecutive_429s("test_api")

        state = storage.get_state("test_api")
        assert state.is_in_cooldown is True
        assert 0 < state.remaining_cooldown <= cooldown_duration

    def test_cooldown_expired(self):
        """An expired cooldown reports no remaining time."""
        storage = MockInMemoryRateLimitStorage()

        storage.set_cooldown("test_api", time.time() - 5.0)

        state = storage.get_state("test_api")
        assert state.is_in_cooldown is False
        assert state.remaining_cooldown == 0.0


# =============================================================================
# Fail-open behavior
# =============================================================================


class TestEmitRateLimitEventFailOpen:
    """_emit_rate_limit_event fail-open tests."""

    def test_emit_survives_import_error(self):
        """An EventBus import failure passes without raising (fail-open)."""
        from baldur.services.rate_limit_coordinator import _emit_rate_limit_event

        with patch(
            "baldur.services.rate_limit_coordinator._emit_rate_limit_event",
            wraps=_emit_rate_limit_event,
        ):
            with patch(
                "baldur.services.event_bus.get_event_bus",
                side_effect=ImportError("no module"),
            ):
                _emit_rate_limit_event("RATE_LIMIT_429", {"key": "test"})

    def test_emit_survives_generic_exception(self):
        """An emit-time exception passes without raising (fail-open)."""
        from baldur.services.rate_limit_coordinator import _emit_rate_limit_event

        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=RuntimeError("bus broken"),
        ):
            _emit_rate_limit_event("RATE_LIMIT_429", {"key": "test"})

    def test_emit_unknown_event_type_does_not_crash(self):
        """An unknown EventType warns and returns without emitting."""
        from baldur.services.rate_limit_coordinator import _emit_rate_limit_event

        mock_bus = MagicMock()
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            _emit_rate_limit_event("NONEXISTENT_EVENT_TYPE", {"key": "test"})

        mock_bus.emit.assert_not_called()


# =============================================================================
# Metric recording
# =============================================================================


class TestRecordRateLimitMetrics:
    """_record_rate_limit_429 / _record_rate_limit_cooldown recording tests."""

    def test_records_429_counter(self):
        """rate_limit_429_total is incremented."""
        from baldur.services.rate_limit_coordinator import (
            _record_rate_limit_429,
        )

        mock_counter = MagicMock()
        mock_labels = MagicMock()
        mock_counter.labels.return_value = mock_labels

        with patch(
            "baldur.services.metrics.definitions.rate_limit_429_total",
            mock_counter,
        ):
            _record_rate_limit_429(key="payment_api", status_code=429)

        mock_counter.labels.assert_called_with(key="payment_api", status_code="429")
        mock_labels.inc.assert_called_once()

    def test_records_cooldown_histogram(self):
        """rate_limit_cooldown_seconds observes the computed cooldown."""
        from baldur.services.rate_limit_coordinator import (
            _record_rate_limit_cooldown,
        )

        mock_histogram = MagicMock()
        mock_hist_labels = MagicMock()
        mock_histogram.labels.return_value = mock_hist_labels
        mock_gauge = MagicMock()
        mock_gauge.labels.return_value = MagicMock()

        cooldown_value = 15.5
        with patch(
            "baldur.services.metrics.definitions.rate_limit_cooldown_seconds",
            mock_histogram,
        ):
            with patch(
                "baldur.services.metrics.definitions.rate_limit_consecutive_429s",
                mock_gauge,
            ):
                _record_rate_limit_cooldown(
                    key="test", cooldown_seconds=cooldown_value, consecutive_429s=1
                )

        mock_histogram.labels.assert_called_with(key="test")
        mock_hist_labels.observe.assert_called_with(cooldown_value)

    def test_records_consecutive_gauge(self):
        """rate_limit_consecutive_429s is set to the consecutive count."""
        from baldur.services.rate_limit_coordinator import (
            _record_rate_limit_cooldown,
        )

        mock_histogram = MagicMock()
        mock_histogram.labels.return_value = MagicMock()
        mock_gauge = MagicMock()
        mock_gauge_labels = MagicMock()
        mock_gauge.labels.return_value = mock_gauge_labels

        consecutive = 5
        with patch(
            "baldur.services.metrics.definitions.rate_limit_cooldown_seconds",
            mock_histogram,
        ):
            with patch(
                "baldur.services.metrics.definitions.rate_limit_consecutive_429s",
                mock_gauge,
            ):
                _record_rate_limit_cooldown(
                    key="test", cooldown_seconds=1.0, consecutive_429s=consecutive
                )

        mock_gauge.labels.assert_called_with(key="test")
        mock_gauge_labels.set.assert_called_with(consecutive)

    def test_record_429_survives_a_missing_metrics_module(self):
        """A missing definitions module is a no-op, not an ImportError.

        The counter is ``on_rate_limited``'s first statement, so a metrics
        fault here would abort the cooldown the caller is about to receive.
        """
        import sys

        from baldur.services.rate_limit_coordinator import _record_rate_limit_429

        with patch.dict(sys.modules, {"baldur.services.metrics.definitions": None}):
            _record_rate_limit_429(key="k")

    def test_record_cooldown_survives_a_missing_metrics_module(self):
        """A missing definitions module is a no-op, not an ImportError."""
        import sys

        from baldur.services.rate_limit_coordinator import _record_rate_limit_cooldown

        with patch.dict(sys.modules, {"baldur.services.metrics.definitions": None}):
            _record_rate_limit_cooldown(
                key="k", cooldown_seconds=1.0, consecutive_429s=1
            )

    def test_record_429_survives_a_broken_metric(self):
        """A registry fault at the label lookup is swallowed."""
        from baldur.services.rate_limit_coordinator import _record_rate_limit_429

        with patch(
            "baldur.services.metrics.definitions.rate_limit_429_total",
            _BrokenMetric(),
        ):
            _record_rate_limit_429(key="k")

    def test_record_cooldown_survives_a_broken_metric(self):
        """A registry fault at the label lookup is swallowed."""
        from baldur.services.rate_limit_coordinator import _record_rate_limit_cooldown

        with patch(
            "baldur.services.metrics.definitions.rate_limit_cooldown_seconds",
            _BrokenMetric(),
        ):
            _record_rate_limit_cooldown(
                key="k", cooldown_seconds=1.0, consecutive_429s=1
            )


# =============================================================================
# Recording order under a degraded coordination store
# =============================================================================


def _deterministic_coordinator(storage):
    """Coordinator with jitter and debouncing off, for exact metric readings."""
    from baldur.services.rate_limit_coordinator import (
        RateLimitCoordinator,
        RateLimitCoordinatorConfig,
    )

    config = RateLimitCoordinatorConfig(
        jitter_percent=0.0,
        debounce_window_seconds=0.0,
    )
    return RateLimitCoordinator(storage=storage, config=config)


class TestOnRateLimitedStorageUnavailableBehavior:
    """A failing coordination store must not erase the evidence of the storm.

    Every caller wraps ``on_rate_limited`` fail-open, so a storage fault leaves
    no trace in the business path — the metrics are the only surviving signal,
    and they have to survive exactly the outage an operator needs them for.
    Each recording site therefore sits at the earliest point where its value is
    already true, which is what these exit paths pin: the 429 count precedes
    every storage call, while the cooldown values are only known once the store
    has merged them.
    """

    @pytest.mark.parametrize(
        "failing_call",
        ["increment_consecutive_429s", "extend_cooldown"],
        ids=["increment", "extend_cooldown"],
    )
    def test_storage_unavailable_still_counts_the_429(self, mock_storage, failing_call):
        """Whichever storage call fails, the 429 counter has already advanced."""
        from baldur.interfaces.rate_limit_storage import (
            RateLimitStorageUnavailableError,
        )

        key = _unique_key(f"unavailable_{failing_call}")
        coordinator = _deterministic_coordinator(mock_storage)

        with patch.object(
            mock_storage,
            failing_call,
            side_effect=RateLimitStorageUnavailableError("coordination store down"),
        ):
            with pytest.raises(RateLimitStorageUnavailableError):
                coordinator.on_rate_limited(key)

        assert _sample("baldur_rate_limit_429_total", key=key, status_code="429") == 1.0

    def test_storage_unavailable_on_increment_records_no_429_cooldown_values(
        self, mock_storage
    ):
        """A failing increment aborts before any cooldown value is known.

        Negative half of the ordering: the cooldown is computed from the
        increment's return value, so there is nothing truthful to record yet and
        the two cooldown series must stay untouched.
        """
        from baldur.interfaces.rate_limit_storage import (
            RateLimitStorageUnavailableError,
        )

        key = _unique_key("unavailable_before_cooldown")
        coordinator = _deterministic_coordinator(mock_storage)

        with patch.object(
            mock_storage,
            "increment_consecutive_429s",
            side_effect=RateLimitStorageUnavailableError("coordination store down"),
        ):
            with pytest.raises(RateLimitStorageUnavailableError):
                coordinator.on_rate_limited(key)

        assert _sample("baldur_rate_limit_429_total", key=key, status_code="429") == 1.0
        assert _sample("baldur_rate_limit_cooldown_seconds_count", key=key) is None
        assert _sample("baldur_rate_limit_consecutive_429s", key=key) is None

    def test_storage_unavailable_on_extend_cooldown_records_no_cooldown_values(
        self, mock_storage
    ):
        """A failing store leaves both cooldown series untouched.

        The recorded cooldown is the one now *in force* for the key, and that
        value only exists once the store's monotonic merge returns — so a store
        that raises has nothing truthful to record. The storage-degradation
        alert reads exactly this asymmetry: 429s climbing while cooldown
        observations do not.
        """
        from baldur.interfaces.rate_limit_storage import (
            RateLimitStorageUnavailableError,
        )

        key = _unique_key("unavailable_after_cooldown")
        coordinator = _deterministic_coordinator(mock_storage)

        with patch.object(
            mock_storage,
            "extend_cooldown",
            side_effect=RateLimitStorageUnavailableError("coordination store down"),
        ):
            with pytest.raises(RateLimitStorageUnavailableError):
                coordinator.on_rate_limited(key)

        assert _sample("baldur_rate_limit_429_total", key=key, status_code="429") == 1.0
        assert _sample("baldur_rate_limit_cooldown_seconds_count", key=key) is None
        assert _sample("baldur_rate_limit_consecutive_429s", key=key) is None


# =============================================================================
# Wait-or-defer decision metrics
# =============================================================================


class TestWaitIfNeededMetricsBehavior:
    """``wait_if_needed`` records the decision it made — one series per branch.

    Both series measure the *decision*, not its outcome: the wait is observed
    before sleeping, because a caller killed mid-sleep still had the full wait
    imposed on it. The no-cooldown fast path records nothing, which is what
    makes the histogram count equal the number of waits.
    """

    def _cooldown(self, storage, key: str, seconds: float) -> float:
        cooldown_until = time.time() + seconds
        storage.set_cooldown(key, cooldown_until)
        return cooldown_until

    def test_wait_metric_observes_the_imposed_cooldown_when_served(self, mock_storage):
        """A served wait observes the full remaining cooldown, and defers nothing."""
        key = _unique_key("wait_served")
        coordinator = _deterministic_coordinator(mock_storage)
        self._cooldown(mock_storage, key, 2.0)

        with mock_sleep():
            result = coordinator.wait_if_needed(key, max_wait=10.0)

        assert result.waited is True
        assert _sample("baldur_rate_limit_wait_seconds_count", key=key) == 1.0
        assert _sample("baldur_rate_limit_wait_seconds_sum", key=key) == pytest.approx(
            result.wait_time
        )
        # Negative: a served wait is not also a deferral.
        assert _sample("baldur_rate_limit_deferrals_total", key=key) is None

    def test_wait_metric_is_observed_before_the_sleep_not_after(self, mock_storage):
        """A caller killed mid-sleep still had the full wait imposed on it.

        Observing after the sleep returns would drop exactly the waits an
        operator most needs to see — the ones long enough for the caller to be
        killed inside them.
        """
        key = _unique_key("wait_interrupted")
        coordinator = _deterministic_coordinator(mock_storage)
        self._cooldown(mock_storage, key, 2.0)

        with patch("time.sleep", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                coordinator.wait_if_needed(key, max_wait=10.0)

        assert _sample("baldur_rate_limit_wait_seconds_count", key=key) == 1.0

    def test_deferral_counter_increments_when_the_cooldown_outlasts_the_bound(
        self, mock_storage
    ):
        """A deferral increments its counter and observes no wait.

        The deferred call sleeps nothing, so counting it as a wait would inflate
        the imposed-wait histogram with time no caller ever spent.
        """
        key = _unique_key("wait_deferred")
        coordinator = _deterministic_coordinator(mock_storage)
        self._cooldown(mock_storage, key, 300.0)

        with mock_sleep() as sleep_mock:
            result = coordinator.wait_if_needed(key, max_wait=1.0)

        assert result.deferred is True
        assert sleep_mock.call_count == 0
        assert _sample("baldur_rate_limit_deferrals_total", key=key) == 1.0
        # Negative: the deferral branch never observes into the wait histogram.
        assert _sample("baldur_rate_limit_wait_seconds_count", key=key) is None

    def test_no_cooldown_fast_path_records_neither_wait_metric_nor_deferral(
        self, mock_storage
    ):
        """Outside cooldown neither series moves — the histogram counts waits only."""
        key = _unique_key("wait_fast_path")
        coordinator = _deterministic_coordinator(mock_storage)

        with mock_sleep() as sleep_mock:
            result = coordinator.wait_if_needed(key, max_wait=1.0)

        assert result.waited is False
        assert result.deferred is False
        assert sleep_mock.call_count == 0
        assert _sample("baldur_rate_limit_wait_seconds_count", key=key) is None
        assert _sample("baldur_rate_limit_deferrals_total", key=key) is None


class TestWaitDeferralHelpersBehavior:
    """The two wait/defer recorders are fail-open, like every other metric helper.

    A metrics fault must never surface in the rate-limit path: the caller is
    already in a degraded situation, and losing observability is strictly better
    than losing the cooldown.
    """

    def test_record_wait_metric_survives_a_missing_metrics_module(self):
        """A missing definitions module is a no-op, not an ImportError."""
        import sys

        from baldur.services.rate_limit_coordinator import _record_rate_limit_wait

        with patch.dict(sys.modules, {"baldur.services.metrics.definitions": None}):
            _record_rate_limit_wait(key="k", wait_seconds=1.0)

    def test_record_deferral_survives_a_missing_metrics_module(self):
        """A missing definitions module is a no-op, not an ImportError."""
        import sys

        from baldur.services.rate_limit_coordinator import _record_rate_limit_deferral

        with patch.dict(sys.modules, {"baldur.services.metrics.definitions": None}):
            _record_rate_limit_deferral(key="k")

    def test_record_wait_metric_survives_a_broken_metric(self):
        """A registry fault at the label lookup is swallowed."""
        from baldur.services.rate_limit_coordinator import _record_rate_limit_wait

        with patch(
            "baldur.services.metrics.definitions.rate_limit_wait_seconds",
            _BrokenMetric(),
        ):
            _record_rate_limit_wait(key="k", wait_seconds=1.0)

    def test_record_deferral_survives_a_broken_metric(self):
        """A registry fault at the label lookup is swallowed."""
        from baldur.services.rate_limit_coordinator import _record_rate_limit_deferral

        with patch(
            "baldur.services.metrics.definitions.rate_limit_deferrals_total",
            _BrokenMetric(),
        ):
            _record_rate_limit_deferral(key="k")


_RECORDER_HELPERS = [
    ("_record_rate_limit_429", {"key": "k"}, "rate_limit_429_total"),
    (
        "_record_rate_limit_cooldown",
        {"key": "k", "cooldown_seconds": 1.0, "consecutive_429s": 1},
        "rate_limit_cooldown_seconds",
    ),
    (
        "_record_rate_limit_wait",
        {"key": "k", "wait_seconds": 1.0},
        "rate_limit_wait_seconds",
    ),
    ("_record_rate_limit_deferral", {"key": "k"}, "rate_limit_deferrals_total"),
]

_RECORDER_HELPER_IDS = ["429", "cooldown", "wait", "deferral"]


class TestCoordinatorFailOpenLogEventsContract:
    """Names and levels of the coordinator's fail-open log events.

    These are an incident-triage surface — an operator finds them by grep — so
    the literal names are pinned rather than derived. Two things they must not
    drift back to: the ``adaptive_throttle.`` component prefix (wrong component
    for a rate-limit helper) and an ``_available`` name on a path that only runs
    when the thing is *un*available. The level split is a standards floor:
    ``_failed`` is WARNING, while the two ``_unavailable`` events stay DEBUG
    because a stripped install hits them on every single call.
    """

    def test_missing_eventbus_logs_the_unavailable_event_at_debug(self):
        """The ImportError path names the bus as unavailable, at DEBUG."""
        from baldur.services.rate_limit_coordinator import _emit_rate_limit_event

        with (
            patch(
                "baldur.services.event_bus.get_event_bus",
                side_effect=ImportError("no module"),
            ),
            capture_logs() as logs,
        ):
            _emit_rate_limit_event("RATE_LIMIT_429", {"key": "k"})

        record = next(
            log
            for log in logs
            if log["event"] == "rate_limit_coordinator.eventbus_unavailable"
        )
        assert record["log_level"] == "debug"

    def test_emit_failure_logs_emit_event_failed_at_warning(self):
        """A live-bus emit failure is a genuine anomaly — WARNING, with the cause."""
        from baldur.services.rate_limit_coordinator import _emit_rate_limit_event

        mock_bus = MagicMock(spec=["emit"])
        mock_bus.emit.side_effect = RuntimeError("bus broken")

        with (
            patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus),
            capture_logs() as logs,
        ):
            _emit_rate_limit_event("RATE_LIMIT_429", {"key": "k"})

        record = next(
            log
            for log in logs
            if log["event"] == "rate_limit_coordinator.emit_event_failed"
        )
        assert record["log_level"] == "warning"

    def test_unknown_event_type_logs_a_warning_naming_the_type(self):
        """The triaging operator needs the rejected name, not just the fact."""
        from baldur.services.rate_limit_coordinator import _emit_rate_limit_event

        mock_bus = MagicMock(spec=["emit"])
        with (
            patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus),
            capture_logs() as logs,
        ):
            _emit_rate_limit_event("NONEXISTENT_EVENT_TYPE", {"key": "k"})

        record = next(
            log
            for log in logs
            if log["event"] == "rate_limit_coordinator.unknown_event_type"
        )
        assert record["log_level"] == "warning"
        assert record["event_type_name"] == "NONEXISTENT_EVENT_TYPE"

    @pytest.mark.parametrize(
        ("helper_name", "kwargs", "metric_name"),
        _RECORDER_HELPERS,
        ids=_RECORDER_HELPER_IDS,
    )
    def test_missing_metrics_module_logs_the_unavailable_event_at_debug(
        self, helper_name, kwargs, metric_name
    ):
        """Every recorder helper reports a stripped install the same way."""
        import sys

        import baldur.services.rate_limit_coordinator as coordinator_pkg

        helper = getattr(coordinator_pkg, helper_name)

        with (
            patch.dict(sys.modules, {"baldur.services.metrics.definitions": None}),
            capture_logs() as logs,
        ):
            helper(**kwargs)

        record = next(
            log
            for log in logs
            if log["event"] == "rate_limit_coordinator.metrics_module_unavailable"
        )
        assert record["log_level"] == "debug"

    @pytest.mark.parametrize(
        ("helper_name", "kwargs", "metric_name"),
        _RECORDER_HELPERS,
        ids=_RECORDER_HELPER_IDS,
    )
    def test_a_broken_metric_logs_metrics_failed_at_warning(
        self, helper_name, kwargs, metric_name
    ):
        """A registry fault is unexpected, so it clears the ``_failed`` floor.

        This is the level D5 raised from DEBUG: a swallowed registry fault that
        only whispers at DEBUG is invisible on the install that has it.
        """
        import baldur.services.rate_limit_coordinator as coordinator_pkg

        helper = getattr(coordinator_pkg, helper_name)

        with (
            patch(
                f"baldur.services.metrics.definitions.{metric_name}",
                _BrokenMetric(),
            ),
            capture_logs() as logs,
        ):
            helper(**kwargs)

        record = next(
            log
            for log in logs
            if log["event"] == "rate_limit_coordinator.metrics_failed"
        )
        assert record["log_level"] == "warning"


class TestRateLimitWaitMetricDefinitionsContract:
    """Published shape of the two wait/defer series (names, labels, buckets, help)."""

    def test_wait_histogram_lowest_bucket_is_the_cooldown_floor(self):
        """0.1s is the coordinator's minimum cooldown — the first useful bucket."""
        from baldur.services.metrics.definitions import rate_limit_wait_seconds

        assert rate_limit_wait_seconds._upper_bounds[0] == 0.1

    def test_wait_histogram_top_explicit_bucket_is_the_retry_after_ceiling(self):
        """3600s, not max_delay: an honored Retry-After can push a wait far past
        the ladder cap, and collapsing those into +Inf hides the very case the
        series exists to show."""
        from baldur.services.metrics.definitions import rate_limit_wait_seconds

        buckets = list(rate_limit_wait_seconds._upper_bounds)
        assert buckets[-1] == float("inf")
        assert buckets[-2] == 3600.0

    def test_wait_histogram_help_text_states_imposed_not_slept_semantics(self):
        """The help text has to say "imposed": the value is recorded at decision
        time, so it is not the time any caller actually slept."""
        from baldur.services.metrics.definitions import rate_limit_wait_seconds

        assert "imposed" in rate_limit_wait_seconds._documentation

    @pytest.mark.parametrize(
        "metric_name",
        ["rate_limit_wait_seconds", "rate_limit_deferrals_total"],
        ids=["wait", "deferrals"],
    )
    def test_wait_and_deferral_series_are_labelled_by_key_alone(self, metric_name):
        """``key`` is the unit of coordination, so it is the only label."""
        from baldur.services.metrics import definitions

        assert getattr(definitions, metric_name)._labelnames == ("key",)


class TestRateLimitCooldownMetricDefinitionsContract:
    """Published shape of the cooldown series (buckets, help)."""

    def test_cooldown_histogram_shares_the_wait_histogram_bucket_set(self):
        """The two describe the same quantity from either side of one 429.

        A cooldown that lands in a bucket the wait series does not have makes
        the two unreadable side by side, and the old set stopped at 300s — every
        honored ``Retry-After`` above that collapsed into ``+Inf``, which is the
        exact range an operator opens this series to see.
        """
        from baldur.services.metrics.definitions import (
            rate_limit_cooldown_seconds,
            rate_limit_wait_seconds,
        )

        assert tuple(rate_limit_cooldown_seconds._upper_bounds) == tuple(
            rate_limit_wait_seconds._upper_bounds
        )

    def test_cooldown_histogram_top_explicit_bucket_is_the_retry_after_ceiling(self):
        """3600s — the default ceiling on an honored header — is a real bucket."""
        from baldur.services.metrics.definitions import rate_limit_cooldown_seconds

        buckets = list(rate_limit_cooldown_seconds._upper_bounds)
        assert buckets[-1] == float("inf")
        assert buckets[-2] == 3600.0

    def test_an_hour_long_cooldown_lands_in_a_finite_bucket(self):
        """The regression, observed end to end rather than read off the config."""
        from baldur.services.metrics.definitions import rate_limit_cooldown_seconds

        key = _unique_key("cooldown_bucket")
        rate_limit_cooldown_seconds.labels(key=key).observe(3600.0)

        assert (
            _sample("baldur_rate_limit_cooldown_seconds_bucket", key=key, le="3600.0")
            == 1.0
        )

    def test_cooldown_histogram_help_text_states_in_force_not_computed_semantics(self):
        """The recorded value is the cooldown that won the merge, not this call's.

        The two differ whenever a peer's longer cooldown is still running, and an
        operator reading "cooldown after a 429" would otherwise assume the number
        describes the 429 that was just handled.
        """
        from baldur.services.metrics.definitions import rate_limit_cooldown_seconds

        assert "in force" in rate_limit_cooldown_seconds._documentation


# =============================================================================
# retry_after header precedence
# =============================================================================


class TestRateLimitCoordinatorRetryAfter:
    """on_rate_limited retry_after header precedence tests."""

    def test_uses_retry_after_header_when_provided(self, mock_storage):
        """A provided retry_after wins over default_retry_after."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        default_ra = DEFAULT_RETRY_AFTER
        header_ra = 30.0
        config = RateLimitCoordinatorConfig(
            default_retry_after=default_ra,
            backoff_multiplier=1.0,
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        delay = coordinator.on_rate_limited("test_api", retry_after=header_ra)
        assert delay == pytest.approx(header_ra, rel=0.1)

    def test_uses_default_retry_after_when_none(self, mock_storage):
        """A missing retry_after falls back to default_retry_after."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        default_ra = 7.0
        config = RateLimitCoordinatorConfig(
            default_retry_after=default_ra,
            backoff_multiplier=1.0,
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        delay = coordinator.on_rate_limited("test_api", retry_after=None)
        assert delay == pytest.approx(default_ra, rel=0.1)

    def test_max_delay_cap(self, mock_storage):
        """The headerless ladder is capped at max_delay."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        max_delay = 30.0
        config = RateLimitCoordinatorConfig(
            default_retry_after=10.0,
            backoff_multiplier=DEFAULT_BACKOFF_MULTIPLIER,
            max_delay=max_delay,
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        delay = None
        for _ in range(10):
            delay = coordinator.on_rate_limited("test_api")

        assert delay <= max_delay


# =============================================================================
# on_success behavior
# =============================================================================


class TestRateLimitCoordinatorOnSuccess:
    """on_success() behavior tests."""

    def test_on_success_resets_consecutive_429s(self, mock_storage):
        """A success resets the consecutive-429 count."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        mock_storage.increment_consecutive_429s("test_api")
        mock_storage.increment_consecutive_429s("test_api")
        assert mock_storage.get_state("test_api").consecutive_429s == 2

        coordinator.on_success("test_api")
        assert mock_storage.get_state("test_api").consecutive_429s == 0

    def test_on_success_no_error_when_no_prior_429(self, mock_storage):
        """on_success without a prior 429 does not raise."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        coordinator.on_success("test_api")
        assert mock_storage.get_state("test_api").consecutive_429s == 0


# =============================================================================
# Cooldown-end arming — the all-clear tracks the cooldown it belongs to
# =============================================================================


class _RecordingTimer:
    """``threading.Timer`` stand-in that arms without starting a thread.

    Deferred rather than synchronous: every assertion below turns on *when* a
    callback runs relative to a re-arm or a cancel, which is exactly the
    ordering the ownership match exists to survive.
    """

    def __init__(self, interval, function, start_error=None):
        self.interval = interval
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False
        self._start_error = start_error

    def start(self):
        if self._start_error is not None:
            raise self._start_error
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        """Run the callback the way the timer thread would."""
        self.function()


class _TimerRecorder:
    """Collects every arming, and can make ``start()`` refuse like a real one."""

    def __init__(self):
        self.armed: list[_RecordingTimer] = []
        self.start_error: Exception | None = None

    def __call__(self, interval, function, args=None, kwargs=None):
        timer = _RecordingTimer(interval, function, self.start_error)
        self.armed.append(timer)
        return timer


@pytest.fixture
def timers(monkeypatch):
    """Replace ``threading.Timer`` with a recorder, and hand back the recorder."""
    recorder = _TimerRecorder()
    monkeypatch.setattr(threading, "Timer", recorder)
    return recorder


def _silent_event_bus():
    """A bus that swallows emissions, for tests that assert on state not events.

    Built through the shared factory rather than a bare ``MagicMock`` so these
    tests add no spec-less mock to the tree's budget.
    """
    bus, _emitted = make_mock_event_bus()
    return bus


def _cooldown_end_events(emitted):
    return [e for e in emitted if "RATE_LIMIT_COOLDOWN_END" in e["event_type"]]


def _429_events(emitted):
    return [e for e in emitted if "RATE_LIMIT_429" in e["event_type"]]


class TestScheduleCooldownEndBehavior:
    """The all-clear is armed for every 429, and exactly one fires per episode.

    Scheduling used to sit inside the 429 event-debounce gate, so a 429 the
    debounce suppressed extended the shared cooldown while the all-clear stayed
    armed at the pre-extension time. The PRO adaptive throttle consumes that
    event and starts restoring outbound throughput — into a cooldown that is
    still live, which is the self-DDoS the coordinator exists to prevent.
    """

    @staticmethod
    def _storm_coordinator(mock_storage):
        """A coordinator whose debounce window swallows a whole burst's events."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            default_retry_after=1.0,
            backoff_multiplier=1.0,
            jitter_percent=0.0,
            debounce_window_seconds=DEFAULT_DEBOUNCE_WINDOW,
        )
        return RateLimitCoordinator(storage=mock_storage, config=config)

    def test_a_storm_arms_the_all_clear_for_its_final_expiry(
        self, mock_storage, timers
    ):
        """Each 429 of a burst moves the all-clear, including the suppressed ones."""
        # Given three 429s inside one debounce window, each extending further
        key = _unique_key("storm")
        coordinator = self._storm_coordinator(mock_storage)
        mock_bus, emitted = make_mock_event_bus()

        # When the burst arrives
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            for header in (60, 120, 180):
                coordinator.on_rate_limited(key, retry_after=header)

        # Then every 429 re-armed, and the live arming is the final expiry
        final_expiry = mock_storage.get_state(key).cooldown_until
        assert len(timers.armed) == 3
        assert coordinator._cooldown_timers[key][1] == final_expiry
        assert coordinator._cooldown_timers[key][0] is timers.armed[-1]
        assert timers.armed[-1].interval == pytest.approx(180, abs=2)
        assert [t.cancelled for t in timers.armed] == [True, True, False]

    def test_a_storm_emits_exactly_one_all_clear_at_its_final_expiry(
        self, mock_storage, timers
    ):
        """Negative: the burst produces one END, and it carries the final expiry."""
        key = _unique_key("storm_end")
        coordinator = self._storm_coordinator(mock_storage)
        mock_bus, emitted = make_mock_event_bus()

        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            for header in (60, 120, 180):
                coordinator.on_rate_limited(key, retry_after=header)
            for timer in timers.armed:
                timer.fire()

        ends = _cooldown_end_events(emitted)
        assert len(ends) == 1
        assert ends[0]["data"]["cooldown_until"] == (
            mock_storage.get_state(key).cooldown_until
        )
        assert ends[0]["data"]["key"] == key

    def test_de_gating_the_arming_did_not_de_gate_the_429_event(
        self, mock_storage, timers
    ):
        """Negative: the same burst still emits exactly one RATE_LIMIT_429.

        Only the scheduling side-effect left the debounce gate. The event itself
        stays debounced — it is a notification, and one per window is the point.
        """
        key = _unique_key("storm_debounce")
        coordinator = self._storm_coordinator(mock_storage)
        mock_bus, emitted = make_mock_event_bus()

        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            for header in (60, 120, 180):
                coordinator.on_rate_limited(key, retry_after=header)

        assert len(_429_events(emitted)) == 1
        assert len(timers.armed) == 3

    def test_a_429_that_does_not_move_the_expiry_leaves_the_timer_alone(
        self, mock_storage, timers
    ):
        """Re-arm once per real extension, not once per 429.

        A shorter candidate loses the monotonic merge, so the effective expiry
        is unchanged and there is nothing to re-arm. Cancelling and rebuilding a
        timer per 429 would put a storm's worth of thread churn on the path.
        """
        key = _unique_key("no_extension")
        coordinator = self._storm_coordinator(mock_storage)

        with patch(
            "baldur.services.event_bus.get_event_bus", return_value=_silent_event_bus()
        ):
            coordinator.on_rate_limited(key, retry_after=300)
            coordinator.on_rate_limited(key, retry_after=5)

        assert len(timers.armed) == 1
        assert timers.armed[0].cancelled is False
        assert coordinator._cooldown_timers[key][0] is timers.armed[0]

    def test_an_expiry_already_past_still_yields_one_all_clear(
        self, mock_storage, timers
    ):
        """The silent skip is gone: a lapsed cooldown still owes its all-clear.

        The old scheduler returned on a non-positive delay, so a cooldown that
        had already expired by arming time armed nothing and announced nothing —
        leaving the PRO throttle reduced until the next 429 cycle.
        """
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )
        from baldur.services.rate_limit_coordinator.coordinator import (
            _MIN_COOLDOWN_END_DELAY_SECONDS,
        )

        key = _unique_key("past_expiry")
        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )
        mock_bus, emitted = make_mock_event_bus()

        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            coordinator._schedule_cooldown_end_event(key, time.time() - 5)
            assert timers.armed[0].interval == pytest.approx(
                _MIN_COOLDOWN_END_DELAY_SECONDS, abs=1e-6
            )
            timers.armed[0].fire()

        assert len(_cooldown_end_events(emitted)) == 1
        assert key not in coordinator._cooldown_timers

    def test_a_re_arm_at_a_fired_expiry_arms_strictly_later_than_it(
        self, mock_storage, timers
    ):
        """The positive clamp is what makes the ownership match decidable.

        ``armed_expiry`` doubles as the ownership token and is no longer
        monotonically increasing per key (``clear()`` can move it earlier), so
        the guarantee that a successor never collides with a fired predecessor
        rests on this clamp rather than on ordering.
        """
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        key = _unique_key("clamp")
        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )
        fired_expiry = time.time()

        with patch(
            "baldur.services.event_bus.get_event_bus", return_value=_silent_event_bus()
        ):
            coordinator._schedule_cooldown_end_event(key, fired_expiry)

        assert coordinator._cooldown_timers[key][1] > fired_expiry

    def test_a_stale_callback_announces_nothing_and_keeps_the_successor(
        self, mock_storage, timers
    ):
        """Negative: a cancelled-but-already-fired timer must not speak or unregister.

        ``Timer.cancel()`` cannot stop a callback that has already entered, so a
        re-arm racing a firing timer leaves two callbacks live. Without the owner
        match the stale one both announces recovery into the extended cooldown
        and pops the live successor's registration, leaving the key monitored by
        nothing at all.
        """
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        key = _unique_key("stale_callback")
        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )
        mock_bus, emitted = make_mock_event_bus()
        now = time.time()

        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            coordinator._schedule_cooldown_end_event(key, now + 60)
            coordinator._schedule_cooldown_end_event(key, now + 120)
            stale, live = timers.armed

            # The stale callback runs after its timer was cancelled and replaced
            stale.fire()
            assert _cooldown_end_events(emitted) == []
            assert coordinator._cooldown_timers[key][0] is live

            live.fire()

        assert len(_cooldown_end_events(emitted)) == 1
        assert key not in coordinator._cooldown_timers

    def test_clear_then_a_shorter_429_re_arms_at_the_new_expiry(
        self, mock_storage, timers
    ):
        """``clear()`` is the operator escape, and it must not strand the all-clear.

        ``clear()`` drops the storage key without touching the timer registry, so
        a re-arm rule of "only when the expiry moved later" would find the stale
        far arming already covering the fresh short cooldown and skip it — the
        operator escapes a bogus hour-long ``Retry-After`` and the throttle stays
        dampened for that hour anyway.
        """
        key = _unique_key("clear_escape")
        coordinator = self._storm_coordinator(mock_storage)
        mock_bus, emitted = make_mock_event_bus()

        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            coordinator.on_rate_limited(key, retry_after=3600)
            coordinator.clear(key)
            coordinator.on_rate_limited(key, retry_after=30)

            stale, live = timers.armed
            assert live.interval == pytest.approx(30, abs=2)
            assert coordinator._cooldown_timers[key][1] == (
                mock_storage.get_state(key).cooldown_until
            )
            assert stale.cancelled is True

            live.fire()

        assert len(_cooldown_end_events(emitted)) == 1

    def test_a_refused_arm_is_logged_and_leaves_no_registry_entry(
        self, mock_storage, timers
    ):
        """A refused ``Timer.start`` must not leave a dead entry behind.

        ``start()`` raises at interpreter shutdown and at a live process's thread
        ceiling. A registry entry left behind after a refusal is a key no later
        429 can ever re-arm, because every re-arm compares against the entry that
        no timer backs.
        """
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        key = _unique_key("arm_refused")
        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )
        timers.start_error = RuntimeError("can't start new thread")

        with capture_logs() as logs:
            coordinator._schedule_cooldown_end_event(key, time.time() + 60)

        assert key not in coordinator._cooldown_timers
        record = next(
            log
            for log in logs
            if log["event"] == "rate_limit_coordinator.cooldown_timer_arm_failed"
        )
        assert record["log_level"] == "warning"
        assert record["rate_limit_key"] == key

    def test_reset_instance_cancels_every_armed_timer(self, mock_storage, timers):
        """The registry holds a pair now, and teardown still has to reach the timer."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )
        RateLimitCoordinator._instance = coordinator
        try:
            with patch(
                "baldur.services.event_bus.get_event_bus",
                return_value=_silent_event_bus(),
            ):
                coordinator._schedule_cooldown_end_event(
                    _unique_key("reset"), time.time() + 60
                )
            RateLimitCoordinator.reset_instance()
        finally:
            RateLimitCoordinator._instance = None

        assert timers.armed[0].cancelled is True
        assert coordinator._cooldown_timers == {}


# =============================================================================
# Monotonic cooldown — the stored expiry moves only later
# =============================================================================


class TestOnRateLimitedMonotonicBehavior:
    """A short 429 never cuts a live longer cooldown, and the numbers say so.

    Under the previous last-writer-wins store, a worker whose 429 carried no
    ``Retry-After`` computed a ~10-60 s ladder delay and overwrote a peer's
    honored ``Retry-After: 900`` — every worker in the fleet then resumed long
    before the provider's stated earliest time.
    """

    @staticmethod
    def _coordinator(mock_storage, **overrides):
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            **{
                "default_retry_after": 1.0,
                "backoff_multiplier": 1.0,
                "jitter_percent": 0.0,
                "debounce_window_seconds": 0.0,
                **overrides,
            }
        )
        return RateLimitCoordinator(storage=mock_storage, config=config)

    def test_a_headerless_429_does_not_shorten_an_honored_retry_after(
        self, mock_storage, timers
    ):
        """The stored expiry after both 429s is still the honored one."""
        key = _unique_key("monotonic")
        coordinator = self._coordinator(mock_storage)

        with patch(
            "baldur.services.event_bus.get_event_bus", return_value=_silent_event_bus()
        ):
            coordinator.on_rate_limited(key, retry_after=300)
            honored_until = mock_storage.get_state(key).cooldown_until
            coordinator.on_rate_limited(key)

        assert mock_storage.get_state(key).cooldown_until == honored_until

    def test_on_rate_limited_returns_the_cooldown_in_force(self, mock_storage, timers):
        """The return value is the wait that applies, not the proposal that lost.

        Two callers log this number and the operator-facing escalation payload
        carries it beside ``cooldown_until``; returning the discarded candidate
        made those two contradict each other during exactly the storm an
        operator reads them in.
        """
        key = _unique_key("in_force")
        coordinator = self._coordinator(mock_storage)

        with patch(
            "baldur.services.event_bus.get_event_bus", return_value=_silent_event_bus()
        ):
            coordinator.on_rate_limited(key, retry_after=300)
            in_force = coordinator.on_rate_limited(key)

        assert in_force == pytest.approx(300, abs=2)

    def test_the_cooldown_histogram_observes_the_in_force_value(
        self, mock_storage, timers
    ):
        """Negative: the discarded ~1s candidate is never observed."""
        key = _unique_key("in_force_metric")
        coordinator = self._coordinator(mock_storage)

        with patch(
            "baldur.services.event_bus.get_event_bus", return_value=_silent_event_bus()
        ):
            coordinator.on_rate_limited(key, retry_after=300)
            coordinator.on_rate_limited(key)

        assert _sample("baldur_rate_limit_cooldown_seconds_count", key=key) == 2.0
        assert (
            _sample("baldur_rate_limit_cooldown_seconds_bucket", key=key, le="5.0")
            == 0.0
        )
        assert (
            _sample("baldur_rate_limit_cooldown_seconds_bucket", key=key, le="300.0")
            == 2.0
        )

    def test_the_429_event_payload_carries_the_effective_expiry(
        self, mock_storage, timers
    ):
        """The event's ``cooldown_until`` is the winner, so the PRO handler's
        per-key copy is right without any change on its side."""
        key = _unique_key("payload")
        coordinator = self._coordinator(mock_storage)
        mock_bus, emitted = make_mock_event_bus()

        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            coordinator.on_rate_limited(key, retry_after=300)
            coordinator.on_rate_limited(key)

        second = _429_events(emitted)[1]["data"]
        assert second["cooldown_until"] == mock_storage.get_state(key).cooldown_until
        # The field named for this call's own computation keeps meaning that.
        assert second["calculated_delay"] == pytest.approx(1.0, abs=0.5)

    def test_a_raw_header_string_installs_its_cooldown_instead_of_raising(
        self, mock_storage, timers
    ):
        """The documented direct-drive form passes the header through verbatim.

        ``on_rate_limited(key, retry_after=response.headers.get("Retry-After"))``
        is the coordinator's own docstring example. Uncoerced, the string reached
        a numeric comparison and raised — where every caller's fail-open wrap
        dropped it, so the 429 counter climbed while no cooldown was installed.
        """
        key = _unique_key("raw_header")
        coordinator = self._coordinator(mock_storage)
        before = time.time()

        with patch(
            "baldur.services.event_bus.get_event_bus", return_value=_silent_event_bus()
        ):
            coordinator.on_rate_limited(key, retry_after="120")

        stored = mock_storage.get_state(key).cooldown_until
        assert stored - before == pytest.approx(120, abs=2)

    def test_an_http_date_header_installs_a_cooldown_derived_from_that_date(
        self, mock_storage, timers
    ):
        """The HTTP-date form is honored rather than dropped to the ladder.

        Dropping it is the one hole in the "never resume early" property: a
        provider stating an hours-long wait as a date would get the ~1s ladder.
        """
        key = _unique_key("http_date")
        coordinator = self._coordinator(mock_storage)
        before = time.time()
        header = format_datetime(
            datetime.now(UTC) + timedelta(seconds=120), usegmt=True
        )

        with patch(
            "baldur.services.event_bus.get_event_bus", return_value=_silent_event_bus()
        ):
            coordinator.on_rate_limited(key, retry_after=header)

        stored = mock_storage.get_state(key).cooldown_until
        assert stored - before == pytest.approx(120, abs=3)

    @pytest.mark.parametrize(
        "header",
        ["nan", "not-a-number", "Mon, 01 Jun 2020 12:00:00 GMT"],
        ids=["nan", "unparseable", "past-date"],
    )
    def test_an_unusable_header_falls_back_to_the_ladder(
        self, mock_storage, timers, header
    ):
        """Every unusable form yields the headerless cooldown, and a real number.

        ``"nan"`` is the sharp one: it passes a bare ``float()`` and every
        subsequent comparison, so an unrejected NaN would be stored as the
        expiry and no later read could ever find the cooldown over.
        """
        key = _unique_key("unusable_header")
        coordinator = self._coordinator(mock_storage)
        before = time.time()

        with patch(
            "baldur.services.event_bus.get_event_bus", return_value=_silent_event_bus()
        ):
            coordinator.on_rate_limited(key, retry_after=header)

        stored = mock_storage.get_state(key).cooldown_until
        assert not math.isnan(stored)
        assert stored - before == pytest.approx(1.0, abs=1)

    def test_a_sustained_storm_past_the_backoff_overflow_still_installs_a_cooldown(
        self, mock_storage, timers
    ):
        """The consecutive counter is unbounded, and the ladder must survive it.

        It resets only on a success or an operator ``clear()``, so a long provider
        quota outage walks it past the depth where the exponentiation used to
        raise ``OverflowError`` — and the fail-open wrap then dropped every
        cooldown, at the storm depth that most needs one.
        """
        key = _unique_key("overflow_depth")
        coordinator = self._coordinator(mock_storage, backoff_multiplier=2.0)
        # One below the first attempt whose 2**(attempt-1) overflows a float,
        # so this 429's own increment walks the ladder straight into it.
        mock_storage.get_state(key).consecutive_429s = 1024
        before = time.time()

        with patch(
            "baldur.services.event_bus.get_event_bus", return_value=_silent_event_bus()
        ):
            in_force = coordinator.on_rate_limited(key)

        assert in_force > 0
        assert mock_storage.get_state(key).cooldown_until > before


# =============================================================================
# rate_limit_aware decorator
# =============================================================================


class TestRateLimitAwareDecorator:
    """rate_limit_aware() decorator tests."""

    def test_decorator_calls_wait_and_on_success(self, mock_storage):
        """The decorator calls wait_if_needed and on_success."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        @coordinator.rate_limit_aware("test_api")
        def call_api():
            return mock_response

        result = call_api()
        assert result.status_code == 200

    def test_decorator_calls_on_rate_limited_on_429(
        self, coordinator_no_jitter_no_debounce, mock_storage
    ):
        """The decorator calls on_rate_limited for a 429 response."""
        coordinator = coordinator_no_jitter_no_debounce

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "10"}

        @coordinator.rate_limit_aware("test_api")
        def call_api():
            return mock_response

        call_api()

        state = mock_storage.get_state("test_api")
        assert state.consecutive_429s == 1


# =============================================================================
# _broadcast_to_cluster distributed propagation
# =============================================================================


class TestBroadcastToClusterBehavior:
    """_broadcast_to_cluster fail-open behavior."""

    def test_broadcast_calls_distributed_channel(self, mock_storage):
        """_broadcast_to_cluster calls broadcast_rate_limit_429 on the channel."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        mock_channel = MagicMock()
        with patch(
            "baldur.services.rate_limit.distributed_channel.get_distributed_rate_limit_channel",
            return_value=mock_channel,
        ):
            coordinator._broadcast_to_cluster(
                key="payment_api",
                consecutive_429s=3,
                cooldown_until=1000.0,
                calculated_delay=5.0,
            )

        mock_channel.broadcast_rate_limit_429.assert_called_once_with(
            key="payment_api",
            consecutive_429s=3,
            cooldown_until=1000.0,
            calculated_delay=5.0,
        )

    def test_broadcast_fail_open_on_import_error(self, mock_storage):
        """A distributed-channel import failure passes without raising."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        with patch(
            "baldur.services.rate_limit.distributed_channel.get_distributed_rate_limit_channel",
            side_effect=ImportError("no kafka"),
        ):
            coordinator._broadcast_to_cluster(
                key="test",
                consecutive_429s=1,
                cooldown_until=1000.0,
                calculated_delay=5.0,
            )

    def test_broadcast_fail_open_on_runtime_error(self, mock_storage):
        """A distributed-channel runtime error passes without raising."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        with patch(
            "baldur.services.rate_limit.distributed_channel.get_distributed_rate_limit_channel",
            side_effect=RuntimeError("channel broken"),
        ):
            coordinator._broadcast_to_cluster(
                key="test",
                consecutive_429s=1,
                cooldown_until=1000.0,
                calculated_delay=5.0,
            )

    def test_on_rate_limited_invokes_broadcast(self, mock_storage):
        """on_rate_limited invokes _broadcast_to_cluster."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        with patch.object(coordinator, "_broadcast_to_cluster") as mock_broadcast:
            coordinator.on_rate_limited("test_api", retry_after=5.0)

        mock_broadcast.assert_called_once()
        assert mock_broadcast.call_args[0][0] == "test_api"
