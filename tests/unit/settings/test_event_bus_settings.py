"""
EventBusSettings unit tests (doc 389).

Testable units:
- EventBusSettings: backend + redis_url fields
- get_event_bus_settings() / reset_event_bus_settings(): singleton pair
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# =============================================================================
# Contract: design-specified default values (doc 389)
# =============================================================================


@pytest.fixture(autouse=True)
def _clean_event_bus_env(monkeypatch):
    """Strip BALDUR_EVENT_BUS_ env vars so default-value contracts see the
    pydantic-declared defaults.

    The root ``tests/conftest.py`` injects test-env defaults via
    ``os.environ.setdefault`` (e.g. ``BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS=0.1``
    to shrink dispatch wait in CB tests). Those leak into
    ``EventBusSettings()`` and break the contract tests below that assert
    the production-default values. Removing them via monkeypatch for the
    duration of these tests restores the contract.
    """
    import os

    for key in list(os.environ):
        if key.startswith("BALDUR_EVENT_BUS_"):
            monkeypatch.delenv(key, raising=False)
    return


class TestEventBusSettingsContract:
    """EventBusSettings design contract verification."""

    def test_backend_default_is_memory(self):
        """backend default: 'memory' (L1 in-process only, doc 389)."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings()
        assert settings.backend == "memory"

    def test_redis_url_default_is_none(self):
        """redis_url default: None (falls back to BALDUR_REDIS_URL, doc 389)."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings()
        assert settings.redis_url is None

    def test_backend_allowed_values(self):
        """backend accepts only 'memory' or 'redis' (Literal type, doc 389)."""
        from pydantic import ValidationError

        from baldur.settings.event_bus import EventBusSettings

        EventBusSettings(backend="memory")
        EventBusSettings(backend="redis")
        with pytest.raises(ValidationError):
            EventBusSettings(backend="kafka")

    def test_env_prefix_is_baldur_eventbus(self):
        """env_prefix: BALDUR_EVENT_BUS_ (doc 389)."""
        from baldur.settings.event_bus import EventBusSettings

        assert EventBusSettings.model_config["env_prefix"] == "BALDUR_EVENT_BUS_"

    def test_handler_timeout_seconds_default(self):
        """handler_timeout_seconds default: 5.0 (doc 438)."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings()
        assert settings.handler_timeout_seconds == 5.0

    def test_handler_timeout_seconds_minimum_boundary(self):
        """handler_timeout_seconds minimum boundary: ge=0.0 (doc 438)."""
        from pydantic import ValidationError

        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings(handler_timeout_seconds=0.0)
        assert settings.handler_timeout_seconds == 0.0
        with pytest.raises(ValidationError):
            EventBusSettings(handler_timeout_seconds=-0.1)

    def test_handler_timeout_seconds_maximum_boundary(self):
        """handler_timeout_seconds maximum boundary: le=60.0 (doc 438)."""
        from pydantic import ValidationError

        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings(handler_timeout_seconds=60.0)
        assert settings.handler_timeout_seconds == 60.0
        with pytest.raises(ValidationError):
            EventBusSettings(handler_timeout_seconds=60.1)


# =============================================================================
# Contract: 487 — dispatch_mode / dispatch_workers
# =============================================================================


