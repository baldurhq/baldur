"""
Level-1 tenacity instrumentation - monkey-patch ``tenacity.Retrying.__init__``
to inject Baldur metric/audit callbacks into every Retrying instance created
afterwards.

Level differentiation (per impl 451 D7/D9):
- Level 1 (this module): observation-only — emits ``RETRY_EXHAUSTED`` events
  and records metrics. Does NOT inject ``retry_budget`` /
  ``rate_limit_coordinator``; those are explicit Level-3 (``TenacityBridgePolicy``)
  responsibilities.
- Level 3: ``TenacityBridgePolicy`` constructed by the user with full guards.
  Each Level-3 Retrying instance carries the ``__baldur_bridge_explicit__``
  marker so this patch skips it (no double emission).

Idempotency:
- ``_instrumented`` module flag + ``threading.Lock`` make the patch one-shot.
- ``tenacity.Retrying.__baldur_bridge_patched__`` class marker is the
  external observable.
- ``_reset_instrument_for_testing()`` restores the original ``__init__`` and
  clears flags so xdist parallel tests can re-run init.

Reference:
    451 - D7
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger()


__all__ = [
    "instrument_tenacity",
    "is_instrumented",
    "_reset_instrument_for_testing",
]


_BRIDGE_PATCHED_MARKER = "__baldur_bridge_patched__"
_BRIDGE_EXPLICIT_MARKER = "__baldur_bridge_explicit__"

_instrumented: bool = False
_instrument_lock: threading.Lock = threading.Lock()
_original_init: Callable[..., None] | None = None


def is_instrumented() -> bool:
    """Return True if ``instrument_tenacity()`` has patched ``Retrying``."""
    return _instrumented


def instrument_tenacity() -> bool:  # noqa: C901
    """Patch ``tenacity.Retrying.__init__`` for global observation.

    Returns:
        True if the patch was applied, False on graceful skip (tenacity not
        installed or already patched).

    Side effects:
        - Wraps ``Retrying.__init__`` so every new instance gets Baldur's
          ``before`` / ``after`` / ``before_sleep`` / ``retry_error_callback``
          chained AFTER any user-supplied callbacks.
        - Sets ``Retrying.__baldur_bridge_patched__ = True``.

    Safety:
        - Idempotent under concurrent calls (lock + class marker).
        - Skips Retrying instances marked
          ``__baldur_bridge_explicit__ = True`` (Level-3 collisions).
        - Failures during patching are logged and the function returns False
          — bootstrap must never crash on bridge init.
    """
    global _instrumented, _original_init

    from baldur.bridges.tenacity import _TENACITY_AVAILABLE

    if not _TENACITY_AVAILABLE:
        logger.debug("bridge.tenacity_unavailable")
        return False

    with _instrument_lock:
        if _instrumented:
            return False

        try:
            import tenacity
        except ImportError:
            logger.debug("bridge.tenacity_unavailable")
            return False

        if getattr(tenacity.Retrying, _BRIDGE_PATCHED_MARKER, False):
            _instrumented = True
            return False

        original = tenacity.Retrying.__init__
        _original_init = original

        def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
            # If the caller (TenacityBridgePolicy) marked this construction
            # as explicit, defer entirely to the original __init__ — the
            # explicit policy already wired its callbacks.
            explicit = kwargs.pop(_BRIDGE_EXPLICIT_MARKER, False)

            user_before = kwargs.pop("before", None)
            user_after = kwargs.pop("after", None)
            user_before_sleep = kwargs.pop("before_sleep", None)
            user_retry_error_callback = kwargs.pop("retry_error_callback", None)

            if explicit:
                if user_before is not None:
                    kwargs["before"] = user_before
                if user_after is not None:
                    kwargs["after"] = user_after
                if user_before_sleep is not None:
                    kwargs["before_sleep"] = user_before_sleep
                if user_retry_error_callback is not None:
                    kwargs["retry_error_callback"] = user_retry_error_callback
                original(self, *args, **kwargs)
                setattr(self, _BRIDGE_EXPLICIT_MARKER, True)
                return

            if user_before is not None:
                kwargs["before"] = user_before
            if user_after is not None:
                kwargs["after"] = user_after
            if user_before_sleep is not None:
                kwargs["before_sleep"] = user_before_sleep
            kwargs["retry_error_callback"] = _wrap_retry_error_callback(
                user_retry_error_callback
            )

            original(self, *args, **kwargs)

        try:
            tenacity.Retrying.__init__ = _patched_init  # type: ignore[method-assign]
            setattr(tenacity.Retrying, _BRIDGE_PATCHED_MARKER, True)
            _instrumented = True
            logger.info("bridge.tenacity_instrumented")
            return True
        except Exception as e:
            logger.warning("bridge.tenacity_instrument_failed", error=str(e))
            _original_init = None
            return False


def _reset_instrument_for_testing() -> None:
    """Restore ``Retrying.__init__`` and clear all bridge state.

    Test-only helper. Safe to call when not instrumented (no-op).
    """
    global _instrumented, _original_init

    with _instrument_lock:
        if not _instrumented:
            return
        try:
            import tenacity
        except ImportError:
            _instrumented = False
            _original_init = None
            return
        if _original_init is not None:
            tenacity.Retrying.__init__ = _original_init  # type: ignore[method-assign]
        try:
            delattr(tenacity.Retrying, _BRIDGE_PATCHED_MARKER)
        except AttributeError:
            pass
        _original_init = None
        _instrumented = False


# =============================================================================
# Level-1 callbacks — observation only (no budget / rate-limit injection)
# =============================================================================


def _wrap_retry_error_callback(
    user_callback: Callable[[Any], Any] | None,
) -> Callable[[Any], Any]:
    """Wrap (or supply) a ``retry_error_callback`` that emits ``RETRY_EXHAUSTED``.

    User callbacks run first; their return value is preserved. Baldur's
    event emission is best-effort — any failure logs at WARNING and is
    swallowed to keep tenacity behavior unchanged.
    """

    def _baldur_retry_error(retry_state: Any) -> Any:
        attempt_number = getattr(retry_state, "attempt_number", 1)
        outcome = getattr(retry_state, "outcome", None)
        last_error: BaseException | None = None
        if outcome is not None and getattr(outcome, "failed", False):
            try:
                last_error = outcome.exception()
            except Exception:
                last_error = None
        _emit_retry_exhausted(
            attempts=attempt_number,
            last_error=last_error,
        )

        if user_callback is not None:
            return user_callback(retry_state)

        if last_error is not None:
            raise last_error
        return None

    return _baldur_retry_error


def _emit_retry_exhausted(
    *,
    attempts: int,
    last_error: BaseException | None,
) -> None:
    """Emit ``RETRY_EXHAUSTED`` from Level-1 instrumentation. Best-effort."""
    try:
        from baldur.services.event_bus import get_event_bus
        from baldur.services.event_bus.bus.event_types import EventType

        event_data: dict[str, Any] = {
            "domain": "tenacity_instrument",
            "attempts": attempts,
            "final_error_type": (
                type(last_error).__name__ if last_error is not None else None
            ),
            # Bridge honors arbitrary user stop strategies -> ``stop_condition``,
            # not ``max_attempts`` (see callbacks._emit_retry_exhausted_event).
            "reason": "stop_condition",
        }
        bus = get_event_bus()
        bus.emit(
            event_type=EventType.RETRY_EXHAUSTED,
            data=event_data,
            source="tenacity_bridge",
        )
    except ImportError:
        return
    except Exception as e:
        logger.warning("bridge.tenacity_event_emission_failed", error=str(e))
