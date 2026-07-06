"""
Tests for EventEmitterMixin — TTL-based backoff, fail-safe emission, and priority (381, 394).

EventBus lazy initialization, TTL negative caching, fail-safe _emit_event,
source name customization, priority parameter를 검증합니다.

참조 소스:
- services/event_bus/emitter.py (EventEmitterMixin, _UNAVAILABLE, _DEFAULT_RETRY_INTERVAL)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from baldur.services.event_bus.emitter import (
    _DEFAULT_RETRY_INTERVAL,
    _UNAVAILABLE,
    EventEmitterMixin,
)

# =============================================================================
# Test Subclass
# =============================================================================


class _TestEmitter(EventEmitterMixin):
    """Concrete subclass for testing."""

    _event_source = "test_service"


class _CustomRetryEmitter(EventEmitterMixin):
    """Subclass with custom retry interval."""

    _event_source = "custom_retry"
    _event_bus_retry_interval = 0.1  # 100ms for fast testing


# =============================================================================
# Contract Tests — 설계 계약값
# =============================================================================


class TestEventEmitterMixinDefaultsContract:
    """EventEmitterMixin 클래스 기본값 계약."""

    def test_base_event_source_is_empty_string(self):
        """Base mixin _event_source is empty string."""
        assert EventEmitterMixin._event_source == ""

    def test_default_retry_interval_is_60_seconds(self):
        """Default retry interval is 60 seconds."""
        assert _DEFAULT_RETRY_INTERVAL == 60.0

    def test_subclass_inherits_default_retry_interval(self):
        """Subclass without override inherits default retry interval."""
        assert _TestEmitter._event_bus_retry_interval == 60.0


# =============================================================================
# Behavior Tests — Lazy Initialization
# =============================================================================


class TestEventEmitterMixinInitializationBehavior:
    """_get_event_bus() lazy initialization."""

    def test_returns_event_bus_on_success(self):
        """Successful get_event_bus() caches and returns the bus."""
        emitter = _TestEmitter()
        mock_bus = MagicMock()

        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
        ):
            result = emitter._get_event_bus()

        assert result is mock_bus

    def test_caches_after_first_success(self):
        """Second call returns cached bus without re-importing."""
        emitter = _TestEmitter()
        mock_bus = MagicMock()

        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
        ) as mock_get:
            # When
            emitter._get_event_bus()
            emitter._get_event_bus()

        # Then
        mock_get.assert_called_once()


# =============================================================================
# Behavior Tests — TTL-Based Negative Caching
# =============================================================================


class TestEventEmitterMixinNegativeCachingBehavior:
    """_get_event_bus() TTL-based negative caching on failure."""

    def test_returns_none_on_initialization_failure(self):
        """Import failure returns None."""
        emitter = _TestEmitter()

        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=RuntimeError("unavailable"),
        ):
            result = emitter._get_event_bus()

        assert result is None

    def test_sets_unavailable_sentinel_on_failure(self):
        """Failed initialization sets _UNAVAILABLE sentinel."""
        emitter = _TestEmitter()

        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=RuntimeError("unavailable"),
        ):
            emitter._get_event_bus()

        assert emitter._event_bus is _UNAVAILABLE

    def test_suppresses_retry_within_ttl(self):
        """Within TTL, _get_event_bus() returns None without retrying."""
        emitter = _TestEmitter()

        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=RuntimeError("unavailable"),
        ) as mock_get:
            # When — first call fails, second call within TTL
            emitter._get_event_bus()
            result = emitter._get_event_bus()

        # Then
        assert result is None
        mock_get.assert_called_once()

    def test_retries_after_ttl_expires(self):
        """After TTL expires, _get_event_bus() retries initialization."""
        emitter = _CustomRetryEmitter()  # 100ms TTL
        mock_bus = MagicMock()
        base_time = time.monotonic()

        # Given — initial failure (at base_time)
        with patch("time.monotonic", return_value=base_time):
            with patch(
                "baldur.services.event_bus.get_event_bus",
                side_effect=RuntimeError("unavailable"),
            ):
                emitter._get_event_bus()

        # When — TTL expired (advance past 100ms), retry succeeds
        with patch("time.monotonic", return_value=base_time + 0.2):
            with patch(
                "baldur.services.event_bus.get_event_bus",
                return_value=mock_bus,
            ):
                result = emitter._get_event_bus()

        # Then
        assert result is mock_bus
        assert emitter._event_bus is mock_bus

    def test_re_fails_after_ttl_expires(self):
        """After TTL, retry that fails again re-applies negative caching."""
        emitter = _CustomRetryEmitter()  # 100ms TTL
        base_time = time.monotonic()

        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=RuntimeError("unavailable"),
        ) as mock_get:
            # Given — initial failure (at base_time)
            with patch("time.monotonic", return_value=base_time):
                emitter._get_event_bus()

            # When — TTL expired (advance past 100ms), retry also fails
            with patch("time.monotonic", return_value=base_time + 0.2):
                emitter._get_event_bus()

        # Then
        assert mock_get.call_count == 2
        assert emitter._event_bus is _UNAVAILABLE


# =============================================================================
# Behavior Tests — Fail-Safe _emit_event
# =============================================================================


class TestEventEmitterMixinEmitBehavior:
    """_emit_event fail-safe emission."""

    def test_emits_with_correct_source(self):
        """_emit_event passes _event_source to bus.emit()."""
        emitter = _TestEmitter()
        mock_bus = MagicMock()
        emitter._event_bus = mock_bus

        emitter._emit_event("test_event", {"key": "value"})

        mock_bus.emit.assert_called_once_with(
            "test_event",
            data={"key": "value"},
            source=_TestEmitter._event_source,
        )

    def test_skips_when_bus_is_unavailable(self):
        """_emit_event does nothing when EventBus is in UNAVAILABLE state."""
        emitter = _TestEmitter()
        emitter._event_bus = _UNAVAILABLE
        emitter._event_bus_fail_time = time.monotonic()

        # Should not raise
        emitter._emit_event("test_event", {"key": "value"})

    def test_catches_emit_exception(self):
        """_emit_event catches bus.emit() exceptions without propagation."""
        emitter = _TestEmitter()
        mock_bus = MagicMock()
        mock_bus.emit.side_effect = RuntimeError("bus down")
        emitter._event_bus = mock_bus

        # Should not raise
        emitter._emit_event("test_event", {"key": "value"})

    def test_skips_when_bus_returns_none(self):
        """_emit_event skips when _get_event_bus returns None."""
        emitter = _TestEmitter()

        with patch.object(emitter, "_get_event_bus", return_value=None):
            # Should not raise
            emitter._emit_event("test_event", {"key": "value"})


# =============================================================================
# Behavior Tests — Source Name Customization
# =============================================================================


class TestEventEmitterMixinSourceCustomizationBehavior:
    """_event_source class variable customization per subclass."""

    def test_subclass_overrides_source(self):
        """Subclass _event_source is accessible on instance."""
        emitter = _TestEmitter()
        assert emitter._event_source == _TestEmitter._event_source

    def test_different_subclasses_have_independent_sources(self):
        """Each subclass maintains its own _event_source independently."""
        a = _TestEmitter()
        b = _CustomRetryEmitter()
        assert a._event_source == _TestEmitter._event_source
        assert b._event_source == _CustomRetryEmitter._event_source
        assert a._event_source != b._event_source


# =============================================================================
# Behavior Tests — Priority Parameter (394 D0)
# =============================================================================


class TestEventEmitterMixinPriorityBehavior:
    """D0: EventEmitterMixin._emit_event priority parameter."""

    def test_emit_event_without_priority_uses_positional(self):
        """Without priority, bus.emit is called with data and source only."""
        emitter = _TestEmitter()
        mock_bus = MagicMock()
        emitter._event_bus = mock_bus

        emitter._emit_event("TEST_EVENT", data={"key": "value"})

        mock_bus.emit.assert_called_once_with(
            "TEST_EVENT", data={"key": "value"}, source="test_service"
        )

    def test_emit_event_with_priority_passes_priority(self):
        """With priority, bus.emit receives priority in kwargs."""
        emitter = _TestEmitter()
        mock_bus = MagicMock()
        emitter._event_bus = mock_bus

        emitter._emit_event("TEST_EVENT", data={"key": "value"}, priority="HIGH")

        mock_bus.emit.assert_called_once_with(
            "TEST_EVENT", data={"key": "value"}, source="test_service", priority="HIGH"
        )

    def test_emit_event_priority_none_omits_priority_kwarg(self):
        """priority=None (default) does not pass priority to bus.emit."""
        emitter = _TestEmitter()
        mock_bus = MagicMock()
        emitter._event_bus = mock_bus

        emitter._emit_event("TEST_EVENT", data={}, priority=None)

        call_kwargs = mock_bus.emit.call_args[1]
        assert "priority" not in call_kwargs
