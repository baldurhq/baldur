"""
PoolCircuitBreakerSettings unit tests.

Validates the Django pool-aware circuit-breaker middleware settings:
- default values
- environment-variable overrides (incl. the renamed half-open field)
- field validation (min/max ranges) rejected loudly
- the dead stale-warning-tier UserWarning
- singleton pattern
"""

import pytest
from pydantic import ValidationError


class TestPoolCircuitBreakerSettings:
    """PoolCircuitBreakerSettings tests."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset the singleton before and after each test."""
        from baldur.settings.pool_circuit_breaker import (
            reset_pool_circuit_breaker_settings,
        )

        reset_pool_circuit_breaker_settings()
        yield
        reset_pool_circuit_breaker_settings()

    def test_default_values(self):
        """Default values match the middleware's expected defaults."""
        from baldur.settings.pool_circuit_breaker import PoolCircuitBreakerSettings

        settings = PoolCircuitBreakerSettings()

        assert settings.failure_threshold == 3
        assert settings.success_threshold == 2
        assert settings.recovery_timeout == 10
        assert settings.half_open_max_requests == 3
        assert settings.cache_interval_ms == 100
        assert settings.stale_multiplier == 10
        assert settings.critical_stale_ms == 5000

    def test_env_override_all_dials(self, monkeypatch):
        """Each dial is overridable, including the renamed half-open field."""
        from baldur.settings.pool_circuit_breaker import PoolCircuitBreakerSettings

        monkeypatch.setenv("BALDUR_POOL_CB_FAILURE_THRESHOLD", "7")
        monkeypatch.setenv("BALDUR_POOL_CB_SUCCESS_THRESHOLD", "4")
        monkeypatch.setenv("BALDUR_POOL_CB_RECOVERY_TIMEOUT", "30")
        monkeypatch.setenv("BALDUR_POOL_CB_HALF_OPEN_MAX_REQUESTS", "5")
        monkeypatch.setenv("BALDUR_POOL_CB_CACHE_INTERVAL_MS", "200")
        monkeypatch.setenv("BALDUR_POOL_CB_STALE_MULTIPLIER", "20")
        monkeypatch.setenv("BALDUR_POOL_CB_CRITICAL_STALE_MS", "8000")

        settings = PoolCircuitBreakerSettings()
        assert settings.failure_threshold == 7
        assert settings.success_threshold == 4
        assert settings.recovery_timeout == 30
        assert settings.half_open_max_requests == 5
        assert settings.cache_interval_ms == 200
        assert settings.stale_multiplier == 20
        assert settings.critical_stale_ms == 8000

    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [
            ("failure_threshold", 0),
            ("failure_threshold", 101),
            ("success_threshold", 0),
            ("success_threshold", 101),
            ("recovery_timeout", 0),
            ("recovery_timeout", 3601),
            ("half_open_max_requests", 0),
            ("half_open_max_requests", 101),
            ("cache_interval_ms", 49),
            ("cache_interval_ms", 1001),
            ("stale_multiplier", 0),
            ("stale_multiplier", 101),
            ("critical_stale_ms", 999),
            ("critical_stale_ms", 60001),
        ],
    )
    def test_field_bounds_violation_rejected(self, field, bad_value):
        """Out-of-range values fail loudly at load (no silent clamp)."""
        from baldur.settings.pool_circuit_breaker import PoolCircuitBreakerSettings

        with pytest.raises(ValidationError):
            PoolCircuitBreakerSettings(**{field: bad_value})

    @pytest.mark.parametrize(
        ("field", "edge_value"),
        [
            ("cache_interval_ms", 50),
            ("cache_interval_ms", 1000),
            ("critical_stale_ms", 1000),
            ("critical_stale_ms", 60000),
        ],
    )
    def test_field_bounds_edge_accepted(self, field, edge_value):
        """Boundary values are accepted."""
        from baldur.settings.pool_circuit_breaker import PoolCircuitBreakerSettings

        settings = PoolCircuitBreakerSettings(**{field: edge_value})
        assert getattr(settings, field) == edge_value

    def test_dead_stale_warning_tier_warns(self):
        """interval*multiplier >= critical_stale_ms makes the warning tier dead."""
        from baldur.settings.pool_circuit_breaker import PoolCircuitBreakerSettings

        # 500 * 10 = 5000 == default critical_stale_ms 5000 -> tier unreachable
        with pytest.warns(UserWarning, match="stale-warning tier will never fire"):
            PoolCircuitBreakerSettings(cache_interval_ms=500, stale_multiplier=10)

    def test_healthy_stale_tiers_no_warning(self, recwarn):
        """A well-separated staleness config does not warn."""
        from baldur.settings.pool_circuit_breaker import PoolCircuitBreakerSettings

        PoolCircuitBreakerSettings(
            cache_interval_ms=100, stale_multiplier=10, critical_stale_ms=5000
        )
        dead_tier = [
            w
            for w in recwarn.list
            if "stale-warning tier will never fire" in str(w.message)
        ]
        assert not dead_tier

    def test_singleton_pattern(self):
        """get_pool_circuit_breaker_settings returns a cached singleton."""
        from baldur.settings.pool_circuit_breaker import (
            get_pool_circuit_breaker_settings,
            reset_pool_circuit_breaker_settings,
        )

        settings1 = get_pool_circuit_breaker_settings()
        settings2 = get_pool_circuit_breaker_settings()
        assert settings1 is settings2

        reset_pool_circuit_breaker_settings()
        settings3 = get_pool_circuit_breaker_settings()
        assert settings1 is not settings3

    def test_singleton_env_reload(self, monkeypatch):
        """A reset after an env change reflects the new value."""
        from baldur.settings.pool_circuit_breaker import (
            get_pool_circuit_breaker_settings,
            reset_pool_circuit_breaker_settings,
        )

        settings1 = get_pool_circuit_breaker_settings()
        assert settings1.failure_threshold == 3

        monkeypatch.setenv("BALDUR_POOL_CB_FAILURE_THRESHOLD", "9")
        reset_pool_circuit_breaker_settings()

        settings2 = get_pool_circuit_breaker_settings()
        assert settings2.failure_threshold == 9
