"""Unit tests for ``baldur.bridges.tenacity.instrument`` (impl 451).

Scope:
- ``instrument_tenacity()`` — idempotent patch, class marker.
- User callback pass-through — ``before`` / ``after`` / ``before_sleep`` are not
  chained (Level-1 emits exclusively via ``retry_error_callback``).
- ``_reset_instrument_for_testing()`` — round-trip restoration of __init__.
- D5↔D7 interplay — Level-1 patch skips Level-3 explicit-marker instances.
- Graceful skip when tenacity is unavailable.
"""

from __future__ import annotations

import tenacity

from baldur.bridges.tenacity.instrument import (
    _BRIDGE_PATCHED_MARKER,
    _reset_instrument_for_testing,
    instrument_tenacity,
    is_instrumented,
)

# =============================================================================
# Contract — idempotency
# =============================================================================


class TestInstrumentTenacityIdempotencyContract:
    """``instrument_tenacity()`` is one-shot — second call returns False."""

    def test_first_call_returns_true_and_sets_class_marker(self):
        """Patch applies on first call; class marker observable externally."""
        # Given a fresh state
        assert is_instrumented() is False

        # When the first patch is applied
        applied = instrument_tenacity()

        # Then
        assert applied is True
        assert is_instrumented() is True
        assert getattr(tenacity.Retrying, _BRIDGE_PATCHED_MARKER, False) is True

    def test_second_call_returns_false_and_keeps_state(self):
        """Idempotent: a second invocation is a no-op."""
        instrument_tenacity()
        second = instrument_tenacity()

        assert second is False
        assert is_instrumented() is True

    def test_marker_attribute_name_contract(self):
        """Marker name is the published constant per impl 451 D7."""
        assert _BRIDGE_PATCHED_MARKER == "__baldur_bridge_patched__"


# =============================================================================
# Behavior — user callback pass-through (Level-1 does not chain before/after/before_sleep)
# =============================================================================


class TestInstrumentTenacityCallbackPassthroughBehavior:
    """``before`` / ``after`` / ``before_sleep`` are attached verbatim — Level-1
    emits exclusively via ``retry_error_callback``, so it does not need to wrap
    the per-attempt hooks.
    """

    def test_user_before_attached_verbatim_and_runs(self):
        """User ``before`` callable runs unchanged on each attempt."""
        instrument_tenacity()

        observed: list[str] = []

        def _user_before(_state):
            observed.append("user_before")

        retrying = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(1),
            before=_user_before,
        )

        retrying(lambda: "x")
        assert observed == ["user_before"]
        # Verbatim attachment — Baldur is not wrapping the user callback.
        assert retrying.before is _user_before

    def test_no_user_callback_means_no_baldur_chaining_for_before(self):
        """Without a user ``before``, the patched __init__ leaves ``before``
        absent from kwargs so tenacity's default is used unchanged."""
        instrument_tenacity()

        retrying = tenacity.Retrying(stop=tenacity.stop_after_attempt(1))
        retrying(lambda: "x")

        # Tenacity's default is ``tenacity.before.before_nothing`` — a real
        # callable, not None. The point is that Baldur did not inject a custom
        # wrapper here.
        assert retrying.before is tenacity.before.before_nothing

    def test_retry_error_callback_is_baldur_wrapped(self):
        """``retry_error_callback`` is the one hook Baldur DOES wrap (Level-1
        emits ``RETRY_EXHAUSTED`` from this hook). The wrapper replaces any
        user-supplied callback rather than passing it through verbatim."""
        instrument_tenacity()

        def _user_callback(_state):
            return "user-fallback"

        retrying = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(1),
            retry_error_callback=_user_callback,
        )

        # Wrapped — the attached object is NOT the user callback itself.
        assert retrying.retry_error_callback is not _user_callback
        assert retrying.retry_error_callback is not None


# =============================================================================
# Behavior — D5↔D7 interplay (skip explicit-marker construction)
# =============================================================================


class TestInstrumentTenacityExplicitInteractionBehavior:
    """Patched ``__init__`` skips chaining when caller passes the explicit marker."""

    def test_explicit_marker_kwarg_is_consumed_and_skips_chaining(self):
        """Explicit-marker construction does NOT chain Baldur callbacks."""
        instrument_tenacity()

        observed: list[str] = []

        def _user_before(_state):
            observed.append("user_before")

        retrying = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(1),
            before=_user_before,
            __baldur_bridge_explicit__=True,  # type: ignore[call-arg]
        )

        # Marker is consumed by the patched __init__ and re-stamped on the instance.
        assert getattr(retrying, "__baldur_bridge_explicit__", False) is True
        # User callback is attached as-is (no chaining).
        assert retrying.before is _user_before

    def test_explicit_emit_count_remains_one_when_both_levels_active(self, monkeypatch):
        """Single RETRY_EXHAUSTED event when explicit Policy runs under instrument."""
        emitted: list[dict] = []

        class _StubBus:
            def emit(self, *, event_type, data, source):
                emitted.append({"source": source, "data": data})

        monkeypatch.setattr(
            "baldur.services.event_bus.get_event_bus", lambda: _StubBus()
        )

        instrument_tenacity()

        # Use TenacityBridgePolicy (Level 3) on top of the patched module.
        from baldur.bridges.tenacity.policy import TenacityBridgePolicy

        def _always_fail():
            raise RuntimeError("boom")

        policy: TenacityBridgePolicy[None] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(2),
            wait=tenacity.wait_fixed(0),
            domain="combo",
        )
        policy.execute(_always_fail)

        # Only the bridge's own retry_error_callback should emit; instrument
        # path is bypassed for explicit instances.
        assert len(emitted) == 1
        assert emitted[0]["data"]["domain"] == "combo"


# =============================================================================
# Contract — graceful skip when tenacity unavailable
# =============================================================================


class TestInstrumentTenacityUnavailableContract:
    """When ``_TENACITY_AVAILABLE`` is False, returns False without raising."""

    def test_returns_false_when_extra_missing(self, monkeypatch):
        """Graceful skip — bootstrap must never crash."""
        monkeypatch.setattr("baldur.bridges.tenacity._TENACITY_AVAILABLE", False)
        # Reset since fixture ran before monkeypatch took effect.
        _reset_instrument_for_testing()
        applied = instrument_tenacity()

        assert applied is False
        assert is_instrumented() is False


# =============================================================================
# Contract — _reset_instrument_for_testing round-trip
# =============================================================================


class TestInstrumentResetContract:
    """``_reset_instrument_for_testing()`` restores the original __init__."""

    def test_reset_restores_original_init_identity(self):
        """After reset, Retrying.__init__ is the original object."""
        original_init = tenacity.Retrying.__init__

        instrument_tenacity()
        assert tenacity.Retrying.__init__ is not original_init

        _reset_instrument_for_testing()
        assert tenacity.Retrying.__init__ is original_init

    def test_reset_clears_class_marker(self):
        """Class marker is removed after reset."""
        instrument_tenacity()
        assert hasattr(tenacity.Retrying, _BRIDGE_PATCHED_MARKER)

        _reset_instrument_for_testing()
        assert not hasattr(tenacity.Retrying, _BRIDGE_PATCHED_MARKER)

    def test_reset_when_not_instrumented_is_noop(self):
        """Calling reset twice (or on fresh state) does not raise."""
        _reset_instrument_for_testing()
        # Second call must also be a no-op.
        _reset_instrument_for_testing()
        assert is_instrumented() is False
