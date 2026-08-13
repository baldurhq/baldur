"""Auto-detection stops dialing a Redis nobody named.

``get_rate_limit_storage()`` used to construct the Redis provider on every
install and let ``is_available()``'s failed ping decide. That ping is the
first connect in a "redis-py installed, no server" process, and it happens on
the first protected call — inside the caller's own timed section, where a
composed ``retry=True, timeout=5.0, fallback=...`` turns it into a fallback
that fired on a healthy function.

The skip is narrow on purpose and each half is asserted separately:

- It fires only on the auto-detect path. An explicit ``backend="redis"`` is
  somebody naming a Redis, and it still constructs.
- It writes the fallback gauge the construction it skipped used to write.
  Skipping without that write would leave the shipped "rate limiting fell back
  to per-process" signal reading 0 for every zero-config process — a silent
  under-report of exactly the state the gauge names.
- It does NOT increment the unavailable counter: nothing was attempted, so no
  attempt failed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from baldur.adapters.rate_limit import get_rate_limit_storage
from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.adapters.redis.connection_factory import (
    RedisConnectionFactory,
    configure_redis_connection_factory,
    reset_redis_connection_factory,
)
from baldur.factory import ProviderRegistry
from baldur.factory.adapters import discover_rate_limit_storage_adapters
from baldur.metrics import drift_metrics

SKIP_EVENT = "rate_limit_storage.redis_provider_skipped"


def _metric_value(metric, sample_name):
    """Read one sample off a live Prometheus metric.

    The gauge and the counter are process-global module attributes, so every
    case that reads one also pins it to a known value first.
    """
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


@pytest.fixture
def never_dialed_factory():
    """A connection factory that records any dial and makes none.

    Spy rather than stub: the assertion this lane exists for is that neither
    ``create`` nor ``probe`` is ever reached, and a factory that returned a
    working client would let a regression pass by falling back for a different
    reason.
    """
    instance = MagicMock(spec=RedisConnectionFactory)
    configure_redis_connection_factory(instance)
    yield instance
    reset_redis_connection_factory()


@pytest.fixture
def registered_providers():
    """The three rate-limit providers, registered and instance-free.

    Without the Redis provider actually registered the auto-detect loop skips
    it at ``has_provider`` and every assertion below would pass for the wrong
    reason.
    """
    registry = ProviderRegistry.rate_limit_storage
    with registry.snapshot():
        registry.clear_instances()
        discover_rate_limit_storage_adapters()
        assert registry.has_provider("redis") is True
        yield registry


class TestRateLimitAutoDetectSkipBehavior:
    """Auto-detection in the posture where nobody named a Redis."""

    def test_unconfigured_auto_detect_resolves_the_memory_store(
        self, no_redis_posture, never_dialed_factory, registered_providers
    ):
        """The resolved backend is the in-process one."""
        assert isinstance(get_rate_limit_storage(), InMemoryRateLimitStorage)

    def test_unconfigured_auto_detect_never_reaches_the_connection_factory(
        self, no_redis_posture, never_dialed_factory, registered_providers
    ):
        """Neither the client build nor the admission probe happens.

        Both spies matter: probing instead of constructing would still be a
        connect on the first protected call, just a cheaper one.
        """
        get_rate_limit_storage()

        never_dialed_factory.create.assert_not_called()
        never_dialed_factory.probe.assert_not_called()

    def test_unconfigured_auto_detect_announces_the_skip_at_debug(
        self, no_redis_posture, never_dialed_factory, registered_providers
    ):
        """Expected posture, not an incident — and it names its reason."""
        with capture_logs() as logs:
            get_rate_limit_storage()

        skips = [entry for entry in logs if entry["event"] == SKIP_EVENT]
        assert [(entry["log_level"], entry["reason"]) for entry in skips] == [
            ("debug", "redis_not_configured")
        ]

    def test_unconfigured_auto_detect_keeps_the_fallback_gauge_armed(
        self, no_redis_posture, never_dialed_factory, registered_providers
    ):
        """The gauge reports what it names: coordination is per-process now.

        Armed from 0 rather than read blind, so the write under test is the
        one being observed — the gauge is a process-global.
        """
        # Given: the gauge says "not falling back"
        drift_metrics.set_ratelimit_fallback_mode(False)
        assert _fallback_gauge() == 0

        # When
        get_rate_limit_storage()

        # Then
        assert _fallback_gauge() == 1

    def test_unconfigured_auto_detect_does_not_count_a_failed_attempt(
        self, no_redis_posture, never_dialed_factory, registered_providers
    ):
        """Nothing was attempted, so the unavailable counter must not move."""
        before = _unavailable_counter()

        get_rate_limit_storage()

        assert _unavailable_counter() == before

    def test_explicit_redis_backend_still_constructs_the_provider(
        self, no_redis_posture, never_dialed_factory, registered_providers
    ):
        """An explicit ask is somebody naming a Redis — the skip must not fire.

        The factory is reached; what it returns is beside the point, which is
        why the assertion is on the dial rather than on the instance type.
        """
        get_rate_limit_storage(backend="redis")

        never_dialed_factory.probe.assert_called_once()

    def test_configured_redis_is_not_skipped_by_the_auto_detect_loop(
        self, no_redis_posture, never_dialed_factory, registered_providers, monkeypatch
    ):
        """Naming a Redis by environment variable re-arms the whole path.

        The same fixture, one variable added: this is the boundary between the
        posture the skip is for and every deployment that configured Redis.
        """
        monkeypatch.setenv("BALDUR_REDIS_URL", no_redis_posture)

        get_rate_limit_storage()

        never_dialed_factory.probe.assert_called_once()
