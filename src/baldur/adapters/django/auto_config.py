"""
Django Auto-Configuration for Baldur.

Provides configure_baldur() — an explicit wrapper that consumers call
at the bottom of their settings.py to inject baldur middleware groups,
set up DRF EXCEPTION_HANDLER, and optionally initialize OTEL.

Usage:
    # settings.py (last line)
    from baldur.adapters.django import configure_baldur
    configure_baldur(namespace=globals())
"""

from __future__ import annotations

import logging

from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("baldur")

# =========================================================================
# Middleware Group Defaults
# =========================================================================

DEFAULT_EARLY_GROUP = [
    "baldur.audit.trace.trace_id_middleware",
    "baldur.api.django.middleware.HealthBridgeMiddleware",
    # Drain-aware + request-tracking (471) sit immediately after the bridge
    # so bridge endpoints emit their own ``status: draining`` payload before
    # DrainAware short-circuits, and so 503-rejected requests are not tracked.
    "baldur.api.django.middleware.DrainAwareMiddleware",
    "baldur.api.django.middleware.RequestTrackingMiddleware",
    "baldur.api.django.tiering.TieringMiddleware",
    "baldur.api.django.middleware.IPBanMiddleware",
    "baldur.api.django.middleware.BaldurMiddleware",
    "baldur.api.django.middleware.actor_context.ActorContextMiddleware",
]

DEFAULT_POST_AUTH_GROUP = [
    "baldur.api.django.cell.middleware.CellTaggingMiddleware",
    "baldur.api.django.cell.middleware.BaggageSyncMiddleware",
    "baldur.api.django.rate_limit.HybridRateLimitMiddleware",
    "baldur.api.django.pool_circuit_breaker.PoolCircuitBreakerMiddleware",
]

DEFAULT_TAIL_GROUP = [
    "baldur.api.django.audit_middleware.AuditMiddleware",
]

MIDDLEWARE_TOGGLES: dict[str, str] = {
    "baldur.api.django.tiering.TieringMiddleware": "BALDUR_TIERING_MIDDLEWARE_ENABLED",
    "baldur.api.django.middleware.actor_context.ActorContextMiddleware": "BALDUR_ACTOR_MIDDLEWARE_ENABLED",
    "baldur.api.django.cell.middleware.CellTaggingMiddleware": "BALDUR_CELL_TAGGING_ENABLED",
    "baldur.api.django.pool_circuit_breaker.PoolCircuitBreakerMiddleware": "BALDUR_POOL_CB_MIDDLEWARE_ENABLED",
    "baldur.api.django.audit_middleware.AuditMiddleware": "BALDUR_AUDIT_MIDDLEWARE_ENABLED",
    "baldur.api.django.middleware.DrainAwareMiddleware": "BALDUR_DRAIN_AWARE_MIDDLEWARE_ENABLED",
    "baldur.api.django.middleware.RequestTrackingMiddleware": "BALDUR_REQUEST_TRACKING_MIDDLEWARE_ENABLED",
}


# =========================================================================
# Public API
# =========================================================================


def configure_baldur(
    namespace: dict,
    *,
    early_group: list[str] | None = None,
    post_auth_group: list[str] | None = None,
    tail_group: list[str] | None = None,
    domains: list[str] | None = None,
    disable_auto_otel: bool = False,
) -> None:
    """Called from consumer settings.py to wrap baldur configuration explicitly.

    Args:
        namespace: globals() of the consumer settings module — modifies MIDDLEWARE, REST_FRAMEWORK, etc.
        early_group: List of middleware to insert before Django core (default: DEFAULT_EARLY_GROUP)
        post_auth_group: List of middleware to insert after AuthenticationMiddleware (default: DEFAULT_POST_AUTH_GROUP)
        tail_group: List of middleware to insert at the tail (default: DEFAULT_TAIL_GROUP)
        domains: BALDUR_CORE_DOMAINS setting (list of business domains)
        disable_auto_otel: If True, skip OTEL-related setup (used when deferring to a Gunicorn hook)

    Note:
        This function MUST be called at the **very bottom** of settings.py.
        It must run after MIDDLEWARE, REST_FRAMEWORK, etc. are defined to work correctly.
    """
    from baldur.settings.auto_config import get_auto_config_settings

    auto_settings = get_auto_config_settings()

    _validate_prerequisites(namespace)

    if auto_settings.middleware:
        _inject_middleware_groups(
            namespace,
            early=early_group if early_group is not None else list(DEFAULT_EARLY_GROUP),
            post_auth=post_auth_group
            if post_auth_group is not None
            else list(DEFAULT_POST_AUTH_GROUP),
            tail=tail_group if tail_group is not None else list(DEFAULT_TAIL_GROUP),
        )

    if auto_settings.exception_handler:
        _setup_exception_handler(namespace)

    if domains is not None:
        namespace["BALDUR_CORE_DOMAINS"] = domains

    # Global structlog configuration (idempotent — safe to call repeatedly)
    # Design 323: after removing __init__.py side-effects, initialize via this wrapper
    from baldur.observability.structlog_config import configure_structlog

    configure_structlog()

    from baldur.settings.observability import get_observability_settings

    otel_enabled = (
        get_observability_settings().effective_otel_enabled and not disable_auto_otel
    )
    if otel_enabled and not _is_gunicorn_master():
        _initialize_otel(namespace)

    logger.debug("baldur.configure_baldur_applied")


