"""
Tests for CircuitBreakerHandlerMixin (#413 D1-mixin).

Covers:
- _handle_cb_opened: save limit, reduce to cb_open_limit_percent
- _handle_cb_closed: restore saved limit, None fallback to initial_limit
- _handle_cb_half_opened: set to cb_half_open_limit_percent
- Self-source event skip (source="throttle")
- Metrics and audit recording on state changes
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import patch

from baldur.services.event_bus import BaldurEvent, EventType
from baldur_pro.services.throttle.adaptive import (
    get_adaptive_throttle,
    reset_adaptive_throttle,
)


def _make_cb_event(
    event_type: str,
    service_name: str = "payment_api",
    source: str = "circuit_breaker_service",
    **extra_data,
) -> BaldurEvent:
    """Create a CB EventBus event for testing."""
    data = {"service_name": service_name, **extra_data}
    return BaldurEvent(event_type=event_type, data=data, source=source)


class TestCBMixinOpenedBehavior:
    """_handle_cb_opened saves limit and reduces to CB open percent."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    @patch("baldur_pro.services.throttle.adaptive._circuit_breaker._record_audit_safe")
    def test_cb_opened_saves_current_limit_and_reduces_to_min(self, _mock_audit):
        """CB OPEN with default cb_open_limit_percent=0.0 reduces to min_limit."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 200

        event = _make_cb_event(EventType.CIRCUIT_BREAKER_OPENED)
        throttle._handle_cb_opened(event)

        # Saved pre-open limit
        assert throttle._limit_before_cb_open == 200
        # Reduced to min_limit (cb_open_limit_percent default is 0.0)
        assert throttle.current_limit == throttle.config.min_limit

    @patch("baldur_pro.services.throttle.adaptive._circuit_breaker._record_audit_safe")
    def test_cb_opened_with_nonzero_percent_calculates_correctly(self, _mock_audit):
        """CB OPEN with cb_open_limit_percent > 0 calculates initial * percent."""
        throttle = get_adaptive_throttle()
        # Override config percent for this test
        original_percent = throttle.config.cb_open_limit_percent
        try:
            throttle.config.cb_open_limit_percent = 0.3
            throttle.current_limit = 200

            event = _make_cb_event(EventType.CIRCUIT_BREAKER_OPENED)
            throttle._handle_cb_opened(event)

            expected = max(
                int(throttle.config.initial_limit * 0.3),
                throttle.config.min_limit,
            )
            assert throttle.current_limit == expected
        finally:
            throttle.config.cb_open_limit_percent = original_percent

    @patch("baldur_pro.services.throttle.adaptive._circuit_breaker._record_audit_safe")
    def test_cb_opened_skips_self_sourced_event(self, mock_audit):
        """Events with source='throttle' are ignored to prevent circular reference."""
        throttle = get_adaptive_throttle()
        original_limit = throttle.current_limit

        event = _make_cb_event(EventType.CIRCUIT_BREAKER_OPENED, source="throttle")
        throttle._handle_cb_opened(event)

        assert throttle.current_limit == original_limit
        mock_audit.assert_not_called()


class TestCBMixinClosedBehavior:
    """_handle_cb_closed restores saved limit or falls back to initial_limit."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    @patch("baldur_pro.services.throttle.adaptive._circuit_breaker._record_audit_safe")
    def test_cb_closed_restores_saved_limit(self, _mock_audit):
        """CB CLOSED restores the limit saved before CB OPEN."""
        throttle = get_adaptive_throttle()
        original_limit = throttle.config.initial_limit
        throttle.current_limit = original_limit

        # Simulate OPEN first
        throttle._limit_before_cb_open = original_limit
        throttle.current_limit = throttle.config.min_limit

        event = _make_cb_event(EventType.CIRCUIT_BREAKER_CLOSED)
        throttle._handle_cb_closed(event)

        assert throttle.current_limit == original_limit
        assert throttle._limit_before_cb_open is None

    @patch("baldur_pro.services.throttle.adaptive._circuit_breaker._record_audit_safe")
    def test_cb_closed_falls_back_to_initial_when_no_saved_limit(self, _mock_audit):
        """CB CLOSED with _limit_before_cb_open=None falls back to initial_limit."""
        throttle = get_adaptive_throttle()
        throttle._limit_before_cb_open = None
        throttle.current_limit = throttle.config.min_limit

        event = _make_cb_event(EventType.CIRCUIT_BREAKER_CLOSED)
        throttle._handle_cb_closed(event)

        assert throttle.current_limit == throttle.config.initial_limit

    @patch("baldur_pro.services.throttle.adaptive._circuit_breaker._record_audit_safe")
    def test_cb_closed_skips_self_sourced_event(self, mock_audit):
        """Events with source='throttle' are ignored."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = throttle.config.min_limit

        event = _make_cb_event(EventType.CIRCUIT_BREAKER_CLOSED, source="throttle")
        throttle._handle_cb_closed(event)

        assert throttle.current_limit == throttle.config.min_limit
        mock_audit.assert_not_called()


class TestCBMixinHalfOpenedBehavior:
    """_handle_cb_half_opened sets limit based on cb_half_open_limit_percent."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_cb_half_opened_sets_half_open_limit(self):
        """CB HALF_OPEN sets limit to initial_limit * cb_half_open_limit_percent."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = throttle.config.min_limit

        event = _make_cb_event(EventType.CIRCUIT_BREAKER_HALF_OPENED)
        throttle._handle_cb_half_opened(event)

        expected = max(
            int(
                throttle.config.initial_limit
                * throttle.config.cb_half_open_limit_percent
            ),
            throttle.config.min_limit,
        )
        assert throttle.current_limit == expected


class TestCBMixinOpenCloseLifecycleBehavior:
    """Full OPEN → HALF_OPEN → CLOSED lifecycle preserves limit."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    @patch("baldur_pro.services.throttle.adaptive._circuit_breaker._record_audit_safe")
    def test_open_then_close_restores_original_limit(self, _mock_audit):
        """OPEN → CLOSED cycle restores the pre-open limit."""
        throttle = get_adaptive_throttle()
        original = throttle.config.initial_limit
        throttle.current_limit = original

        # OPEN
        throttle._handle_cb_opened(_make_cb_event(EventType.CIRCUIT_BREAKER_OPENED))
        assert throttle.current_limit < original

        # CLOSE
        throttle._handle_cb_closed(_make_cb_event(EventType.CIRCUIT_BREAKER_CLOSED))
        assert throttle.current_limit == original

    @patch("baldur_pro.services.throttle.adaptive._circuit_breaker._record_audit_safe")
    def test_multi_cycle_re_open_does_not_corrupt_saved_limit(self, _mock_audit):
        """Re-OPEN within a cycle preserves the original pre-cycle limit (#494 D4).

        A single CB cycle can fire ``closed→open`` → ``half_open`` →
        ``open`` (re-trip after a failed trial) → ``half_open`` → ``closed``.
        The consumer-side guard ensures only the FIRST OPEN saves
        ``_limit_before_cb_open``; subsequent OPEN events within the same
        cycle (between the closed→open and open→closed boundaries) must
        NOT overwrite it with the in-cycle reduced value.
        """
        throttle = get_adaptive_throttle()
        original = throttle.config.initial_limit
        throttle.current_limit = original

        # First OPEN — save original
        throttle._handle_cb_opened(_make_cb_event(EventType.CIRCUIT_BREAKER_OPENED))
        assert throttle._limit_before_cb_open == original
        in_cycle_reduced = throttle.current_limit
        assert in_cycle_reduced < original

        # HALF_OPEN — limit shifts but _limit_before_cb_open stays
        throttle._handle_cb_half_opened(
            _make_cb_event(EventType.CIRCUIT_BREAKER_HALF_OPENED)
        )

        # Re-OPEN (failed trial) — guard MUST prevent overwrite
        throttle._handle_cb_opened(_make_cb_event(EventType.CIRCUIT_BREAKER_OPENED))
        assert throttle._limit_before_cb_open == original, (
            "re-OPEN overwrote _limit_before_cb_open with the in-cycle "
            "reduced value — D4 guard regression"
        )

        # CLOSE — restores to original, not to the in-cycle reduced value
        throttle._handle_cb_closed(_make_cb_event(EventType.CIRCUIT_BREAKER_CLOSED))
        assert throttle.current_limit == original
        assert throttle._limit_before_cb_open is None


class TestCBMixinSubscribeContract:
    """_subscribe_circuit_breaker_events is fail-open on ImportError."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_subscribe_does_not_raise_on_import_error(self):
        """Subscription failure (ImportError) does not propagate."""
        throttle = get_adaptive_throttle()
        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=ImportError("no event bus"),
        ):
            # Should not raise
            throttle._subscribe_circuit_breaker_events()
