"""
Bulkhead Policy — resource-isolation-based function-wrapping Policy.

Wraps the Bulkhead ABC implementations with the ResiliencePolicy.execute()
interface.

Injects the Bulkhead instance via DI in the constructor to enable Registry-independent testing, and
the bulkhead_policy() / async_bulkhead_policy() factory functions handle Registry integration.

Exception handling contract (same as CircuitBreakerPolicy):
- BulkheadFullError → absorbed into PolicyResult(outcome=REJECTED)
- BulkheadTimeoutError → absorbed into PolicyResult(outcome=TIMEOUT) (raised only
  by implementations that bound execution time, e.g. worker-pool bulkheads)
- Business exception during function execution → re-propagated via raise (handled by upstream Policy)

Composition:
- BulkheadPolicy: synchronous bulkhead (any Bulkhead ABC implementation)
- AsyncBulkheadPolicy: asynchronous bulkhead (AsyncSemaphoreBulkhead)
- bulkhead_policy(): synchronous factory (BulkheadRegistry singleton integration)
- async_bulkhead_policy(): asynchronous factory (BulkheadRegistry singleton integration)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import structlog

from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)
from baldur.services.bulkhead.async_semaphore import AsyncSemaphoreBulkhead
from baldur.services.bulkhead.base import Bulkhead
from baldur.services.bulkhead.exceptions import (
    BulkheadFullError,
    BulkheadTimeoutError,
)

logger = structlog.get_logger()

T = TypeVar("T")

__all__ = [
    "AsyncBulkheadPolicy",
    "BulkheadPolicy",
    "async_bulkhead_policy",
    "bulkhead_policy",
]


# =============================================================================
# BulkheadPolicy — synchronous bulkhead Policy
# =============================================================================


class BulkheadPolicy(ResiliencePolicy[T]):
    """
    Synchronous Bulkhead Policy — resource isolation.

    Delegates execution polymorphically to ``Bulkhead.execute()`` — each
    implementation supplies its own execution semantics (semaphore: run in the
    calling thread, ``timeout`` bounds the admission wait; worker-pool
    implementations may offload and also bound execution time).

    Exception handling contract (same as CircuitBreakerPolicy):
    - BulkheadFullError → absorbed into PolicyResult(outcome=REJECTED)
    - BulkheadTimeoutError → absorbed into PolicyResult(outcome=TIMEOUT)
      (unreachable on the semaphore path — admission failure raises FullError)
    - Business exception during function execution → re-propagated via raise
    """

    def __init__(
        self,
        bulkhead: Bulkhead,
        timeout: float | None = None,
    ):
        """
        Args:
            bulkhead: Bulkhead ABC implementation.
                      Injected via DI. Registry lookup uses the bulkhead_policy() factory.
            timeout: Timeout (seconds) forwarded to ``Bulkhead.execute()``.
                     Semaphore implementations bound the admission wait with it
                     (None = fail immediately, Fast Fail); worker-pool
                     implementations bound execution time with it.
        """
        self._bulkhead = bulkhead
        self._timeout = timeout

    @property
    def name(self) -> str:
        """Policy name."""
        return "bulkhead"

    @property
    def bulkhead_name(self) -> str:
        """Domain identifier of the internal Bulkhead instance."""
        return self._bulkhead.name

    def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Execute the function within the bulkhead resource.

        A single polymorphic call — ``self._bulkhead.execute()`` — carries the
        implementation-specific semantics; this policy only maps exceptions to
        outcomes:

        - BulkheadFullError → PolicyOutcome.REJECTED
        - BulkheadTimeoutError → PolicyOutcome.TIMEOUT
        - Business exceptions during function execution re-propagate via raise.
        """
        try:
            result = self._bulkhead.execute(
                func,
                *args,
                timeout=self._timeout,
                **kwargs,
            )
            return PolicyResult(
                value=result,
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=["bulkhead"],
                metadata={
                    "bulkhead_name": self._bulkhead.name,
                    "state": self._get_state_dict(),
                },
            )
        except BulkheadFullError as e:
            return PolicyResult(
                outcome=PolicyOutcome.REJECTED,
                error=e,
                executed_policies=["bulkhead"],
                metadata={
                    "bulkhead_name": self._bulkhead.name,
                    "state": self._get_state_dict(),
                },
            )
        except BulkheadTimeoutError as e:
            return PolicyResult(
                outcome=PolicyOutcome.TIMEOUT,
                error=e,
                executed_policies=["bulkhead"],
                metadata={
                    "bulkhead_name": self._bulkhead.name,
                    "timeout": self._timeout,
                    "state": self._get_state_dict(),
                },
            )
        # Business exceptions during function execution are not caught and propagate upward (raise)

    def _get_state_dict(self) -> dict:
        """Convert the Bulkhead state into a serializable dictionary."""
        state = self._bulkhead.get_state()
        return {
            "active_count": state.active_count,
            "max_concurrent": state.max_concurrent,
            "available_permits": state.available_permits,
            "utilization_percent": state.utilization_percent,
        }


# =============================================================================
# AsyncBulkheadPolicy — asynchronous bulkhead Policy
# =============================================================================


