"""Cache-adapter doubles for the idempotency cache resolver.

- :class:`DistributedCacheStandIn` — the real in-process cache adapter
  reporting a distributed ``provider_name``. In production the resolver refuses
  Baldur's in-process default (``provider_name == "memory"``) like a missing
  adapter, so a test that models "a shared cache is registered" needs an
  adapter that says it is one. Subclassing the in-process adapter keeps the
  atomic ``setnx`` / ``cas_*`` overrides the idempotency gate validates, so the
  stand-in also backs real sync dedup without a server. The async resolver
  maps a ``"redis"`` provider to a real async Redis adapter, so an async gate
  resolved over this stand-in dials Redis.

Usage:
    from tests.factories.cache_doubles import DistributedCacheStandIn
"""

from __future__ import annotations

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter

__all__ = ["DistributedCacheStandIn"]


class DistributedCacheStandIn(InMemoryCacheAdapter):
    """In-process cache adapter that reports itself as a Redis backend."""

    @property
    def provider_name(self) -> str:
        return "redis"
