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
- on_success, cooldown-record handoff to the announcer
"""

from __future__ import annotations

import asyncio
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

from tests.factories.rate_limit_doubles import (
    NetworkBackedRateLimitStorage,
    RaisingRateLimitStorage,
    ToThreadSpy,
)
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

        # When the burst arrives (the recording announcer keeps the emit path
        # free of a live daemon thread)
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
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
# Shared seams for the store-facing cases below
# =============================================================================


class _RecordingAnnouncer:
    """``CooldownAnnouncer`` stand-in that records instead of running a thread.

    The coordinator's own cases assert on the *store* and on the 429 event, not
    on when the all-clear lands; a real announcer would spawn a daemon thread per
    coordinator built here. The verified-announcement contract lives in the
    announcer's own module test, where two coordinators share one store.
    """

    def __init__(self):
        self.begun: list[str] = []
        self.tracked: list[tuple[str, float | None]] = []
        self.reverified: list[str] = []
        self.ensure_running_calls = 0
        self.stopped = False

    def begin(self, key):
        self.begun.append(key)

    def track(self, key, cooldown_until=None):
        self.tracked.append((key, cooldown_until))

    def reverify(self, key):
        self.reverified.append(key)

    def ensure_running(self):
        self.ensure_running_calls += 1

    def run_once(self, now=None):
        return []

    def stop(self):
        self.stopped = True

    @property
    def pending(self):
        return {key: until for key, until in self.tracked if until is not None}

    @property
    def is_alive(self):
        return False


@pytest.fixture
def announcer(monkeypatch) -> _RecordingAnnouncer:
    """Give every coordinator built in the test the recording announcer."""
    from baldur.services.rate_limit_coordinator import coordinator as coordinator_module

    recorder = _RecordingAnnouncer()
    monkeypatch.setattr(
        coordinator_module, "CooldownAnnouncer", lambda **kwargs: recorder
    )
    return recorder


def _silent_event_bus():
    """A bus that swallows emissions, for tests that assert on state not events.

    Built through the shared factory rather than a bare ``MagicMock`` so these
    tests add no spec-less mock to the tree's budget.
    """
    bus, _emitted = make_mock_event_bus()
    return bus


def _429_events(emitted):
    return [e for e in emitted if "RATE_LIMIT_429" in e["event_type"]]


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
        self, mock_storage, announcer
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

    def test_on_rate_limited_returns_the_cooldown_in_force(
        self, mock_storage, announcer
    ):
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
        self, mock_storage, announcer
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
        self, mock_storage, announcer
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
        self, mock_storage, announcer
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
        self, mock_storage, announcer
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
        self, mock_storage, announcer, header
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
        self, mock_storage, announcer
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


# =============================================================================
# Announcer handoff — the coordinator brackets its own store write
# =============================================================================


class TestOnRateLimitedAnnouncerHandoffBehavior:
    """The 429 path hands the announcer a marker, then the store's own answer.

    The bracket spans the write, not just the record update: between the store
    accepting a cooldown and the record carrying it, a verifying read would see
    the pre-429 expiry it was armed for and announce into the cooldown this call
    is installing.
    """

    def test_the_marker_is_set_before_the_store_write_lands(
        self, mock_storage, announcer
    ):
        """Ordering is the whole point — a marker set afterwards shields nothing."""
        # Given a store that reports the announcer's marker state as it writes
        observed: list[list[str]] = []
        real_extend = mock_storage.extend_cooldown

        def _observing_extend(key, cooldown_until, ttl=None):
            observed.append(list(announcer.begun))
            return real_extend(key, cooldown_until, ttl)

        mock_storage.extend_cooldown = _observing_extend
        coordinator = _deterministic_coordinator(mock_storage)

        # When a 429 is handled
        coordinator.on_rate_limited("test_api", retry_after=5.0)

        # Then the key was already in flight while the store was being written
        assert observed == [["test_api"]]

    def test_the_recorded_expiry_is_the_stores_effective_value(
        self, mock_storage, announcer
    ):
        """Not this call's proposal: the store decides which cooldown wins.

        Recording the candidate would put the record — and therefore the
        all-clear — at an expiry the shared store discarded.
        """
        coordinator = _deterministic_coordinator(mock_storage)
        coordinator.on_rate_limited("test_api", retry_after=600.0)

        coordinator.on_rate_limited("test_api", retry_after=1.0)

        effective = mock_storage.get_state("test_api").cooldown_until
        assert announcer.tracked[-1] == ("test_api", effective)

    def test_a_store_that_raises_releases_the_marker_without_recording(
        self, mock_storage, announcer
    ):
        """The ``finally`` leg, on the path every caller wraps fail-open.

        A recorded expiry the store never accepted would be an all-clear for a
        cooldown that was never installed, and a marker left behind would shield
        the key from every later verification pass in this process.
        """
        from baldur.interfaces.rate_limit_storage import (
            RateLimitStorageUnavailableError,
        )

        coordinator = _deterministic_coordinator(mock_storage)

        with patch.object(
            mock_storage,
            "extend_cooldown",
            side_effect=RateLimitStorageUnavailableError("coordination store down"),
        ):
            with pytest.raises(RateLimitStorageUnavailableError):
                coordinator.on_rate_limited("test_api")

        assert announcer.begun == ["test_api"]
        assert announcer.tracked == [("test_api", None)]
        assert announcer.pending == {}

    def test_every_429_is_bracketed_including_a_debounced_one(
        self, mock_storage, announcer
    ):
        """A suppressed 429 extends the shared cooldown just the same.

        The event debounce is about notification volume; leaving the announcer
        out of a debounced call would leave the record at the pre-extension
        expiry, which is the announce-into-a-live-cooldown bug in miniature.
        """
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage,
            config=RateLimitCoordinatorConfig(
                jitter_percent=0.0,
                debounce_window_seconds=DEFAULT_DEBOUNCE_WINDOW,
            ),
        )

        for _ in range(3):
            coordinator.on_rate_limited("test_api", retry_after=5.0)

        assert announcer.begun == ["test_api"] * 3
        assert [key for key, _until in announcer.tracked] == ["test_api"] * 3


class TestCoordinatorAnnouncerDelegationBehavior:
    """The coordinator's other three announcer touch points."""

    def test_clear_asks_for_an_immediate_re_verification(self, mock_storage, announcer):
        """The operator escape produces its all-clear instead of leaving the
        consumer waiting for the expiry that was just cleared."""
        coordinator = _deterministic_coordinator(mock_storage)

        coordinator.clear("test_api")

        assert announcer.reverified == ["test_api"]

    def test_wait_if_needed_revives_the_announcer_from_the_request_path(
        self, mock_storage, announcer
    ):
        """A cooldown is precisely the window in which nothing else pokes it.

        The announcer's own entry points run on a 429 or an operator clear, so a
        thread that died — or a fork child that inherited records and no thread —
        would hold its records until the next 429 the cooldown is preventing.
        """
        coordinator = _deterministic_coordinator(mock_storage)

        coordinator.wait_if_needed("test_api")

        assert announcer.ensure_running_calls == 1

    def test_reset_instance_leaves_no_live_thread_and_no_registration(
        self, mock_storage
    ):
        """Test isolation, and the same shape a second ``get_instance()`` needs.

        Two live announcers over one key would emit the all-clear twice, and a
        registration outliving its thread reports a dead worker forever.
        """
        from baldur.metrics.recorders.daemon_worker import (
            get_registered_daemon_workers,
        )
        from baldur.services.rate_limit_coordinator import RateLimitCoordinator
        from baldur.services.rate_limit_coordinator.announcer import DAEMON_WORKER_NAME

        RateLimitCoordinator.reset_instance()
        instance = _deterministic_coordinator(mock_storage)
        RateLimitCoordinator._instance = instance
        try:
            instance.on_rate_limited("test_api", retry_after=600.0)
            assert DAEMON_WORKER_NAME in get_registered_daemon_workers()

            RateLimitCoordinator.reset_instance()

            assert instance._announcer.is_alive is False
            assert DAEMON_WORKER_NAME not in get_registered_daemon_workers()
            assert RateLimitCoordinator._instance is None
        finally:
            instance._announcer.stop()
            RateLimitCoordinator.reset_instance()


