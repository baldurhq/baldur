"""
Provider Registry and Factory for the Baldur System.

This package provides the centralized ProviderRegistry for all pluggable
components. Converted from a single-file module to a package per 354.

GenericProviderRegistry[T] (base.py) is the type-safe generic base.
ProviderRegistry (registry.py) is the thin facade composing sub-registries.

Usage:
    from baldur.factory import ProviderRegistry

    # Register and get providers via sub-registries
    ProviderRegistry.cache.register("redis", RedisCacheAdapter)
    cache = ProviderRegistry.cache.get("redis")

    # Backward-compatible convenience methods also available
    ProviderRegistry.register_cache("redis", RedisCacheAdapter)
    cache = ProviderRegistry.get_cache("redis")

Status: Internal
"""

from baldur.factory.base import GenericProviderRegistry
from baldur.factory.registry import (
    ProviderRegistry,
    get_circuit_breaker_repo,
    get_dlq_repo,
    get_storage_backend,
)

__all__ = [
    "GenericProviderRegistry",
    "ProviderRegistry",
    "get_storage_backend",
    "get_circuit_breaker_repo",
    "get_dlq_repo",
]