class TestEventBusDispatchSettingsContract:
    """487 D2: dispatch_mode + dispatch_workers field contract."""

    def test_dispatch_mode_default_is_async_pool(self):
        """dispatch_mode default: 'async_pool' (487 D2)."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings()
        assert settings.dispatch_mode == "async_pool"

    @pytest.mark.parametrize("mode", ["sync", "thread_per_emit", "async_pool"])
    def test_dispatch_mode_accepts_documented_values(self, mode):
        """dispatch_mode accepts the 3 documented Literal values (487 D2)."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings(dispatch_mode=mode)
        assert settings.dispatch_mode == mode

    def test_dispatch_mode_rejects_unknown_value(self):
        """dispatch_mode rejects values outside the Literal set."""
        from pydantic import ValidationError

        from baldur.settings.event_bus import EventBusSettings

        with pytest.raises(ValidationError):
            EventBusSettings(dispatch_mode="parallel")

    def test_dispatch_workers_default_is_32(self):
        """dispatch_workers default: 32 (487 D2 — TimeoutPolicy parity)."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings()
        assert settings.dispatch_workers == 32

    def test_dispatch_workers_lower_boundary(self):
        """dispatch_workers lower bound: ge=1 (1 valid, 0 invalid)."""
        from pydantic import ValidationError

        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings(dispatch_workers=1)
        assert settings.dispatch_workers == 1
        with pytest.raises(ValidationError):
            EventBusSettings(dispatch_workers=0)

    def test_dispatch_workers_upper_boundary(self):
        """dispatch_workers upper bound: le=64 (64 valid, 65 invalid)."""
        from pydantic import ValidationError

        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings(dispatch_workers=64)
        assert settings.dispatch_workers == 64
        with pytest.raises(ValidationError):
            EventBusSettings(dispatch_workers=65)


class TestEventBusDispatchSettingsEnvOverrideBehavior:
    """487 D2: env vars BALDUR_EVENT_BUS_DISPATCH_MODE / DISPATCH_WORKERS."""

    @patch.dict(
        "os.environ",
        {"BALDUR_EVENT_BUS_DISPATCH_MODE": "thread_per_emit"},
        clear=False,
    )
    def test_dispatch_mode_overridden_by_env_var(self):
        """BALDUR_EVENT_BUS_DISPATCH_MODE env var overrides default."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings()
        assert settings.dispatch_mode == "thread_per_emit"

    @patch.dict(
        "os.environ",
        {"BALDUR_EVENT_BUS_DISPATCH_WORKERS": "8"},
        clear=False,
    )
    def test_dispatch_workers_overridden_by_env_var(self):
        """BALDUR_EVENT_BUS_DISPATCH_WORKERS env var overrides default."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings()
        assert settings.dispatch_workers == 8


# =============================================================================
# Behavior: 487 D3 — reset_event_bus_settings drains dispatch executor
# =============================================================================


class TestEventBusSettingsResetCascadeBehavior:
    """487 D3: reset_event_bus_settings() drains BaldurEventBus dispatch executor."""

    def setup_method(self) -> None:
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        BaldurEventBus.shutdown_dispatch_executor()
        reset_event_bus_settings()

    def teardown_method(self) -> None:
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        BaldurEventBus.shutdown_dispatch_executor()
        reset_event_bus_settings()

    def test_reset_drains_dispatch_executor(self):
        """reset_event_bus_settings() clears BaldurEventBus._executor."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        BaldurEventBus._get_executor()
        assert BaldurEventBus._executor is not None

        reset_event_bus_settings()
        assert BaldurEventBus._executor is None

    def test_reset_idempotent_against_drained_executor(self):
        """Calling reset twice is harmless (no-op against an already-drained slot)."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        BaldurEventBus._get_executor()
        reset_event_bus_settings()
        # Second call must not raise.
        reset_event_bus_settings()
        assert BaldurEventBus._executor is None


# =============================================================================
# Behavior: env var override + singleton lifecycle
# =============================================================================


class TestEventBusSettingsEnvOverrideBehavior:
    """EventBusSettings environment variable override behavior."""

    @patch.dict(
        "os.environ",
        {"BALDUR_EVENT_BUS_BACKEND": "redis"},
        clear=False,
    )
    def test_backend_overridden_by_env_var(self):
        """BALDUR_EVENT_BUS_BACKEND env var overrides default."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings()
        assert settings.backend == "redis"

    @patch.dict(
        "os.environ",
        {"BALDUR_EVENT_BUS_REDIS_URL": "redis://custom:6379/0"},
        clear=False,
    )
    def test_redis_url_overridden_by_env_var(self):
        """BALDUR_EVENT_BUS_REDIS_URL env var overrides default None."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings()
        assert settings.redis_url == "redis://custom:6379/0"

    @patch.dict(
        "os.environ",
        {"BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS": "10.0"},
        clear=False,
    )
    def test_handler_timeout_seconds_overridden_by_env_var(self):
        """BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS env var overrides default."""
        from baldur.settings.event_bus import EventBusSettings

        settings = EventBusSettings()
        assert settings.handler_timeout_seconds == 10.0


class TestEventBusSettingsSingletonBehavior:
    """EventBusSettings singleton lifecycle via ServicesGroup."""

    def test_get_returns_settings_instance(self):
        """get_event_bus_settings() returns an EventBusSettings instance."""
        from baldur.settings.event_bus import (
            EventBusSettings,
            get_event_bus_settings,
        )

        settings = get_event_bus_settings()
        assert isinstance(settings, EventBusSettings)

    def test_get_returns_cached_instance(self):
        """get_event_bus_settings() returns the same cached instance."""
        from baldur.settings.event_bus import get_event_bus_settings

        first = get_event_bus_settings()
        second = get_event_bus_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset_event_bus_settings() clears the cached property."""
        from baldur.settings.event_bus import (
            get_event_bus_settings,
            reset_event_bus_settings,
        )

        first = get_event_bus_settings()
        reset_event_bus_settings()
        second = get_event_bus_settings()
        assert first is not second

    def test_reset_when_not_cached_does_not_raise(self):
        """reset_event_bus_settings() is safe when property is not cached."""
        from baldur.settings.event_bus import reset_event_bus_settings

        # Should not raise KeyError
        reset_event_bus_settings()
        reset_event_bus_settings()


class TestServicesGroupEventBusBehavior:
    """ServicesGroup.event_bus wiring verification."""

    def test_services_group_has_event_bus_property(self):
        """ServicesGroup exposes event_bus as cached_property."""
        from baldur.settings.groups import ServicesGroup

        assert hasattr(ServicesGroup, "event_bus")

    def test_services_group_event_bus_returns_settings(self):
        """ServicesGroup.event_bus returns EventBusSettings instance."""
        from baldur.settings.event_bus import EventBusSettings
        from baldur.settings.groups import ServicesGroup

        group = ServicesGroup()
        settings = group.event_bus
        assert isinstance(settings, EventBusSettings)
