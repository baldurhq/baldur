"""``@dlq_protect`` — the capture preset of ``@protected``.

Thin wrapper around ``baldur.protect_facade.protected()`` that pins
``dlq=True``, ``retry=True``, ``circuit_breaker=True``: every call whose
failure reaches the caller is parked in the dead letter queue under the
decorator's name, except the exclusions the DLQ guide lists (a fallback
answered it, a guard refused it, observe-only mode, or capture switched off).
Other kwargs (``fallback``, ``timeout``, ``context``) pass through unchanged.
Capture works on a plain OSS install; PRO overlays the operate-at-scale
surface on the same queue.
"""

# Reference: 458 §D4.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeVar

from baldur.interfaces.resilience_policy import PolicyContext
from baldur.protect_facade import protected

__all__ = ["dlq_protect"]

T = TypeVar("T")

_TIMEOUT_UNSET = object()
_CONTEXT_FROM_UNSET = object()


def dlq_protect(
    name: str,
    *,
    fallback: Callable[[], Any] | Callable[[], Awaitable[Any]] | None = None,
    timeout: float | None = _TIMEOUT_UNSET,  # type: ignore[assignment]
    context_from: Callable[..., PolicyContext]
    | None
    | Literal[False] = _CONTEXT_FROM_UNSET,  # type: ignore[assignment]
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """``@protected`` with DLQ capture, retry and the circuit breaker pinned on.

    Equivalent to ``@protected(name, dlq=True, retry=True, circuit_breaker=True)``
    but states the intent at the decoration site. Every call whose failure
    reaches the caller — retries exhausted, retry switched off, a timeout, or
    an open breaker's rejection — is parked under ``name``; the DLQ guide lists
    the exclusions.

    Args:
        name: Identifier used for metrics/logging (passed to ``protect()``).
        fallback: Optional fallback callable invoked when all branches fail.
        timeout: Per-call timeout in seconds. Omit to use ``ProtectSettings``
            defaults; pass ``None`` to disable.
        context_from: Forwarded to ``@protected``. Defaults (when omitted) to
            ``None`` → auto-extract: ``order_id`` / ``user_id`` and the full
            primitive payload from the wrapped function's arguments populate
            the DLQ entry without manual wiring. Pass ``False`` at
            privacy-sensitive callsites (e.g.,
            ``@dlq_protect("auth.verify_password", context_from=False)``) to
            skip capture. Pass a ``Callable[..., PolicyContext]`` for custom
            extraction logic.

    Returns:
        Decorator that auto-detects sync vs async and dispatches accordingly.

    Usage::

        @dlq_protect("orders.charge")
        def charge(order_id: int) -> None:
            ...

        @dlq_protect("orders.charge_async")
        async def charge_async(order_id: int) -> None:
            ...
    """
    kwargs: dict[str, Any] = {
        "dlq": True,
        "retry": True,
        "circuit_breaker": True,
        "fallback": fallback,
    }
    if timeout is not _TIMEOUT_UNSET:
        kwargs["timeout"] = timeout
    if context_from is not _CONTEXT_FROM_UNSET:
        kwargs["context_from"] = context_from

    return protected(name, **kwargs)