# =============================================================================
# The awaitable twins — thread-hop rule and parity with their sync originals
# =============================================================================

_TO_THREAD = "baldur.services.rate_limit_coordinator.coordinator.asyncio.to_thread"


class TestCoordinatorAsyncTwinsBehavior:
    """``aon_rate_limited`` / ``aon_success`` / ``aget_instance`` leave the loop free.

    The report twin always hops — ``on_rate_limited`` publishes on the event
    bus, which waits on every subscriber that asked for its result — while the
    success twin and the instance read hop only when the work is a network
    call. Each twin leaves the store exactly as its synchronous original does.
    """

    @pytest.fixture(autouse=True)
    def _no_cluster_broadcast(self):
        """Neutralise the Dormant-tier cluster broadcast a real 429 would fire.

        ``on_rate_limited`` reaches ``_broadcast_to_cluster``, which eagerly
        attempts a broker connection where the Kafka adapter is installed — an
        explicit NON-GOAL of this surface, and ~2 s of connect timeout per 429.
        """
        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
            yield

    def test_aon_rate_limited_always_hops_even_on_a_memory_store(self, mock_storage):
        """The publish inside makes this a hop whatever the store type."""
        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        coordinator = _deterministic_coordinator(mock_storage)
        spy = ToThreadSpy()

        with patch(_TO_THREAD, new=spy):
            in_force = asyncio.run(coordinator.aon_rate_limited("k"))

        assert spy.hopped("on_rate_limited") is True
        assert in_force > 0.0
        assert mock_storage.get_state("k").consecutive_429s == 1
        assert isinstance(coordinator, RateLimitCoordinator)

    def test_aon_rate_limited_forwards_every_argument_and_the_return(
        self, mock_storage
    ):
        """Key, Retry-After and status reach the original; its answer comes back."""
        coordinator = _deterministic_coordinator(mock_storage)

        with patch.object(
            coordinator, "on_rate_limited", autospec=True, return_value=12.5
        ) as original:
            in_force = asyncio.run(coordinator.aon_rate_limited("k", 30.0, 503))

        original.assert_called_once_with("k", 30.0, 503)
        assert in_force == 12.5

    def test_aon_success_runs_inline_on_a_memory_store(self, mock_storage):
        """A dict read under a lock is cheaper than an executor hop."""
        coordinator = _deterministic_coordinator(mock_storage)
        mock_storage.increment_consecutive_429s("k")
        mock_storage.increment_consecutive_429s("k")
        spy = ToThreadSpy()

        with patch(_TO_THREAD, new=spy):
            asyncio.run(coordinator.aon_success("k"))

        assert spy.calls == []
        assert mock_storage.get_state("k").consecutive_429s == 0

    def test_aon_success_hops_for_a_network_backed_store(self):
        """A network client's read plus conditional write leaves the loop."""
        storage = NetworkBackedRateLimitStorage()
        storage.increment_consecutive_429s("k")
        coordinator = _deterministic_coordinator(storage)
        spy = ToThreadSpy()

        with patch(_TO_THREAD, new=spy):
            asyncio.run(coordinator.aon_success("k"))

        assert spy.hopped("on_success") is True
        assert storage.get_state("k").consecutive_429s == 0

    def test_aget_instance_returns_the_set_slot_without_a_hop(self):
        """Steady state is a slot read: the same instance, no executor."""
        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        RateLimitCoordinator.reset_instance()
        try:
            first = RateLimitCoordinator.get_instance()
            spy = ToThreadSpy()

            with patch(_TO_THREAD, new=spy):
                got = asyncio.run(RateLimitCoordinator.aget_instance())

            assert got is first
            assert spy.calls == []
        finally:
            RateLimitCoordinator.reset_instance()

    def test_aget_instance_constructs_an_empty_slot_on_a_worker_thread(self):
        """First construction runs storage auto-detect, so it is hopped once.

        A second call finds the slot set and hops no more — the construction
        is idempotent across the two surfaces.
        """
        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        RateLimitCoordinator.reset_instance()
        try:
            spy = ToThreadSpy()

            with patch(_TO_THREAD, new=spy):
                first = asyncio.run(RateLimitCoordinator.aget_instance())
                second = asyncio.run(RateLimitCoordinator.aget_instance())

            assert spy.hopped("get_instance") is True
            assert len(spy.calls) == 1
            assert first is second
            assert first is RateLimitCoordinator.get_instance()
        finally:
            RateLimitCoordinator.reset_instance()

    def test_the_twins_leave_the_store_as_their_sync_originals_do(self):
        """Parity: the same 429 and the same success produce the same state.

        Two identical stores, one driven through the synchronous pair and one
        through the awaitable pair, end in the same counter and — jitter off —
        the same cooldown expiry within the clock's drift between the two calls.
        """
        sync_storage = MockInMemoryRateLimitStorage()
        async_storage = MockInMemoryRateLimitStorage()
        sync_coordinator = _deterministic_coordinator(sync_storage)
        async_coordinator = _deterministic_coordinator(async_storage)

        sync_coordinator.on_rate_limited("k", 30.0)
        asyncio.run(async_coordinator.aon_rate_limited("k", 30.0))

        sync_after_429 = sync_storage.get_state("k")
        async_after_429 = async_storage.get_state("k")
        assert async_after_429.consecutive_429s == sync_after_429.consecutive_429s == 1
        assert async_after_429.cooldown_until == pytest.approx(
            sync_after_429.cooldown_until, abs=0.5
        )

        sync_coordinator.on_success("k")
        asyncio.run(async_coordinator.aon_success("k"))

        assert sync_storage.get_state("k").consecutive_429s == 0
        assert async_storage.get_state("k").consecutive_429s == 0


