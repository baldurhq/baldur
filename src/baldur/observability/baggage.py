"""
OTel Baggage cross-service propagation.

Sets up the W3C TraceContext+Baggage composite propagator and the
bidirectional ContextVar <-> OTel Baggage sync for the ``baldur.*`` keys
(``cell_id`` / ``domain``).

The inject/restore flow is live once the framework startup path wires the
instrumentors (``baldur.bootstrap._instrument_otel_if_enabled`` for the
outbound ``requests`` library, ``BaldurConfig.ready`` for inbound Django):

- **Inbound** — ``DjangoInstrumentor`` extracts the W3C ``baggage`` header
  into the OTel context at request start, and ``restore_contextvars_from_baggage``
  copies the ``baldur.*`` keys back into the local ContextVars so cell/domain
  tagging sees the upstream values instead of re-hashing locally.
- **Outbound** — ``sync_contextvars_to_baggage`` snapshots the current
  ContextVars onto the OTel context at request start, and
  ``RequestsInstrumentor.inject()`` then writes ``traceparent`` + ``baggage``
  headers onto every outgoing HTTP request, carrying ``cell_id`` / ``domain``
  to the downstream service.
"""

from __future__ import annotations

import importlib
from functools import cache
from typing import Any

import structlog

logger = structlog.get_logger()

# Baggage key prefix — the baldur namespace
BAGGAGE_PREFIX = "baldur"

# ContextVar mapping — per Baggage key, the getter (read) and contextvar
# (write) paths. Kept as a single source to prevent sync/restore asymmetry.
# Imported lazily to avoid circular dependencies.
_CONTEXTVAR_BAGGAGE_MAP: dict[str, dict[str, str]] = {
    "cell_id": {
        "getter": "baldur.context.cell_context:get_current_cell_id",
        "contextvar": "baldur.context.cell_context:_current_cell_id",
    },
    "domain": {
        "getter": "baldur.decorators.domain_tag:get_current_domain",
        "contextvar": "baldur.decorators.domain_tag:_current_domain",
    },
}


def setup_baggage_propagation() -> None:
    """
    Register the W3C TraceContext + Baggage CompositePropagator.

    After this function is called, the traceparent and baggage headers are
    propagated together whenever RequestsInstrumentor runs inject().

    Call site: after initialize_opentelemetry() succeeds.
    """
    try:
        from opentelemetry import propagate
        from opentelemetry.baggage.propagation import W3CBaggagePropagator
        from opentelemetry.propagators.composite import CompositePropagator
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        propagate.set_global_textmap(
            CompositePropagator(
                [
                    TraceContextTextMapPropagator(),
                    W3CBaggagePropagator(),
                ]
            )
        )
        logger.info("otel.baggage_propagation_enabled")
    except ImportError:
        logger.debug("otel.propagation_packages_installed")
    except Exception as e:
        logger.warning(
            "baggage.propagation_setup_failed",
            error=e,
        )


@cache
def _resolve_import(path: str) -> Any:
    """
    Dynamically import and cache the attribute named by a
    'module.path:attribute_name' string.

    The lazy import runs only on the first call, avoiding circular
    dependencies. Later calls return immediately from the cache.
    """
    module_path, attr_name = path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr_name)


def sync_contextvars_to_baggage() -> object | None:
    """Sync the current ContextVar values into the OTel Baggage.

    Called by Django middleware (or any user-side outbound HTTP hook that
    wants Baldur context propagated). ContextVar values that are ``None``
    are not added to Baggage.

    Returns:
        An OTel context token that MUST be released via
        ``context.detach(token)``. Returns ``None`` when OTel is not
        installed.
    """
    try:
        from opentelemetry import baggage, context

        ctx = context.get_current()

        for key, entry in _CONTEXTVAR_BAGGAGE_MAP.items():
            try:
                getter = _resolve_import(entry["getter"])
                value = getter()
                if value is not None:
                    ctx = baggage.set_baggage(
                        f"{BAGGAGE_PREFIX}.{key}", str(value), context=ctx
                    )
            except Exception:
                # One failing ContextVar must not abort the whole sync
                logger.debug("baggage.contextvar_sync_failed", key=key, exc_info=True)

        return context.attach(ctx)
    except ImportError:
        # OTel not installed — return a no-op token
        return None


def detach_baggage_token(token: object) -> None:
    """
    Safely release the token returned by sync_contextvars_to_baggage().

    Works without error even where OTel is not installed (token=None).
    """
    if token is None:
        return
    try:
        from opentelemetry import context

        # token is `object` at the OSS API boundary because OTel may be
        # absent; cast to the OTel `Token[Context]` at the call site.
        context.detach(token)  # type: ignore[arg-type]
    except Exception:
        logger.debug("baggage.detach_failed", exc_info=True)


def restore_contextvars_from_baggage() -> None:
    """
    Restore ContextVar values from the received OTel Baggage.

    Uses the contextvar paths in _CONTEXTVAR_BAGGAGE_MAP, so it reads and
    writes through the same mapping as sync_contextvars_to_baggage().

    Must be called after DjangoInstrumentor has loaded the baggage HTTP header
    into the OTel context; otherwise there are no valid values to read.

    Called from the Django BaggageSyncMiddleware or from Celery task_prerun.
    """
    try:
        from opentelemetry import baggage
    except ImportError:
        return

    for key, entry in _CONTEXTVAR_BAGGAGE_MAP.items():
        value = baggage.get_baggage(f"{BAGGAGE_PREFIX}.{key}")
        if value:
            try:
                contextvar = _resolve_import(entry["contextvar"])
                contextvar.set(value)
            except Exception:
                logger.debug(
                    "baggage.contextvar_restore_failed", key=key, exc_info=True
                )
