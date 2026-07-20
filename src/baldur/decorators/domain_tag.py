"""
Domain Tag Decorator and Context Manager.

Auto-tags the active domain on errors raised inside the wrapped scope so
Crisis Multiplier's domain-aware features can react per-domain.

Features:
- ``@domain_tag`` decorator: sets the domain context for the duration of the
  wrapped function call.
- ``DomainContext``: ``with``-statement-based domain context.
- ``contextvars``-backed — thread and async safe.

Usage:
    from baldur.decorators.domain_tag import (
        domain_tag,
        DomainContext,
        get_current_domain,
    )

    # Decorator form
    @domain_tag("payment")
    def process_payment():
        # Errors raised here are tagged with the "payment" domain.
        ...

    # Context-manager form
    with DomainContext("order"):
        # Errors raised here are tagged with the "order" domain.
        ...
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from contextvars import ContextVar, Token
from typing import Any, TypeVar

import structlog

from baldur.core.exceptions import DomainValidationError
from baldur.utils.domain_validation import (
    FALLBACK_DOMAIN,
    validate_and_normalize_domain,
)

logger = structlog.get_logger()


def _validate_or_fallback(domain: object, site: str) -> str:
    """Validate ``domain`` and on rejection emit observability + return fallback.

    Used by the three runtime entry points (``DomainContext.__init__``,
    ``set_domain_context``, and the Celery restore funnel) that must fail
    open: domain tagging is a side-effect, so a rejected value falls back to
    ``FALLBACK_DOMAIN`` instead of raising into the caller's request path.
    """
    try:
        return validate_and_normalize_domain(domain)
    except DomainValidationError as e:
        try:
            from baldur.metrics.event_handlers import DLQMetricEventHandler

            DLQMetricEventHandler.on_domain_rejected(
                site=site,
                reason=e.reason,
                original_domain=e.original_domain,
            )
        except Exception:
            # Observability failures must not break the fallback path.
            logger.warning(
                "domain.input_rejected",
                site=site,
                reason=getattr(e.reason, "value", e.reason),
                original_preview=str(e.original_domain)[:32],
            )
        return FALLBACK_DOMAIN


# =============================================================================
# Context Variable
# =============================================================================

_current_domain: ContextVar[str | None] = ContextVar(
    "baldur_current_domain",
    default=None,
)
"""
Domain of the current execution context.

Backed by ``contextvars`` so it is thread- and async-safe.
"""


# =============================================================================
# Domain Context Manager
# =============================================================================


class DomainContext:
    """
    Domain context manager.

    Sets the domain for the enclosed code block via ``with``. On block exit,
    the previous domain context is restored automatically.

    Usage:
        with DomainContext("payment"):
            # The domain is "payment" inside this block.
            process_payment()
        # The previous domain is restored on exit.

    Attributes:
        domain: Domain name to apply for the block.
    """

    def __init__(self, domain: str):
        """
        Initialize a DomainContext.

        Args:
            domain: Domain name to apply.
        """
        self.domain = _validate_or_fallback(domain, site="domain_context")
        self._token: Token | None = None
        self._previous_domain: str | None = None

    def __enter__(self) -> DomainContext:
        """Context enter: set the domain."""
        self._previous_domain = _current_domain.get()
        self._token = _current_domain.set(self.domain)

        logger.debug(
            "domain_context.entered",
            domain=self.domain,
            previous_domain=self._previous_domain,
        )

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context exit: restore the previous domain."""
        if self._token is not None:
            _current_domain.reset(self._token)
            self._token = None

        logger.debug(
            "domain_context.exited",
            domain=self.domain,
            current_domain=_current_domain.get(),
        )

        # Propagate exceptions (return None == False for __exit__).


# =============================================================================
# Domain Tag Decorator
# =============================================================================

F = TypeVar("F", bound=Callable[..., Any])


