"""
Bulkhead Decorator - sync/async auto-dispatching decorator.

Internally uses BulkheadPolicy/AsyncBulkheadPolicy to
perform bulkhead control based on PolicyResult.

The fallback parameter is deprecated and has been migrated to the
FallbackPolicy/AsyncFallbackPolicy + PolicyComposer/AsyncPolicyComposer
combination. Fallback is activated only on BulkheadFullError, and
business exceptions are re-propagated without fallback.

Usage:
    @bulkhead(ConnectionType.DATABASE)
    def db_operation():
        pass

    @bulkhead(ConnectionType.DATABASE)
    async def async_db_operation():
        pass

    # Custom domains must be provisioned before the first decorated call:
    from baldur.services.bulkhead import get_bulkhead_registry

    get_bulkhead_registry().get_or_create("custom_domain", max_concurrent=10)

    @bulkhead("custom_domain", timeout=5.0)
    def custom_operation():
        pass
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import structlog

from baldur.core.connection_health import ConnectionType

logger = structlog.get_logger()

T = TypeVar("T")

__all__ = ["bulkhead", "bulkhead_for_cache", "bulkhead_for_database"]


def _bulkhead_full_predicate(result: Any) -> bool:
    """Activate fallback only on BulkheadFullError.

    Same as the exception filtering of the existing @bulkhead decorator family,
    fallback is applied only to BulkheadFullError.
    Business exceptions are re-propagated without fallback.
    """
    from baldur.services.bulkhead.exceptions import BulkheadFullError

    return isinstance(result.error, BulkheadFullError)


def bulkhead(  # noqa: C901
    name: str | ConnectionType,
    timeout: float | None = None,
    fallback: Callable[..., T] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Bulkhead decorator (sync/async auto-dispatching).

    Detects the function type via asyncio.iscoroutinefunction() and
    automatically applies the appropriate bulkhead (sync/async).

    Custom domains must be provisioned (via ``get_or_create()``, ``register()``,
    or a policy/settings helper) before the decorated function is first called.
    An unregistered domain raises ``BulkheadNotFoundError`` — naming the registered
    compartments — on both sync and async callees; the error is raised before any
    fallback is considered, so ``fallback`` does not mask it. The four built-in
    compartments (``database``, ``cache``, ``external_api``, ``message_queue``) are
    always available.

    Args:
        name: Domain name or ConnectionType
        timeout: Resource acquisition wait timeout (seconds). If None, fail immediately.
        fallback: Alternative function to call when the bulkhead is full

    Returns:
        Decorator with the bulkhead applied

    Examples:
        # Synchronous function (built-in compartment)
        @bulkhead(ConnectionType.DATABASE)
        def db_query():
            return execute_query()

        # Asynchronous function (built-in compartment)
        @bulkhead(ConnectionType.DATABASE)
        async def async_db_query():
            return await execute_async_query()

        # Timeout setting (built-in compartment)
        @bulkhead("external_api", timeout=5.0)
        def call_external_api():
            return requests.get(url)

        # Custom domain with fallback — provision it first, then decorate
        from baldur.services.bulkhead import get_bulkhead_registry

        get_bulkhead_registry().get_or_create("reports", max_concurrent=5)

        @bulkhead("reports", fallback=lambda: {"status": "unavailable"})
        def get_data():
            return fetch_data()
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:  # noqa: C901
        if asyncio.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> T:
                from baldur.services.bulkhead.policy import (
                    AsyncBulkheadPolicy,
                )
                from baldur.services.bulkhead.registry import (
                    get_bulkhead_registry,
                )

                registry = get_bulkhead_registry()
                key = name.value if isinstance(name, ConnectionType) else name
                async_bh = registry.get_async(key)
                bp = AsyncBulkheadPolicy(async_bulkhead=async_bh, timeout=timeout)

                if fallback is not None:
                    from baldur.resilience.policies.composer import (
                        compose_async,
                    )
                    from baldur.resilience.policies.fallback import (
                        AsyncFallbackPolicy,
                    )

                    if asyncio.iscoroutinefunction(fallback):

                        async def fb_fn() -> T:
                            return await fallback(*args, **kwargs)

                    else:

                        async def fb_fn() -> T:
                            return fallback(*args, **kwargs)

                    fb_policy = AsyncFallbackPolicy(
                        fallback_fn=fb_fn,
                        predicate=_bulkhead_full_predicate,
                    )
                    result = await compose_async(fb_policy, bp).execute(
                        fn,
                        *args,
                        **kwargs,
                    )
                else:
                    result = await bp.execute(fn, *args, **kwargs)

                if result.success:
                    return result.value  # type: ignore[return-value]
                if result.error:
                    raise result.error
                return None

            return async_wrapper  # type: ignore

        @wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> T:
            from baldur.services.bulkhead.policy import BulkheadPolicy
            from baldur.services.bulkhead.registry import (
                get_bulkhead_registry,
            )

            registry = get_bulkhead_registry()
            key = name.value if isinstance(name, ConnectionType) else name
            bh = registry.get(key)
            bp = BulkheadPolicy(bulkhead=bh, timeout=timeout)

            if fallback is not None:
                from baldur.resilience.policies.composer import compose
                from baldur.resilience.policies.fallback import (
                    FallbackPolicy,
                )

                fb_policy = FallbackPolicy(
                    fallback_fn=lambda: fallback(*args, **kwargs),
                    predicate=_bulkhead_full_predicate,
                )
                result = compose(fb_policy, bp).execute(fn, *args, **kwargs)
            else:
                result = bp.execute(fn, *args, **kwargs)

            if result.success:
                return result.value  # type: ignore[return-value]
            if result.error:
                raise result.error
            return None

        return sync_wrapper

    return decorator


def bulkhead_for_database(  # noqa: C901
    alias: str = "default",
    timeout: float | None = None,
    fallback: Callable[..., T] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Per-DB-alias bulkhead decorator.

    Internally uses BulkheadPolicy/AsyncBulkheadPolicy and
    looks up the bulkhead for the given alias via the Registry's get_for_database().

    Args:
        alias: Django DB alias (default, replica, analytics, etc.)
        timeout: Resource acquisition wait timeout (seconds)
        fallback: (deprecated) Alternative function to call when the bulkhead is full.
                  Using the BulkheadPolicy + FallbackPolicy combination directly is recommended.

    Examples:
        @bulkhead_for_database("default")
        def write_to_db():
            Model.objects.using("default").create(...)

        @bulkhead_for_database("replica")
        def read_from_replica():
            return Model.objects.using("replica").all()
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:  # noqa: C901
        if asyncio.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> T:
                from baldur.services.bulkhead.policy import (
                    AsyncBulkheadPolicy,
                )
                from baldur.services.bulkhead.registry import (
                    get_bulkhead_registry,
                )

                registry = get_bulkhead_registry()
                bh = registry.get_for_database(alias)
                async_bh = registry.get_async(bh.name)
                bp = AsyncBulkheadPolicy(async_bulkhead=async_bh, timeout=timeout)

                if fallback is not None:
                    from baldur.resilience.policies.composer import (
                        compose_async,
                    )
                    from baldur.resilience.policies.fallback import (
                        AsyncFallbackPolicy,
                    )

                    if asyncio.iscoroutinefunction(fallback):

                        async def fb_fn() -> T:
                            return await fallback(*args, **kwargs)

                    else:

                        async def fb_fn() -> T:
                            return fallback(*args, **kwargs)

                    fb_policy = AsyncFallbackPolicy(
                        fallback_fn=fb_fn,
                        predicate=_bulkhead_full_predicate,
                    )
                    result = await compose_async(fb_policy, bp).execute(
                        fn,
                        *args,
                        **kwargs,
                    )
                else:
                    result = await bp.execute(fn, *args, **kwargs)

                if result.success:
                    return result.value  # type: ignore[return-value]
                if result.error:
                    raise result.error
                return None

            return async_wrapper  # type: ignore

        @wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> T:
            from baldur.services.bulkhead.policy import BulkheadPolicy
            from baldur.services.bulkhead.registry import (
                get_bulkhead_registry,
            )

            registry = get_bulkhead_registry()
            bh = registry.get_for_database(alias)
            bp = BulkheadPolicy(bulkhead=bh, timeout=timeout)

            if fallback is not None:
                from baldur.resilience.policies.composer import compose
                from baldur.resilience.policies.fallback import (
                    FallbackPolicy,
                )

                fb_policy = FallbackPolicy(
                    fallback_fn=lambda: fallback(*args, **kwargs),
                    predicate=_bulkhead_full_predicate,
                )
                result = compose(fb_policy, bp).execute(fn, *args, **kwargs)
            else:
                result = bp.execute(fn, *args, **kwargs)

            if result.success:
                return result.value  # type: ignore[return-value]
            if result.error:
                raise result.error
            return None

        return sync_wrapper

    return decorator


def bulkhead_for_cache(  # noqa: C901
    name: str = "default",
    timeout: float | None = None,
    fallback: Callable[..., T] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Per-cache-instance bulkhead decorator.

    Internally uses BulkheadPolicy/AsyncBulkheadPolicy and
    looks up the bulkhead for the given cache via the Registry's get_for_cache().

    Args:
        name: Cache name (default, session, etc.)
        timeout: Resource acquisition wait timeout (seconds)
        fallback: (deprecated) Alternative function to call when the bulkhead is full.
                  Using the BulkheadPolicy + FallbackPolicy combination directly is recommended.

    Examples:
        @bulkhead_for_cache("default")
        def get_cached_value(key: str):
            return cache.get(key)

        @bulkhead_for_cache("session")
        def get_session_data(session_id: str):
            return session_cache.get(session_id)
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:  # noqa: C901
        if asyncio.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> T:
                from baldur.services.bulkhead.policy import (
                    AsyncBulkheadPolicy,
                )
                from baldur.services.bulkhead.registry import (
                    get_bulkhead_registry,
                )

                registry = get_bulkhead_registry()
                bh = registry.get_for_cache(name)
                async_bh = registry.get_async(bh.name)
                bp = AsyncBulkheadPolicy(async_bulkhead=async_bh, timeout=timeout)

                if fallback is not None:
                    from baldur.resilience.policies.composer import (
                        compose_async,
                    )
                    from baldur.resilience.policies.fallback import (
                        AsyncFallbackPolicy,
                    )

                    if asyncio.iscoroutinefunction(fallback):

                        async def fb_fn() -> T:
                            return await fallback(*args, **kwargs)

                    else:

                        async def fb_fn() -> T:
                            return fallback(*args, **kwargs)

                    fb_policy = AsyncFallbackPolicy(
                        fallback_fn=fb_fn,
                        predicate=_bulkhead_full_predicate,
                    )
                    result = await compose_async(fb_policy, bp).execute(
                        fn,
                        *args,
                        **kwargs,
                    )
                else:
                    result = await bp.execute(fn, *args, **kwargs)

                if result.success:
                    return result.value  # type: ignore[return-value]
                if result.error:
                    raise result.error
                return None

            return async_wrapper  # type: ignore

        @wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> T:
            from baldur.services.bulkhead.policy import BulkheadPolicy
            from baldur.services.bulkhead.registry import (
                get_bulkhead_registry,
            )

            registry = get_bulkhead_registry()
            bh = registry.get_for_cache(name)
            bp = BulkheadPolicy(bulkhead=bh, timeout=timeout)

            if fallback is not None:
                from baldur.resilience.policies.composer import compose
                from baldur.resilience.policies.fallback import (
                    FallbackPolicy,
                )

                fb_policy = FallbackPolicy(
                    fallback_fn=lambda: fallback(*args, **kwargs),
                    predicate=_bulkhead_full_predicate,
                )
                result = compose(fb_policy, bp).execute(fn, *args, **kwargs)
            else:
                result = bp.execute(fn, *args, **kwargs)

            if result.success:
                return result.value  # type: ignore[return-value]
            if result.error:
                raise result.error
            return None

        return sync_wrapper

    return decorator