class AsyncBulkheadPolicy:
    """
    Asynchronous Bulkhead Policy — AsyncResiliencePolicy Protocol implementation.

    Wraps AsyncSemaphoreBulkhead and returns results in PolicyResult form.
    Follows the same exception handling contract as the synchronous BulkheadPolicy.

    Since AsyncSemaphoreBulkhead is a separate class that does not inherit from Bulkhead(ABC),
    it is implemented as a class fully separated from the synchronous BulkheadPolicy.
    """

    def __init__(
        self,
        async_bulkhead: AsyncSemaphoreBulkhead,
        timeout: float | None = None,
    ):
        """
        Args:
            async_bulkhead: AsyncSemaphoreBulkhead instance (DI).
            timeout: Resource acquisition timeout (seconds). If None, fail immediately.
        """
        self._async_bulkhead = async_bulkhead
        self._timeout = timeout

    @property
    def name(self) -> str:
        """Policy name."""
        return "bulkhead"

    @property
    def bulkhead_name(self) -> str:
        """Domain identifier of the internal AsyncSemaphoreBulkhead."""
        return self._async_bulkhead.name

    async def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Execute the function within the asynchronous bulkhead resource.

        BulkheadFullError → absorbed into PolicyResult(outcome=REJECTED).
        Business exceptions during function execution are re-propagated via raise.
        """
        try:
            async with self._async_bulkhead.acquire(timeout=self._timeout):
                result = await func(*args, **kwargs)
                return PolicyResult(
                    value=result,
                    outcome=PolicyOutcome.SUCCESS,
                    executed_policies=["bulkhead"],
                    metadata={
                        "bulkhead_name": self._async_bulkhead.name,
                        "state": self._get_state_dict(),
                    },
                )
        except BulkheadFullError as e:
            return PolicyResult(
                outcome=PolicyOutcome.REJECTED,
                error=e,
                executed_policies=["bulkhead"],
                metadata={
                    "bulkhead_name": self._async_bulkhead.name,
                    "state": self._get_state_dict(),
                },
            )
        # Business exceptions during function execution are not caught and propagate upward

    def _get_state_dict(self) -> dict:
        """Convert the AsyncSemaphoreBulkhead state into a serializable dictionary."""
        state = self._async_bulkhead.get_state()
        return {
            "active_count": state.active_count,
            "max_concurrent": state.max_concurrent,
            "available_permits": state.available_permits,
            "utilization_percent": state.utilization_percent,
        }


# =============================================================================
# Factory functions — BulkheadRegistry singleton integration
# =============================================================================


def bulkhead_policy(
    name: str,
    max_concurrent: int | None = None,
    timeout: float | None = None,
    bulkhead_type: str = "semaphore",
) -> BulkheadPolicy:
    """
    BulkheadPolicy factory — BulkheadRegistry singleton integration.

    Calls the Registry's get_or_create() to guarantee a single
    global Bulkhead instance for the same name.

    The Registry dependency occurs only in this factory function.
    The BulkheadPolicy class itself does not know the Registry (test-friendly).

    Args:
        name: Domain name (Registry key)
        max_concurrent: Maximum concurrent execution count (Registry default if None)
        timeout: Resource acquisition timeout (fail immediately if None)
        bulkhead_type: "semaphore" or "thread_pool"

    Returns:
        BulkheadPolicy instance (uses the Registry singleton Bulkhead)
    """
    from baldur.services.bulkhead.registry import get_bulkhead_registry

    registry = get_bulkhead_registry()
    bulkhead = registry.get_or_create(
        name=name,
        max_concurrent=max_concurrent,
        bulkhead_type=bulkhead_type,
    )
    return BulkheadPolicy(bulkhead=bulkhead, timeout=timeout)


def async_bulkhead_policy(
    name: str,
    max_concurrent: int | None = None,
    timeout: float | None = None,
) -> AsyncBulkheadPolicy:
    """
    AsyncBulkheadPolicy factory — BulkheadRegistry singleton integration.

    Calls the Registry's get_async() to guarantee a single
    global AsyncSemaphoreBulkhead instance for the same name.

    Args:
        name: Domain name (Registry key)
        max_concurrent: Maximum concurrent execution count (Registry default if None).
                        The synchronous Bulkhead is provisioned first (so the domain
                        is registry-visible to the admin API, metrics, and shutdown
                        iteration) and used as the configuration basis for the
                        asynchronous instance.
        timeout: Resource acquisition timeout (fail immediately if None)

    Returns:
        AsyncBulkheadPolicy instance (uses the Registry singleton AsyncSemaphoreBulkhead)
    """
    from baldur.services.bulkhead.registry import get_bulkhead_registry

    registry = get_bulkhead_registry()

    # Provision the synchronous twin unconditionally so the domain is
    # registry-visible and get_async() derives capacity from it (async is based
    # on sync configuration). get_or_create() falls back to the registry default
    # when max_concurrent is None.
    registry.get_or_create(name=name, max_concurrent=max_concurrent)

    async_bh = registry.get_async(name)
    return AsyncBulkheadPolicy(async_bulkhead=async_bh, timeout=timeout)