def domain_tag(domain: str) -> Callable[[F], F]:
    """
    Domain-tagging decorator.

    Sets the domain context while the wrapped function executes and restores
    the previous context on exit (both sync and async callables are
    supported).

    Args:
        domain: Domain name to apply for the duration of the call.

    Returns:
        Decorator that wraps the target callable.

    Usage:
        @domain_tag("payment")
        def process_payment():
            # Errors raised here are tagged with the "payment" domain.
            ...

        @domain_tag("order")
        async def create_order():
            # Async functions are supported.
            ...
    """
    # 545 D4: decoration-time validation raises loud so dev/CI catches bad
    # literals (e.g., ``@domain_tag("invalid name!")``) at module import.
    normalized_domain = validate_and_normalize_domain(domain)

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            with DomainContext(normalized_domain):
                return func(*args, **kwargs)

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            with DomainContext(normalized_domain):
                return await func(*args, **kwargs)

        # Detect async functions
        import asyncio

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper  # type: ignore

    return decorator


# =============================================================================
# Utility Functions
# =============================================================================


def get_current_domain() -> str | None:
    """
    Return the current domain from the active context.

    Returns:
        Current domain name, or None if no context is set.

    Usage:
        @domain_tag("payment")
        def process_payment():
            domain = get_current_domain()
            print(f"Current domain: {domain}")  # "payment"
    """
    return _current_domain.get()


def clear_domain_context() -> None:
    """
    Clear the current domain context.

    Primarily for test cleanup. In application code, ``DomainContext`` and
    ``@domain_tag`` clean up automatically on scope exit.
    """
    _current_domain.set(None)


def set_domain_context(domain: str | None) -> Token:
    """
    Set the domain context (low-level API).

    Primarily intended for framework integrations. Prefer ``DomainContext``
    or ``@domain_tag`` in application code.

    Args:
        domain: Domain to apply (None clears the context).

    Returns:
        Restoration token. Pass it to ``_current_domain.reset(token)`` to
        roll back to the previous value.

    Usage:
        token = set_domain_context("payment")
        try:
            # Domain is "payment" in this block.
            ...
        finally:
            _current_domain.reset(token)
    """
    if domain is None:
        return _current_domain.set(None)
    validated = _validate_or_fallback(domain, site="set_domain_context")
    return _current_domain.set(validated)


# =============================================================================
# Django/Flask Middleware Integration Helper
# =============================================================================


class DomainMiddlewareMixin:
    """
    Mixin for web-framework middlewares.

    Extracts the domain from the request URL or headers and applies it as the
    active context for the downstream view/handler.

    Usage (Django):
        class DomainMiddleware(DomainMiddlewareMixin):
            def __init__(self, get_response):
                self.get_response = get_response

            def __call__(self, request):
                domain = self.extract_domain_from_request(request)
                with DomainContext(domain or "unknown"):
                    return self.get_response(request)
    """

    DOMAIN_HEADER = "X-Domain"
    """Domain identification header."""

    URL_DOMAIN_MAPPING: dict[str, str] = {}
    """
    URL pattern → domain mapping (per-project configuration).

    Override in a subclass, or use the Django setting
    ``BALDUR_DOMAIN_MAPPING``.
    Example: ``{"/api/payments/": "payment", "/api/orders/": "order"}``.
    """

    def extract_domain_from_request(self, request) -> str | None:
        """
        Extract the domain from a request.

        Resolution order:
        1. ``X-Domain`` header
        2. URL pattern match
        3. ``None``
        """
        # Header lookup
        if hasattr(request, "headers"):
            domain = request.headers.get(self.DOMAIN_HEADER)
            if domain:
                return domain.lower()
        elif hasattr(request, "META"):
            # Django style
            domain = request.META.get(
                f"HTTP_{self.DOMAIN_HEADER.replace('-', '_').upper()}"
            )
            if domain:
                return domain.lower()

        # URL pattern lookup
        path = getattr(request, "path", "")
        for pattern, domain in self.URL_DOMAIN_MAPPING.items():
            if path.startswith(pattern):
                return domain

        return None