# =========================================================================
# Middleware Injection
# =========================================================================


def _inject_middleware_groups(
    namespace: dict,
    early: list[str],
    post_auth: list[str],
    tail: list[str],
) -> None:
    """Insert baldur middleware groups into the MIDDLEWARE list."""
    middleware = list(namespace.get("MIDDLEWARE", []))

    early = _filter_by_toggles(early, namespace)
    post_auth = _filter_by_toggles(post_auth, namespace)
    tail = _filter_by_toggles(tail, namespace)

    # Skip entries that already exist (Consumer added them manually)
    early = [m for m in early if m not in middleware]
    post_auth = [m for m in post_auth if m not in middleware]
    tail = [m for m in tail if m not in middleware]

    # early -> right after PrometheusBeforeMiddleware (front if missing)
    early_idx = _find_insert_point(
        middleware, "PrometheusBeforeMiddleware", after=True, fallback=0
    )
    for i, m in enumerate(early):
        middleware.insert(early_idx + i, m)

    # post_auth -> right after AuthenticationMiddleware, skipping past XFrameOptionsMiddleware
    auth_idx = _find_insert_point(
        middleware, "AuthenticationMiddleware", after=True, fallback=len(middleware)
    )
    xframe_idx = _find_insert_point(
        middleware, "XFrameOptionsMiddleware", after=True, fallback=auth_idx
    )
    insert_idx = max(auth_idx, xframe_idx)
    for i, m in enumerate(post_auth):
        middleware.insert(insert_idx + i, m)

    # tail -> right before PrometheusAfterMiddleware (tail if missing)
    tail_idx = _find_insert_point(
        middleware, "PrometheusAfterMiddleware", after=False, fallback=len(middleware)
    )
    for i, m in enumerate(tail):
        middleware.insert(tail_idx + i, m)

    namespace["MIDDLEWARE"] = middleware


def _filter_by_toggles(group: list[str], namespace: dict) -> list[str]:
    """Remove middleware whose toggle setting is False."""
    result = []
    for m in group:
        toggle = MIDDLEWARE_TOGGLES.get(m)
        if toggle and not namespace.get(toggle, True):
            continue
        result.append(m)
    return result


def _find_insert_point(
    middleware: list[str], target_substr: str, *, after: bool, fallback: int
) -> int:
    """Find target in the middleware list and return the insertion index."""
    for i, m in enumerate(middleware):
        if target_substr in m:
            return (i + 1) if after else i
    return fallback


# =========================================================================
# Exception Handler
# =========================================================================


def _setup_exception_handler(namespace: dict) -> None:
    """Auto-configure the DRF EXCEPTION_HANDLER."""
    rest_settings = namespace.get("REST_FRAMEWORK", {})
    if "EXCEPTION_HANDLER" not in rest_settings:
        rest_settings["EXCEPTION_HANDLER"] = (
            "baldur.api.django.exceptions.handler.baldur_exception_handler"
        )
    namespace["REST_FRAMEWORK"] = rest_settings


# =========================================================================
# Validation
# =========================================================================


def _validate_prerequisites(namespace: dict) -> None:
    """Validate that required settings are defined before the wrapper is called."""
    if "MIDDLEWARE" not in namespace:
        raise ImproperlyConfigured(
            "configure_baldur() must be called after MIDDLEWARE is defined. "
            "Place it at the very bottom of settings.py."
        )
    if "INSTALLED_APPS" not in namespace:
        raise ImproperlyConfigured(
            "configure_baldur() must be called after INSTALLED_APPS is defined."
        )


# =========================================================================
# OTEL Initialization (dev-server only)
# =========================================================================


def _is_gunicorn_master() -> bool:
    """Return True if the current process is the Gunicorn Master/Arbiter.

    Thin wrapper around ``baldur.core.process_utils.is_gunicorn_master()``
    kept module-local for the OTEL initialization gate below. See the
    helper docstring for the env-var-late race caveat.
    """
    from baldur.core.process_utils import is_gunicorn_master

    return is_gunicorn_master()


def _initialize_otel(namespace: dict) -> None:
    """Initialize OTEL in the development-server environment."""
    try:
        from baldur.observability import initialize_opentelemetry

        result = initialize_opentelemetry()
        namespace["_otel_initialized"] = result
    except ImportError:
        namespace["_otel_initialized"] = False
    except Exception:
        logger.warning("baldur.otel_initialization_failed", exc_info=True)
        namespace["_otel_initialized"] = False
