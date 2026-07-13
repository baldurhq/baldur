"""
PoolCircuitBreaker constructor settings-wiring regression tests.

Asserts the ``PoolCircuitBreaker.__init__`` maps every ``BALDUR_POOL_CB_*``
settings field onto its private attribute, and that an out-of-range value fails
loudly (ValidationError) instead of being silently clamped.

The constructor performs a single Act (settings mapping) and does not start the
background refresh thread — that is deferred to enabled-middleware
construction — so no thread teardown is needed.
"""

import pytest
from pydantic import ValidationError


class TestPoolCircuitBreakerSettingsWiring:
    """Constructor reads the BALDUR_POOL_CB_ settings layer."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """Reset both the settings singleton and the CB singleton per test."""
        from baldur.api.django.pool_circuit_breaker import PoolCircuitBreaker
        from baldur.settings.pool_circuit_breaker import (
            reset_pool_circuit_breaker_settings,
        )

        reset_pool_circuit_breaker_settings()
        PoolCircuitBreaker.reset_instance()
        yield
        reset_pool_circuit_breaker_settings()
        PoolCircuitBreaker.reset_instance()

    def test_ctor_maps_defaults(self):
        """With no env overrides the ctor picks up the field defaults."""
        from baldur.api.django.pool_circuit_breaker import PoolCircuitBreaker

        cb = PoolCircuitBreaker()

        assert cb._failure_threshold == 3
        assert cb._success_threshold == 2
        assert cb._recovery_timeout == 10
        assert cb._half_open_max_requests == 3
        assert cb._cache_interval_ms == 100
        assert cb._stale_threshold_multiplier == 10
        assert cb._critical_stale_ms == 5000

    def test_ctor_reads_env_overrides(self, monkeypatch):
        """Env overrides flow through the settings layer into ctor attributes."""
        from baldur.api.django.pool_circuit_breaker import PoolCircuitBreaker
        from baldur.settings.pool_circuit_breaker import (
            reset_pool_circuit_breaker_settings,
        )

        monkeypatch.setenv("BALDUR_POOL_CB_FAILURE_THRESHOLD", "7")
        monkeypatch.setenv("BALDUR_POOL_CB_HALF_OPEN_MAX_REQUESTS", "5")
        monkeypatch.setenv("BALDUR_POOL_CB_CACHE_INTERVAL_MS", "250")

        # Re-read env after setenv and rebuild the CB singleton.
        reset_pool_circuit_breaker_settings()
        PoolCircuitBreaker.reset_instance()
        cb = PoolCircuitBreaker()

        assert cb._failure_threshold == 7
        assert cb._half_open_max_requests == 5
        assert cb._cache_interval_ms == 250

    def test_ctor_fails_loud_on_out_of_range(self, monkeypatch):
        """An out-of-range dial raises ValidationError at construction."""
        from baldur.api.django.pool_circuit_breaker import PoolCircuitBreaker
        from baldur.settings.pool_circuit_breaker import (
            reset_pool_circuit_breaker_settings,
        )

        # 40 is below the ge=50 floor for the cache interval.
        monkeypatch.setenv("BALDUR_POOL_CB_CACHE_INTERVAL_MS", "40")

        reset_pool_circuit_breaker_settings()
        PoolCircuitBreaker.reset_instance()

        try:
            with pytest.raises(ValidationError):
                PoolCircuitBreaker()
        finally:
            # __init__ raised mid-construction, leaving a partially built
            # singleton whose thread-teardown attributes are unset; drop it so
            # the autouse teardown's reset_instance() has nothing to stop.
            PoolCircuitBreaker._instance = None
