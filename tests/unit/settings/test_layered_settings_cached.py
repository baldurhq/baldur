"""Unit tests for the hot-path cached layered settings helper (686 D3).

``get_layered_settings_cached`` wraps ``get_layered_settings`` in a per-process,
per-``config_type`` TTL snapshot so request-rate consumers do not pay a layered
read's Pydantic-validation cost on every call.

Verification techniques applied:
- Contract: ``LAYERED_SETTINGS_CACHE_TTL_SECONDS`` value (hardcoded assertion)
- Time dependency: TTL expiry driven by a patched ``time.monotonic`` (no sleeps)
- Idempotency / side effects: a warm hit returns the cached instance without
  recomputing; an expired entry recomputes
- Dependency interaction: exact recompute (underlying-call) counts
- Singleton/lifecycle: ``reset_layered_settings_cached`` clears the snapshot
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.settings import layered_provider
from baldur.settings.idempotency import IdempotencySettings
from baldur.settings.layered_provider import (
    LAYERED_SETTINGS_CACHE_TTL_SECONDS,
    get_layered_settings_cached,
    reset_layered_settings_cached,
)
from baldur.settings.metrics import MetricsSettings


class _FakeMonotonic:
    """Controllable stand-in for the module-level ``time`` reference.

    ``get_layered_settings_cached`` only calls ``time.monotonic()``, so a fake
    exposing that single method fully controls the cache clock — the monotonic
    patching the Testability Notes call for, without any sleeps.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _counting_layered():
    """Return (call-counter, side_effect) for patching ``get_layered_settings``.

    Each underlying compute returns a distinct string so identity (``is``) and
    equality distinguish a cached hit from a recompute.
    """
    calls = {"n": 0}

    def _side(settings_class, config_type):
        calls["n"] += 1
        return f"{config_type}:{calls['n']}"

    return calls, _side


@pytest.fixture(autouse=True)
def _reset_cache():
    # Explicit per-test reset — the cache is the xdist-flaky cached-singleton
    # class flagged in the Testability Notes; conftest also resets it, but this
    # keeps the file self-contained.
    reset_layered_settings_cached()
    yield
    reset_layered_settings_cached()


class TestLayeredSettingsCachedContract:
    def test_cache_ttl_constant_is_30_seconds(self):
        """686 D3 pins the hot-path snapshot TTL at 30s (a module constant,
        deliberately NOT a BALDUR_* field)."""
        assert LAYERED_SETTINGS_CACHE_TTL_SECONDS == 30.0


class TestLayeredSettingsCachedBehavior:
    def test_first_call_computes_and_caches_the_layered_result(self):
        # Given a cold cache
        calls, side = _counting_layered()
        clock = _FakeMonotonic()

        # When the helper is called once
        with (
            patch.object(layered_provider, "time", clock),
            patch.object(layered_provider, "get_layered_settings", side_effect=side),
        ):
            result = get_layered_settings_cached(IdempotencySettings, "idempotency")

        # Then the layered read runs exactly once and its result is returned
        assert result == "idempotency:1"
        assert calls["n"] == 1

    def test_warm_hit_within_ttl_returns_cached_instance_without_recompute(self):
        # Given a populated cache
        calls, side = _counting_layered()
        clock = _FakeMonotonic()

        with (
            patch.object(layered_provider, "time", clock),
            patch.object(layered_provider, "get_layered_settings", side_effect=side),
        ):
            first = get_layered_settings_cached(IdempotencySettings, "idempotency")
            # When time advances but stays strictly within the TTL window
            clock.advance(LAYERED_SETTINGS_CACHE_TTL_SECONDS - 0.001)
            second = get_layered_settings_cached(IdempotencySettings, "idempotency")

        # Then the same cached object is returned and no recompute happened
        assert first is second
        assert calls["n"] == 1

    def test_expired_entry_recomputes_and_returns_fresh_value(self):
        # Given a populated cache
        calls, side = _counting_layered()
        clock = _FakeMonotonic()

        with (
            patch.object(layered_provider, "time", clock),
            patch.object(layered_provider, "get_layered_settings", side_effect=side),
        ):
            first = get_layered_settings_cached(IdempotencySettings, "idempotency")
            # When the TTL has fully elapsed
            clock.advance(LAYERED_SETTINGS_CACHE_TTL_SECONDS + 1.0)
            second = get_layered_settings_cached(IdempotencySettings, "idempotency")

        # Then the layered read runs again and a fresh value is returned
        assert calls["n"] == 2
        assert first == "idempotency:1"
        assert second == "idempotency:2"

    def test_ttl_boundary_is_exclusive_at_exactly_expiry(self):
        # The freshness check is ``entry.expiry > now`` (strict), so an entry is
        # already stale at the exact expiry instant.
        calls, side = _counting_layered()
        clock = _FakeMonotonic(start=0.0)

        with (
            patch.object(layered_provider, "time", clock),
            patch.object(layered_provider, "get_layered_settings", side_effect=side),
        ):
            get_layered_settings_cached(IdempotencySettings, "idempotency")  # expiry=30
            clock.advance(LAYERED_SETTINGS_CACHE_TTL_SECONDS)  # now == expiry
            get_layered_settings_cached(IdempotencySettings, "idempotency")

        assert calls["n"] == 2

    def test_distinct_config_types_are_cached_independently(self):
        # Given two different config_types read through the cache
        calls, side = _counting_layered()
        clock = _FakeMonotonic()

        with (
            patch.object(layered_provider, "time", clock),
            patch.object(layered_provider, "get_layered_settings", side_effect=side),
        ):
            idem = get_layered_settings_cached(IdempotencySettings, "idempotency")
            metrics = get_layered_settings_cached(MetricsSettings, "metrics")
            # When the first config_type is read again within its TTL
            idem_again = get_layered_settings_cached(IdempotencySettings, "idempotency")

        # Then each config_type computed once and the repeat hit its own entry
        assert calls["n"] == 2
        assert idem == "idempotency:1"
        assert metrics == "metrics:2"
        assert idem_again == idem

    def test_reset_clears_cache_forcing_recompute(self):
        # Given a populated cache
        calls, side = _counting_layered()
        clock = _FakeMonotonic()

        with (
            patch.object(layered_provider, "time", clock),
            patch.object(layered_provider, "get_layered_settings", side_effect=side),
        ):
            get_layered_settings_cached(IdempotencySettings, "idempotency")
            # When the cache is reset within the TTL window
            reset_layered_settings_cached()
            get_layered_settings_cached(IdempotencySettings, "idempotency")

        # Then the next read recomputes despite being within the TTL
        assert calls["n"] == 2
