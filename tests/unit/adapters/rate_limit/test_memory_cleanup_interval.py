"""Unit tests for InMemoryRateLimitStorage cleanup-cadence injection (687 D4).

The expired-entry cleanup cadence moved from a hardcoded ``100`` to a
constructor parameter that resolves from
``RateLimitSettings.memory_cleanup_interval_ops`` when left unset.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestMemoryCleanupIntervalBehavior:
    """cleanup_interval injection vs settings resolution — 687 D4."""

    def test_explicit_cleanup_interval_is_used(self):
        # Given/When: an explicit cadence is injected
        from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage

        storage = InMemoryRateLimitStorage(cleanup_interval=7)

        # Then: the injected value wins without consulting settings
        assert storage._cleanup_interval == 7

    def test_none_resolves_from_settings(self):
        # Given: settings supply a non-default cadence
        from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage

        settings = MagicMock()
        settings.memory_cleanup_interval_ops = 250

        # When: the cadence is left unset (None)
        with patch(
            "baldur.settings.rate_limit.get_rate_limit_settings",
            return_value=settings,
        ):
            storage = InMemoryRateLimitStorage()

        # Then: it resolves from RateLimitSettings.memory_cleanup_interval_ops
        assert storage._cleanup_interval == 250

    def test_default_cadence_matches_settings_default(self):
        # Given/When: no injection and real settings
        from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
        from baldur.settings.rate_limit import (
            RateLimitSettings,
            reset_rate_limit_settings,
        )

        reset_rate_limit_settings()
        storage = InMemoryRateLimitStorage()

        # Then: the resolved default equals the settings field default
        assert (
            storage._cleanup_interval
            == RateLimitSettings.model_fields["memory_cleanup_interval_ops"].default
        )
