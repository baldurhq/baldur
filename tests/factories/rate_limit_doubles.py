"""Rate-limit storage doubles and the coordinator's thread-hop spy.

Shared by the coordinator's own tests and by every retry / bridge / facade
surface that drives a real ``RateLimitCoordinator`` over a controlled store:

- :class:`RaisingRateLimitStorage` — one named store method raises, the rest
  delegate (a backend fault at a single call site, for the fail-open wraps).
- :class:`NetworkBackedRateLimitStorage` — the real in-process adapter
  reporting a network-backed ``storage_type`` (drives the worker-thread hop
  branch of the coordinator's awaitable surface without a server).
- :class:`ToThreadSpy` — records what a module hops through
  ``asyncio.to_thread`` and then really hops, so a test can assert which
  calls left the event loop and which ran inline.

Usage:
    from tests.factories.rate_limit_doubles import (
        NetworkBackedRateLimitStorage,
        RaisingRateLimitStorage,
        ToThreadSpy,
    )
"""

from __future__ import annotations

import asyncio
from typing import Any

from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.interfaces.rate_limit_storage import RateLimitStorageType

__all__ = [
    "NetworkBackedRateLimitStorage",
    "RaisingRateLimitStorage",
    "ToThreadSpy",
]


class RaisingRateLimitStorage:
    """Storage double that raises on ONE named method, delegating the rest.

    Models a backend fault (Redis down / thread exhaustion) at a single call
    site so each coordinator fail-open wrap can be exercised in isolation. A
    spec-less dynamic wrapper by design — it forwards every real method except
    the one under fault — so the delegation cannot silently drift from the inner
    double's surface.
    """

    def __init__(self, inner: Any, fail_on: str):
        self._inner = inner
        self._fail_on = fail_on

    def __getattr__(self, name: str) -> Any:
        if name == self._fail_on:

            def _raise(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError(f"storage down: {name}")

            return _raise
        return getattr(self._inner, name)


class NetworkBackedRateLimitStorage:
    """An in-process store that reports a network-backed type.

    The coordinator's thread-hop rule keys on ``storage_type``, not on where
    the state lives — the Redis adapter reports ``REDIS`` while serving its
    in-process fallback too — so a wrapper over the real memory adapter is
    enough to drive the hop branch.
    """

    def __init__(self, inner: Any = None):
        self._inner = inner or InMemoryRateLimitStorage()

    @property
    def storage_type(self) -> RateLimitStorageType:
        return RateLimitStorageType.REDIS

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class ToThreadSpy:
    """Records what a module hops to a worker thread, then really hops.

    Installed over a module's ``asyncio.to_thread`` reference; ``calls`` holds
    the hopped callables in order.
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.call_args: list[tuple[Any, ...]] = []
        self._real = asyncio.to_thread

    async def __call__(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(fn)
        self.call_args.append(args)
        return await self._real(fn, *args, **kwargs)

    def hopped(self, method_name: str) -> bool:
        """Whether a callable named ``method_name`` was hopped at least once."""
        return any(getattr(fn, "__name__", "") == method_name for fn in self.calls)

    def count(self, method_name: str) -> int:
        """How many times a callable named ``method_name`` was hopped."""
        return sum(1 for fn in self.calls if getattr(fn, "__name__", "") == method_name)