# =============================================================================
# rate_limit_aware — a 429 the decorated function *raises*
# =============================================================================

_DECORATED_SURFACES = pytest.mark.parametrize(
    "is_async", [False, True], ids=["def", "async_def"]
)

_OBS_TRACKER = (
    "baldur.services.circuit_breaker.rate_limit_tracker.get_rate_limit_tracker"
)
_OBS_CB_SERVICE = (
    "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service"
)


def _current_scope():
    from baldur.services.circuit_breaker.rate_limit_observation import current_scope

    return current_scope()


def _decorated(coordinator, key: str, body, *, is_async: bool):
    """Wrap ``body`` (returns or raises) under ``rate_limit_aware`` on either surface."""
    if is_async:

        @coordinator.rate_limit_aware(key)
        async def protected():
            return body()

        return protected

    @coordinator.rate_limit_aware(key)
    def protected():
        return body()

    return protected


def _call(decorated, *, is_async: bool):
    """Invoke the decorated function, driving the coroutine to completion."""
    if is_async:
        return asyncio.run(decorated())
    return decorated()


def _raising(error):
    def body():
        raise error

    return body


class TestRateLimitAwareDecoratorRaised429Behavior:
    """A 429 the wrapped function raises installs a cooldown, then re-raises unchanged.

    The wrapper claims the observation scope, which withholds the breaker
    stage's own notify — so before this branch existed, a decorated client
    that *raised* its 429 (``httpx.HTTPStatusError``, ``openai.RateLimitError``)
    under ``protect(name, retry=False)`` was recorded by nobody. The user's
    ``is_429`` / ``get_retry_after`` predicates are a response-object contract
    and never see an exception; the shared classifier answers.
    """

    @pytest.fixture(autouse=True)
    def _no_cluster_broadcast(self):
        """Neutralise the Dormant-tier cluster broadcast a real 429 would fire.

        ``on_rate_limited`` reaches ``_broadcast_to_cluster``, which eagerly
        attempts a broker connection where the Kafka adapter is installed — an
        explicit NON-GOAL of this surface, and ~2 s of connect timeout per 429.
        """
        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
            yield

    @_DECORATED_SURFACES
    def test_a_client_that_raises_429_installs_one_cooldown_and_reraises(
        self, coordinator_no_jitter_no_debounce, mock_storage, is_async
    ):
        """Exactly one cooldown, and the caller sees the client's own object."""
        error = Exception("429 too many requests")
        protected = _decorated(
            coordinator_no_jitter_no_debounce, "k", _raising(error), is_async=is_async
        )

        with pytest.raises(Exception) as exc_info:
            _call(protected, is_async=is_async)

        assert exc_info.value is error
        state = mock_storage.get_state("k")
        assert state.consecutive_429s == 1
        assert state.consecutive_429s != 0
        assert state.cooldown_until > time.time()

    @_DECORATED_SURFACES
    def test_a_client_that_raises_429_with_retry_after_honours_the_header(
        self, coordinator_no_jitter_no_debounce, mock_storage, is_async
    ):
        """The provider's stated wait reaches the cooldown through the exception."""

        class ThrottledError(Exception):
            retry_after = 45.0

        protected = _decorated(
            coordinator_no_jitter_no_debounce,
            "k",
            _raising(ThrottledError("429 too many requests")),
            is_async=is_async,
        )

        with pytest.raises(ThrottledError):
            _call(protected, is_async=is_async)

        assert mock_storage.get_state("k").cooldown_until == pytest.approx(
            time.time() + 45.0, abs=5.0
        )

    @_DECORATED_SURFACES
    def test_a_client_that_raises_a_non_429_neither_notifies_nor_resets(
        self, coordinator_no_jitter_no_debounce, mock_storage, is_async
    ):
        """An ordinary failure is not a 429 and not a success: the store is untouched.

        The reset is owed to an accepted value only (the retry loops' rule), so
        a standing counter survives a transport error.
        """
        mock_storage.increment_consecutive_429s("k")
        mock_storage.increment_consecutive_429s("k")
        protected = _decorated(
            coordinator_no_jitter_no_debounce,
            "k",
            _raising(ConnectionError("connection reset")),
            is_async=is_async,
        )

        with pytest.raises(ConnectionError):
            _call(protected, is_async=is_async)

        state = mock_storage.get_state("k")
        assert state.consecutive_429s == 2
        assert state.cooldown_until == 0.0

    @_DECORATED_SURFACES
    def test_an_inner_deferral_propagates_as_is_and_raises_429_nothing(
        self, coordinator_no_jitter_no_debounce, mock_storage, is_async
    ):
        """Baldur's own refusal is never read as evidence of a provider 429."""
        from baldur.services.rate_limit_coordinator import RateLimitDeferredError

        deferral = RateLimitDeferredError(key="inner", not_before=time.time() + 300)
        protected = _decorated(
            coordinator_no_jitter_no_debounce,
            "k",
            _raising(deferral),
            is_async=is_async,
        )

        with pytest.raises(RateLimitDeferredError) as exc_info:
            _call(protected, is_async=is_async)

        assert exc_info.value is deferral
        state = mock_storage.get_state("k")
        assert state.consecutive_429s == 0
        assert state.cooldown_until == 0.0

    @_DECORATED_SURFACES
    def test_the_raised_outcome_is_marked_on_the_scope_before_the_verdict(
        self, coordinator_no_jitter_no_debounce, is_async
    ):
        """An enclosing loop catches the same object and must find it already seen."""
        from baldur.services.circuit_breaker.rate_limit_observation import (
            close_scope,
            open_scope,
        )

        error = Exception("429 too many requests")
        protected = _decorated(
            coordinator_no_jitter_no_debounce, "k", _raising(error), is_async=is_async
        )

        token, scope = open_scope("k")
        try:
            with pytest.raises(Exception):
                _call(protected, is_async=is_async)
        finally:
            close_scope(token)

        assert scope.was_classified(error) is True
        assert scope.coordination_claimed is True

    @_DECORATED_SURFACES
    def test_a_notify_fault_on_a_raised_429_still_raises_the_clients_error(
        self, mock_storage, is_async
    ):
        """Fail-open: the coordinator's fault never replaces the business exception."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=RaisingRateLimitStorage(
                mock_storage, fail_on="increment_consecutive_429s"
            ),
            config=RateLimitCoordinatorConfig(),
        )
        error = Exception("429 too many requests")
        protected = _decorated(coordinator, "k", _raising(error), is_async=is_async)

        with pytest.raises(Exception) as exc_info:
            _call(protected, is_async=is_async)

        assert exc_info.value is error

    @_DECORATED_SURFACES
    def test_a_raising_client_under_a_breaker_frame_with_no_retry_stage_raises_429_once(
        self, coordinator_no_jitter_no_debounce, mock_storage, is_async
    ):
        """The composition the gap was found on: breaker on, retry off, client raises.

        The decorator's claim withholds the breaker frame's notify, so the
        decorator itself must record the 429 — exactly once (never zero, the
        pre-fix reading) — on the breaker's own service, and neither
        process-wide singleton is asked.
        """
        from baldur.interfaces.repositories import CircuitBreakerStateData
        from baldur.services.circuit_breaker.config import CircuitBreakerDecision
        from baldur.services.circuit_breaker.policy import (
            AsyncCircuitBreakerPolicy,
            CircuitBreakerPolicy,
        )
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )
        from baldur.services.circuit_breaker.service import CircuitBreakerService
        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        cb_service = MagicMock(spec=CircuitBreakerService)
        cb_service.is_enabled = True
        cb_service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=True,
            state=CircuitBreakerStateData(service_name="k", state="closed"),
        )
        breaker = CircuitBreakerPolicy(service_name="k", cb_service=cb_service)
        error = Exception("429 too many requests")
        seen: dict = {}

        def body():
            seen["scope"] = _current_scope()
            raise error

        protected = _decorated(
            coordinator_no_jitter_no_debounce, "k", body, is_async=is_async
        )
        shared_service = MagicMock(spec=CircuitBreakerService)

        with (
            patch(_OBS_TRACKER, return_value=MagicMock(spec=RateLimitTracker)),
            patch(_OBS_CB_SERVICE, return_value=shared_service),
            patch.object(
                RateLimitCoordinator, "get_instance", autospec=True
            ) as singleton,
            pytest.raises(Exception) as exc_info,
        ):
            if is_async:
                asyncio.run(AsyncCircuitBreakerPolicy(breaker).execute(protected))
            else:
                breaker.execute(protected)

        assert exc_info.value is error
        assert mock_storage.get_state("k").consecutive_429s == 1
        assert mock_storage.get_state("k").consecutive_429s != 0
        singleton.assert_not_called()
        cb_service.record_failure.assert_called_once()
        # The cascade half is the decorator's too: the mark it leaves stops the
        # breaker frame noting the 429, so nobody else can count it. It lands
        # on the breaker's own service, which the scope carries.
        assert seen["scope"].rate_limited == 1
        cb_service.record_rate_limit_response.assert_called_once_with("k")
        shared_service.record_rate_limit_response.assert_not_called()

    @_DECORATED_SURFACES
    def test_a_returning_client_under_a_breaker_frame_feeds_the_cascade_once(
        self, coordinator_no_jitter_no_debounce, mock_storage, is_async
    ):
        """The returned-429 twin of the row above: one cooldown, one cascade note.

        Regression: the ownership mark alone suppressed the breaker frame's
        cascade note without the decorator noting the 429 itself, so a
        decorated client under ``protect(name, retry=False)`` fed the breaker's
        429 detector nothing at all.
        """
        from baldur.interfaces.repositories import CircuitBreakerStateData
        from baldur.services.circuit_breaker.config import CircuitBreakerDecision
        from baldur.services.circuit_breaker.policy import (
            AsyncCircuitBreakerPolicy,
            CircuitBreakerPolicy,
        )
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )
        from baldur.services.circuit_breaker.service import CircuitBreakerService
        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        cb_service = MagicMock(spec=CircuitBreakerService)
        cb_service.is_enabled = True
        cb_service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=True,
            state=CircuitBreakerStateData(service_name="k", state="closed"),
        )
        breaker = CircuitBreakerPolicy(service_name="k", cb_service=cb_service)
        response = type("Response", (), {"status_code": 429, "headers": {}})()
        seen: dict = {}

        def body():
            seen["scope"] = _current_scope()
            return response

        protected = _decorated(
            coordinator_no_jitter_no_debounce, "k", body, is_async=is_async
        )
        shared_service = MagicMock(spec=CircuitBreakerService)

        with (
            patch(_OBS_TRACKER, return_value=MagicMock(spec=RateLimitTracker)),
            patch(_OBS_CB_SERVICE, return_value=shared_service),
            patch.object(
                RateLimitCoordinator, "get_instance", autospec=True
            ) as singleton,
        ):
            if is_async:
                asyncio.run(AsyncCircuitBreakerPolicy(breaker).execute(protected))
            else:
                breaker.execute(protected)

        assert mock_storage.get_state("k").consecutive_429s == 1
        singleton.assert_not_called()
        assert seen["scope"].rate_limited == 1
        assert seen["scope"].rate_limited != 0
        cb_service.record_rate_limit_response.assert_called_once_with("k")
        shared_service.record_rate_limit_response.assert_not_called()


class TestRateLimitAwareAsyncDecoratorCascadeNoteBehavior:
    """On an ``async def`` the decorator's 429 cascade note leaves the event loop.

    The identity mark is a list append and stays inline; the note reaches the
    breaker's tracker and can trip the breaker, so it is hopped like the
    cooldown write. Regression: it ran inline on the loop (792 /verify).
    """

    @pytest.fixture(autouse=True)
    def _no_cluster_broadcast(self):
        """Neutralise the Dormant-tier cluster broadcast a real 429 would fire."""
        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
            yield

    @pytest.mark.parametrize(
        "shape", ["raised", "returned"], ids=["raised_429", "returned_429"]
    )
    def test_the_cascade_note_runs_off_the_loop_thread(
        self, coordinator_no_jitter_no_debounce, shape
    ):
        """``record_rate_limit_response`` is reached from a thread that is not the loop's."""
        from baldur.services.circuit_breaker.rate_limit_observation import (
            close_scope,
            open_scope,
        )
        from baldur.services.circuit_breaker.rate_limit_tracker import RateLimitTracker
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        cascade_service = MagicMock(spec=CircuitBreakerService)
        seen_threads: list[threading.Thread] = []
        cascade_service.record_rate_limit_response.side_effect = lambda *_a, **_k: (
            seen_threads.append(threading.current_thread())
        )
        error = Exception("429 too many requests")
        response = type("Response", (), {"status_code": 429})()

        def body():
            if shape == "raised":
                raise error
            return response

        protected = _decorated(
            coordinator_no_jitter_no_debounce, "k", body, is_async=True
        )

        token, scope = open_scope("k")
        try:
            with (
                patch(_OBS_CB_SERVICE, return_value=cascade_service),
                patch(_OBS_TRACKER, return_value=MagicMock(spec=RateLimitTracker)),
            ):
                loop_thread = threading.current_thread()
                if shape == "raised":
                    with pytest.raises(Exception):
                        _call(protected, is_async=True)
                else:
                    _call(protected, is_async=True)
        finally:
            close_scope(token)

        assert scope.rate_limited == 1
        assert seen_threads != []
        assert all(thread is not loop_thread for thread in seen_threads)
