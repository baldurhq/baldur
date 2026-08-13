"""Admission against real sockets, and the rate-limit lane end to end.

Two claims in this file cannot be reached with mocks.

The first is the probe's cost. Its retry band is selected by *measured*
elapsed time against a real connect, because redis-py raises the same
``TimeoutError`` whether the connect or the read timed out. A fake client can
be told to look like either one; only a real socket proves the classification
holds against the failures the OS actually produces. What is asserted is the
timeout *class*, never a number of seconds: a refusal returns immediately on
Linux and is slow enough on some Windows hosts to be classified as a connect
timeout — the same verdict, one retry apart — so the bound that matters is
that admission costs less than the data-path connect budget it replaced.

The second is the rate-limit lane. It is genuine composition rather than
delegation: the storage accessor resolves through the provider registry (a
per-slot lock plus an instance cache the auto-detect loop invalidates on
failure), into the provider factory, into the connection factory, and back out
to a process-wide gauge and a module-level report latch. Four objects share
mutable state across repeated resolutions, which is exactly what the latch
assertion measures.

No infrastructure is required. The unreachable addresses are a bound-then-
closed loopback port and a TEST-NET address that nothing routes; the TEST-NET
leg skips itself on a network that answers the SYN.
"""

from __future__ import annotations

import socket
import time

import pytest
from structlog.testing import capture_logs

from baldur.adapters.rate_limit import get_rate_limit_storage
from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.adapters.redis.connection_factory import (
    _PROBE_CONNECT_ATTEMPTS,
    RedisConnectionFactory,
    configure_redis_connection_factory,
    reset_redis_connection_factory,
)
from baldur.factory import adapters as adapters_module
from baldur.factory.adapters import discover_rate_limit_storage_adapters
from baldur.factory.registry import ProviderRegistry
from baldur.metrics import drift_metrics
from baldur.settings.redis import RedisSettings, get_redis_settings
from tests.factories.redis_posture import refusing_redis_url

# Reserved for documentation; nothing routes it, so a connect gets no answer
# at all rather than an RST.
_BLACK_HOLE_HOST = "192.0.2.1"
_BLACK_HOLE_URL = f"redis://{_BLACK_HOLE_HOST}:6379/0"

# Long enough to tell "no answer" from "slow answer", short enough that the
# skip decision is not itself a stall.
_BLACK_HOLE_PRECHECK_SECONDS = 0.2

# Absorbs scheduler noise and the dual-stack resolution some hosts do. Large
# relative to the probe budget and still far below the data-path budget the
# assertions separate it from.
_TIMING_SLACK_SECONDS = 1.5

# The zero-config first-call budget: framework-side resolution must not be
# something a caller's own timeout notices.
_UNCONFIGURED_RESOLUTION_BUDGET_SECONDS = 1.0

_RESOLUTION_ATTEMPTS = 3

# Not the shipped 0.5: the configured-but-down lane re-probes on every
# resolution, and a budget the default is not turns "the lane got faster" into
# evidence that the knob is read rather than a number being hardcoded.
_SHORTENED_PROBE_BUDGET_SECONDS = 0.15

PROBE_FAILED_EVENT = "rate_limit_storage.redis_probe_failed"
LATCH_ATTRIBUTE = "_rate_limit_redis_probe_failure_reported"


@pytest.fixture(autouse=True)
def _cleanup_between_tests():
    """Opt out of the lane's Redis-required cleanup.

    The package fixture this overrides depends on a live server and skips the
    whole module without one. Every case here is about what happens when there
    is no server, so it must run on a host that has none.
    """
    yield
    reset_redis_connection_factory()


@pytest.fixture(autouse=True)
def unlatched(monkeypatch):
    """Start every case with the announcement latch open."""
    monkeypatch.setattr(adapters_module, LATCH_ATTRIBUTE, False)


def _metric_value(metric, sample_name):
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


