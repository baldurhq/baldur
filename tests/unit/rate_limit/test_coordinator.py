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
import time
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time

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

    def test_metrics_fail_open_on_import_error(self):
        """A broken metric definition passes without raising."""
        from baldur.services.rate_limit_coordinator import (
            _record_rate_limit_429,
            _record_rate_limit_cooldown,
        )

        with patch(
            "baldur.services.metrics.definitions.rate_limit_429_total",
            side_effect=AttributeError("no such metric"),
        ):
            _record_rate_limit_429(key="test")
            _record_rate_limit_cooldown(
                key="test", cooldown_seconds=1.0, consecutive_429s=1
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


class TestOnRateLimitedStorageUnavailable:
    """A failing coordination store must not erase the evidence of the storm.

    Every caller wraps ``on_rate_limited`` fail-open, so a storage fault leaves
    no trace in the business path — the metrics are the only surviving signal,
    and they have to survive exactly the outage an operator needs them for.
    Each recording site therefore sits at the earliest point where its values
    are already true, which is what these exit paths pin.
    """

    @pytest.mark.parametrize(
        "failing_call",
        ["increment_consecutive_429s", "set_cooldown"],
        ids=["increment", "set_cooldown"],
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

    def test_storage_unavailable_on_set_cooldown_keeps_the_429_cooldown_values(
        self, mock_storage
    ):
        """A failing store still leaves the computed cooldown recorded.

        Positive half of the ordering: the cooldown is computed and recorded
        before it is stored, so a store that then raises invalidates neither
        number. The storage-degradation alert reads exactly this asymmetry —
        429s climbing while cooldown observations do not.
        """
        from baldur.interfaces.rate_limit_storage import (
            RateLimitStorageUnavailableError,
        )

        key = _unique_key("unavailable_after_cooldown")
        coordinator = _deterministic_coordinator(mock_storage)

        with patch.object(
            mock_storage,
            "set_cooldown",
            side_effect=RateLimitStorageUnavailableError("coordination store down"),
        ):
            with pytest.raises(RateLimitStorageUnavailableError):
                coordinator.on_rate_limited(key)

        assert _sample("baldur_rate_limit_429_total", key=key, status_code="429") == 1.0
        assert _sample("baldur_rate_limit_cooldown_seconds_count", key=key) == 1.0
        assert _sample("baldur_rate_limit_consecutive_429s", key=key) == 1.0


# =============================================================================
# Wait-or-defer decision metrics
# =============================================================================


class TestWaitIfNeededMetrics:
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


class TestWaitDeferralHelpers:
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
# _schedule_cooldown_end_event scheduling
# =============================================================================


class TestRateLimitCoordinatorScheduleCooldownEnd:
    """_schedule_cooldown_end_event scheduling tests."""

    def test_schedule_skipped_when_delay_is_zero_or_negative(self, mock_storage):
        """A past cooldown_until arms no timer."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        coordinator._schedule_cooldown_end_event("test_api", time.time() - 5)
        assert "test_api" not in coordinator._cooldown_timers

    def test_schedule_cancels_existing_timer(self, mock_storage):
        """Re-scheduling the same key replaces its timer."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        coordinator._schedule_cooldown_end_event("test_api", time.time() + 60)
        first_timer = coordinator._cooldown_timers.get("test_api")
        assert first_timer is not None

        coordinator._schedule_cooldown_end_event("test_api", time.time() + 120)
        second_timer = coordinator._cooldown_timers.get("test_api")
        assert second_timer is not first_timer

        second_timer.cancel()


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
