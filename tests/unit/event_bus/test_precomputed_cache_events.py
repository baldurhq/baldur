"""
Precomputed cache EventType and channel mapping tests (doc 445 G3).

Covers:
- Contract: PRECOMPUTED_CACHE_INVALIDATED exists with correct value,
  mapped to CONFIG channel
"""

from __future__ import annotations

from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.event_bus.redis_bus import EVENT_TYPE_TO_CHANNEL, EventChannel


class TestPrecomputedCacheEventTypeContract:
    """Contract verification for PRECOMPUTED_CACHE_INVALIDATED EventType."""

    def test_event_type_exists(self):
        """PRECOMPUTED_CACHE_INVALIDATED member exists in EventType enum."""
        assert hasattr(EventType, "PRECOMPUTED_CACHE_INVALIDATED")

    def test_event_type_value(self):
        """PRECOMPUTED_CACHE_INVALIDATED value is 'precomputed_cache_invalidated'."""
        assert (
            EventType.PRECOMPUTED_CACHE_INVALIDATED == "precomputed_cache_invalidated"
        )

    def test_channel_mapping_is_config(self):
        """PRECOMPUTED_CACHE_INVALIDATED maps to CONFIG channel."""
        channel = EVENT_TYPE_TO_CHANNEL[EventType.PRECOMPUTED_CACHE_INVALIDATED]
        assert channel == EventChannel.CONFIG