def _black_hole_is_unrouted() -> bool:
    """Does a connect to the TEST-NET address go unanswered on this host?

    A network that answers — a captive portal, a corporate proxy, a container
    network that RSTs unknown destinations — turns the black-hole leg into a
    refusal leg, which measures a different band.
    """
    sock = socket.socket()
    sock.settimeout(_BLACK_HOLE_PRECHECK_SECONDS)
    try:
        sock.connect((_BLACK_HOLE_HOST, 6379))
    except TimeoutError:
        return True
    except OSError:
        return False
    else:
        return False
    finally:
        sock.close()


@pytest.fixture(scope="session")
def black_hole_reachable() -> bool:
    return not _black_hole_is_unrouted()


@pytest.fixture
def probe_factory():
    """A factory on the shipped budgets, so the bounds are the shipped ones."""
    return RedisConnectionFactory(settings=RedisSettings())


def _probe_class_upper_bound(settings) -> float:
    """Every attempt the probe is allowed, plus slack.

    Derived from the setting and the attempt constant rather than written as a
    number: a change to either must move this bound with it.
    """
    return _PROBE_CONNECT_ATTEMPTS * settings.probe_connect_timeout + (
        _TIMING_SLACK_SECONDS
    )


class TestAdmissionProbeCostBehavior:
    """What admission costs against a socket that will not serve."""

    def test_refused_connect_fails_admission_within_the_probe_class(
        self, probe_factory
    ):
        """The dominant zero-config failure, on the budget the probe promises."""
        settings = probe_factory._settings
        url = refusing_redis_url()

        started = time.monotonic()
        with pytest.raises(Exception):
            probe_factory.probe(url)
        elapsed = time.monotonic() - started

        assert elapsed <= _probe_class_upper_bound(settings)

    def test_refused_connect_costs_less_than_the_data_path_connect_budget(
        self, probe_factory
    ):
        """The regression this bounds is the stall it replaced.

        Before the probe, admission was a ping on the client the site was
        about to keep, so it ran on ``socket_connect_timeout``.
        """
        settings = probe_factory._settings
        url = refusing_redis_url()

        started = time.monotonic()
        with pytest.raises(Exception):
            probe_factory.probe(url)
        elapsed = time.monotonic() - started

        assert elapsed < settings.socket_connect_timeout

    def test_unanswered_connect_fails_admission_within_the_probe_class(
        self, probe_factory, black_hole_reachable
    ):
        """A host that never answers is the worst case the band retry allows."""
        if black_hole_reachable:
            pytest.skip("this network answers the TEST-NET address")

        settings = probe_factory._settings

        started = time.monotonic()
        with pytest.raises(Exception):
            probe_factory.probe(_BLACK_HOLE_URL)
        elapsed = time.monotonic() - started

        assert (
            settings.probe_connect_timeout
            <= elapsed
            <= _probe_class_upper_bound(settings)
        )

    def test_unanswered_connect_costs_less_than_the_data_path_connect_budget(
        self, probe_factory, black_hole_reachable
    ):
        """Even the worst case stays inside a caller's own timeout window."""
        if black_hole_reachable:
            pytest.skip("this network answers the TEST-NET address")

        settings = probe_factory._settings

        started = time.monotonic()
        with pytest.raises(Exception):
            probe_factory.probe(_BLACK_HOLE_URL)
        elapsed = time.monotonic() - started

        assert elapsed < settings.socket_connect_timeout


@pytest.fixture
def rate_limit_lane():
    """The real registry, discovered fresh, with instances cleared.

    Snapshotted rather than reset: the providers registered at import time
    belong to the rest of the session.
    """
    registry = ProviderRegistry.rate_limit_storage
    with registry.snapshot():
        registry.clear_instances()
        discover_rate_limit_storage_adapters()
        assert registry.has_provider("redis") is True
        yield registry


@pytest.fixture
def live_connection_factory(no_redis_posture):
    """The real connection factory, pointed at an address nothing serves."""
    instance = RedisConnectionFactory(settings=get_redis_settings())
    configure_redis_connection_factory(instance)
    yield instance
    reset_redis_connection_factory()


