"""EventBus Settings — backend selection and Redis configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = [
    "EventBusSettings",
    "get_event_bus_settings",
    "reset_event_bus_settings",
]


class EventBusSettings(BaseSettings):
    """EventBus backend configuration.

    Env vars:
        BALDUR_EVENT_BUS_BACKEND: "memory" (L1 in-process) or "redis" (L2 cross-pod)
        BALDUR_EVENT_BUS_REDIS_URL: Dedicated Redis URL for Pub/Sub
            (falls back to BALDUR_REDIS_URL when None)
        BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS: Per-handler wall-clock bound.
        BALDUR_EVENT_BUS_DISPATCH_MODE: ``async_pool`` (default — shared
            ThreadPoolExecutor with timeout enforcement), ``thread_per_emit``
            (legacy per-call ``Thread.start()`` path), or ``sync`` (inline on
            caller thread; bypasses timeout — test/CLI only).
        BALDUR_EVENT_BUS_DISPATCH_WORKERS: ``async_pool`` mode worker ceiling.
            Threads are lazy-spawned, so resident count is bounded by active
            concurrent submits, not by the ceiling. Read once on first
            ``BaldurEventBus._get_executor()`` call — change requires
            ``reset_event_bus_settings()`` to drain the executor before the
            new value takes effect (the cascade is wired automatically).
    """

    model_config = make_settings_config("BALDUR_EVENT_BUS_")

    backend: Literal["memory", "redis"] = Field(
        default="memory",
        description="EventBus backend: memory (L1 in-process only), redis (L2 cross-pod)",
    )
    redis_url: str | None = Field(
        default=None,
        description="Dedicated Redis URL for Pub/Sub. "
        "Falls back to BALDUR_REDIS_URL when None.",
    )
    handler_timeout_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=60.0,
        description="Max seconds per handler. 0 disables timeout guard.",
    )
    dispatch_mode: Literal["sync", "thread_per_emit", "async_pool"] = Field(
        default="async_pool",
        description=(
            "Handler dispatch mechanism. 'async_pool' (default) — shared "
            "ThreadPoolExecutor singleton with future.result(timeout=); "
            "'thread_per_emit' — legacy per-call threading.Thread (escape "
            "hatch, retains current Thread.start() cost); 'sync' — inline on "
            "the caller thread, bypasses handler_timeout_seconds. Use 'sync' "
            "only in tests/CLI where every microsecond counts and runaway "
            "handlers are not a concern."
        ),
    )
    dispatch_workers: int = Field(
        default=32,
        ge=1,
        le=64,
        description=(
            "Worker ceiling for the 'async_pool' dispatch executor. Threads "
            "are lazy-spawned, so resident count is bounded by active "
            "concurrent submits. Default 32 matches "
            "BALDUR_PROTECT_DEFAULT_TIMEOUT_EXECUTOR_WORKERS for a single "
            "shared-executor mental model. Read once on first executor "
            "construction; change requires reset_event_bus_settings() to "
            "drain the pool."
        ),
    )


def get_event_bus_settings() -> EventBusSettings:
    """Return EventBusSettings singleton via ServicesGroup."""
    from baldur.settings.root import get_config

    return get_config().services_group.event_bus


def reset_event_bus_settings() -> None:
    """Reset EventBusSettings cached_property (for testing).

    Mirrors the ``reset_protect_settings()`` → ``reset_protect_caches()``
    cascade pattern — invalidating the settings cache also drains the
    ``BaldurEventBus`` dispatch executor so a subsequent
    ``get_event_bus_settings().dispatch_workers`` change is observable on
    the next ``_get_executor()`` call. Importing the bus module is lazy so
    this helper stays usable from ``settings/`` without creating a circular
    import.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["event_bus"]
    except KeyError:
        pass

    try:
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        BaldurEventBus.shutdown_dispatch_executor()
    except Exception:
        pass
