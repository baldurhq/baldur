"""The rate-limit provider's admission probe, and the signal it preserves.

``RedisRateLimitStorage.is_available()`` is the only writer of the fallback
gauge and the unavailable counter in the tree, and the auto-detect loop is its
only caller. Moving admission ahead of the adapter — so the connect runs on
the bounded probe budget instead of the data-path one — therefore removes the
writer, and the shipped "rate limiting fell back to per-process" gauge would
read 0 for the whole outage unless the probe site writes what that method
would have written.

The announcement is latched. ``get_rate_limit_storage()`` is re-entered by
every component that resolves storage, and the auto-detect loop invalidates
the cached instance on failure, so each entry re-attempts the provider. An
unlatched line is one WARNING per resolution rather than one per outage; the
metrics stay per-attempt, because a counter that only counted the first
failure would be the wrong shape.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from baldur.adapters.rate_limit import get_rate_limit_storage
from baldur.adapters.redis.connection_factory import (
    RedisConnectionFactory,
    configure_redis_connection_factory,
    reset_redis_connection_factory,
)
from baldur.factory import adapters as adapters_module
from baldur.factory.adapters import (
    _probe_rate_limit_redis,
    discover_rate_limit_storage_adapters,
)
from baldur.factory.registry import ProviderRegistry
from baldur.metrics import drift_metrics

PROBE_FAILED_EVENT = "rate_limit_storage.redis_probe_failed"
LATCH_ATTRIBUTE = "_rate_limit_redis_probe_failure_reported"
PROBE_TIMEOUT_ENV_VAR = "BALDUR_REDIS_PROBE_CONNECT_TIMEOUT"

# More than one, so "latched" is distinguishable from "logged once because it
# only ran once".
_RESOLUTION_ATTEMPTS = 3


def _metric_value(metric, sample_name):
    """Read one sample off a live Prometheus metric."""
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == sample_name:
                return sample.value
    raise AssertionError(f"no sample named {sample_name}")


def _fallback_gauge():
    return _metric_value(
        drift_metrics.ratelimit_fallback_active, "baldur_ratelimit_fallback_active"
    )


def _unavailable_counter():
    return _metric_value(
        drift_metrics.ratelimit_redis_unavailable_total,
        "baldur_ratelimit_redis_unavailable_total",
    )


@pytest.fixture(autouse=True)
def unlatched(monkeypatch):
    """Start every case with the announcement latch open.

    The latch is a module-level global that survives across tests, so a case
    that inherited a set latch would assert "logged once" against a lane that
    logged nothing.
    """
    monkeypatch.setattr(adapters_module, LATCH_ATTRIBUTE, False)


@pytest.fixture
def unreachable_factory():
    """A connection factory whose admission probe always refuses."""
    instance = MagicMock(spec=RedisConnectionFactory)
    instance.probe.side_effect = ConnectionError("connection refused")
    configure_redis_connection_factory(instance)
    yield instance
    reset_redis_connection_factory()


@pytest.fixture
def configured_but_down(no_redis_posture, monkeypatch):
    """Somebody named a Redis, and it is not answering.

    Built on the unconfigured posture plus one variable, so the two lanes
    differ by exactly the fact that separates them.
    """
    monkeypatch.setenv("BALDUR_REDIS_URL", no_redis_posture)
    return no_redis_posture


@pytest.fixture
def registered_providers():
    """The three rate-limit providers, registered and instance-free."""
    registry = ProviderRegistry.rate_limit_storage
    with registry.snapshot():
        registry.clear_instances()
        discover_rate_limit_storage_adapters()
        assert registry.has_provider("redis") is True
        yield registry


class TestRateLimitProbeFailureSignalBehavior:
    """What a refused admission probe leaves behind for the operator."""

    def test_failed_probe_still_fails_the_provider(self, unreachable_factory):
        """The exception must escape so auto-detect moves to the next backend."""
        with pytest.raises(ConnectionError):
            _probe_rate_limit_redis(unreachable_factory, "redis://host:6379/0")

    def test_successful_probe_clears_the_announcement_latch(self, unreachable_factory):
        """A recovered Redis re-arms the announcement for the next outage."""
        # Given: an outage has already been announced
        with pytest.raises(ConnectionError):
            _probe_rate_limit_redis(unreachable_factory, "redis://host:6379/0")
        assert getattr(adapters_module, LATCH_ATTRIBUTE) is True

        # When: the probe succeeds
        unreachable_factory.probe.side_effect = None
        _probe_rate_limit_redis(unreachable_factory, "redis://host:6379/0")

        # Then
        assert getattr(adapters_module, LATCH_ATTRIBUTE) is False

    def test_outage_costs_one_warning_across_repeated_resolutions(
        self, configured_but_down, unreachable_factory, registered_providers
    ):
        """One announcement per outage, not one per component that resolves."""
        with capture_logs() as logs:
            for _ in range(_RESOLUTION_ATTEMPTS):
                get_rate_limit_storage()

        warnings = [
            entry
            for entry in logs
            if entry["event"] == PROBE_FAILED_EVENT and entry["log_level"] == "warning"
        ]
        assert len(warnings) == 1

    def test_repeated_resolutions_each_reattempt_the_probe(
        self, configured_but_down, unreachable_factory, registered_providers
    ):
        """The latch silences the log, not the retry.

        Without this the "one WARNING" assertion above would also pass on an
        implementation that stopped probing after the first failure.
        """
        for _ in range(_RESOLUTION_ATTEMPTS):
            get_rate_limit_storage()

        assert unreachable_factory.probe.call_count == _RESOLUTION_ATTEMPTS

    def test_every_failed_attempt_increments_the_unavailable_counter(
        self, configured_but_down, unreachable_factory, registered_providers
    ):
        """Metrics are per-attempt — the latch covers the log line only."""
        before = _unavailable_counter()

        for _ in range(_RESOLUTION_ATTEMPTS):
            get_rate_limit_storage()

        assert _unavailable_counter() - before == _RESOLUTION_ATTEMPTS

    def test_failed_probe_arms_the_fallback_gauge(
        self, configured_but_down, unreachable_factory, registered_providers
    ):
        """The gauge reports the state the coordinator is actually in."""
        drift_metrics.set_ratelimit_fallback_mode(False)
        assert _fallback_gauge() == 0

        get_rate_limit_storage()

        assert _fallback_gauge() == 1

    def test_configured_redis_failure_warns_and_names_the_escape_hatch(
        self, configured_but_down, unreachable_factory, registered_providers
    ):
        """Somebody named this Redis, so its absence is an operational fault.

        The knob is named in the line so an operator whose Redis needs a
        longer connect than the probe budget does not have to go find it.
        """
        with capture_logs() as logs:
            get_rate_limit_storage()

        announcements = [
            entry for entry in logs if entry["event"] == PROBE_FAILED_EVENT
        ]
        assert [
            (entry["log_level"], entry.get("escape_hatch")) for entry in announcements
        ] == [("warning", PROBE_TIMEOUT_ENV_VAR)]

    def test_unconfigured_redis_failure_stays_at_debug(
        self, no_redis_posture, unreachable_factory, registered_providers, monkeypatch
    ):
        """Nobody named this address, so an unreachable one is not an incident.

        The auto-detect skip normally means the probe is never reached in this
        posture; the level split still has to hold, because an explicit
        ``backend="redis"`` ask reaches it with the same predicate answering
        True.
        """
        with capture_logs() as logs:
            with pytest.raises(ConnectionError):
                get_rate_limit_storage(backend="redis")

        announcements = [
            entry for entry in logs if entry["event"] == PROBE_FAILED_EVENT
        ]
        assert [entry["log_level"] for entry in announcements] == ["debug"]