class TestUnconfiguredRateLimitLaneBehavior:
    """Zero config: the lane must not dial, and must not stall."""

    def test_first_resolution_returns_the_memory_store(
        self, live_connection_factory, rate_limit_lane
    ):
        """Coordination is per-process here, which is the documented posture."""
        assert isinstance(get_rate_limit_storage(), InMemoryRateLimitStorage)

    def test_first_resolution_stays_inside_the_first_call_budget(
        self, live_connection_factory, rate_limit_lane
    ):
        """The framework-side resolution a first protected call waits on.

        Generous by two orders of magnitude against the skip's real cost — the
        assertion is that no connect happens at all, and any connect would
        blow through it.
        """
        started = time.monotonic()
        get_rate_limit_storage()
        elapsed = time.monotonic() - started

        assert elapsed < _UNCONFIGURED_RESOLUTION_BUDGET_SECONDS

    def test_first_resolution_reports_the_fallback_it_is_running_in(
        self, live_connection_factory, rate_limit_lane
    ):
        """The shipped gauge survives the skip that removed its old writer."""
        drift_metrics.set_ratelimit_fallback_mode(False)

        get_rate_limit_storage()

        assert _fallback_gauge() == 1


class TestConfiguredButDownRateLimitLaneBehavior:
    """Somebody named a Redis and it is not answering."""

    @pytest.fixture(autouse=True)
    def _configured(self, no_redis_posture, monkeypatch):
        """Name the unreachable Redis, and shorten the probe budget.

        The shortened budget is not only about wall-clock: it is a value the
        shipped default is not, so a probe that ignored the knob and used its
        own number would show up as a lane that costs the same either way.
        """
        monkeypatch.setenv("BALDUR_REDIS_URL", no_redis_posture)
        get_redis_settings().probe_connect_timeout = _SHORTENED_PROBE_BUDGET_SECONDS

    def test_repeated_resolutions_fall_back_to_the_memory_store(
        self, live_connection_factory, rate_limit_lane
    ):
        """Degrades rather than raising, on every attempt."""
        results = [get_rate_limit_storage() for _ in range(_RESOLUTION_ATTEMPTS)]

        assert all(isinstance(r, InMemoryRateLimitStorage) for r in results)

    def test_outage_costs_one_warning_across_repeated_resolutions(
        self, live_connection_factory, rate_limit_lane
    ):
        """The latch is module state shared by every component that resolves."""
        with capture_logs() as logs:
            for _ in range(_RESOLUTION_ATTEMPTS):
                get_rate_limit_storage()

        warnings = [
            entry
            for entry in logs
            if entry["event"] == PROBE_FAILED_EVENT and entry["log_level"] == "warning"
        ]
        assert len(warnings) == 1

    def test_every_resolution_counts_its_own_failed_attempt(
        self, live_connection_factory, rate_limit_lane
    ):
        """Metrics stay per-attempt; only the announcement is latched."""
        before = _unavailable_counter()

        for _ in range(_RESOLUTION_ATTEMPTS):
            get_rate_limit_storage()

        assert _unavailable_counter() - before == _RESOLUTION_ATTEMPTS

    def test_repeated_resolutions_stay_inside_the_probe_class(
        self, live_connection_factory, rate_limit_lane
    ):
        """Each resolution re-probes, and each re-probe stays bounded."""
        settings = get_redis_settings()

        started = time.monotonic()
        for _ in range(_RESOLUTION_ATTEMPTS):
            get_rate_limit_storage()
        elapsed = time.monotonic() - started

        assert elapsed <= _RESOLUTION_ATTEMPTS * _probe_class_upper_bound(settings)

    def test_repeated_resolutions_cost_less_than_one_data_path_connect(
        self, live_connection_factory, rate_limit_lane
    ):
        """The discriminator against the defect this replaced.

        Admission used to be a ping on the client the provider was about to
        keep, so it ran on ``socket_connect_timeout``. Three resolutions that
        way cost three data-path budgets; three on the probe budget do not
        reach even one.
        """
        settings = get_redis_settings()

        started = time.monotonic()
        for _ in range(_RESOLUTION_ATTEMPTS):
            get_rate_limit_storage()
        elapsed = time.monotonic() - started

        assert elapsed < settings.socket_connect_timeout
