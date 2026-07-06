"""
Hash Chain Performance Optimization Components (Phase 3).

Provides high-performance features for distributed hash chain operations:

- HashChainPerformanceManager: Main facade for all performance features
- LuaAtomicHashChain: 5 RTT → 1 RTT via Lua script atomization
- PipelineBatchQuery: Multi-key batch retrieval via Redis pipeline
- BatchFlushWriter: n×fsync → 1×fsync via batched file writes
- AsyncAuditWriter: Non-blocking async write operations
- SamplingVerifier: O(n) → O(k) probabilistic chain verification
- PendingSequenceWatchdog: Self-cleanup daemon for stale entries

Usage:
    # Recommended: Use the main facade
    from baldur.audit.performance import HashChainPerformanceManager

    # Advanced: Direct access to specific components
    from baldur.audit.performance.lua_atomic import LuaAtomicHashChain
    from baldur.audit.performance.sampling import SamplingVerifier
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# =============================================================================
# PUBLIC API - Only the main facade is directly imported
# =============================================================================
from baldur.audit.performance.manager import HashChainPerformanceManager

# =============================================================================
# LAZY IMPORTS - Other classes loaded on-demand for backward compatibility
# =============================================================================

# Mapping of symbol names to their module paths
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # lua_atomic.py
    "LuaAtomicHashChain": (
        "baldur.audit.performance.lua_atomic",
        "LuaAtomicHashChain",
    ),
    # batch_query.py
    "PipelineBatchQuery": (
        "baldur.audit.performance.batch_query",
        "PipelineBatchQuery",
    ),
    # batch_writer.py
    "BatchFlushConfig": (
        "baldur.audit.performance.batch_writer",
        "BatchFlushConfig",
    ),
    "BatchFlushWriter": (
        "baldur.audit.performance.batch_writer",
        "BatchFlushWriter",
    ),
    # async_writer.py
    "AsyncAuditWriter": (
        "baldur.audit.performance.async_writer",
        "AsyncAuditWriter",
    ),
    # sampling.py
    "SamplingConfig": ("baldur.audit.performance.sampling", "SamplingConfig"),
    "SamplingVerifier": ("baldur.audit.performance.sampling", "SamplingVerifier"),
    # watchdog.py
    "PendingSequenceWatchdog": (
        "baldur.audit.performance.watchdog",
        "PendingSequenceWatchdog",
    ),
}

# Cache for lazily loaded modules
_loaded_symbols: dict[str, object] = {}


def __getattr__(name: str) -> object:
    """Lazy import for backward compatibility.

    This allows:
        from baldur.audit.performance import LuaAtomicHashChain

    Without loading all modules at package import time.
    """
    if name in _loaded_symbols:
        return _loaded_symbols[name]

    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        import importlib

        module = importlib.import_module(module_path)
        symbol = getattr(module, attr_name)
        _loaded_symbols[name] = symbol
        return symbol

    raise AttributeError(f"module 'baldur.audit.performance' has no attribute '{name}'")


def __dir__() -> list[str]:
    """List available symbols for IDE autocompletion."""
    return list(__all__)


# TYPE_CHECKING block for IDE support without runtime import
if TYPE_CHECKING:
    from baldur.audit.performance.async_writer import AsyncAuditWriter
    from baldur.audit.performance.batch_query import PipelineBatchQuery
    from baldur.audit.performance.batch_writer import (
        BatchFlushConfig,
        BatchFlushWriter,
    )
    from baldur.audit.performance.lua_atomic import LuaAtomicHashChain
    from baldur.audit.performance.sampling import SamplingConfig, SamplingVerifier
    from baldur.audit.performance.watchdog import PendingSequenceWatchdog


# =============================================================================
# __all__ - Only expose what's commonly needed
# =============================================================================
__all__ = [
    # Primary API (always use this)
    "HashChainPerformanceManager",
    # Secondary (available via lazy import)
    "LuaAtomicHashChain",
    "PipelineBatchQuery",
    "BatchFlushConfig",
    "BatchFlushWriter",
    "AsyncAuditWriter",
    "SamplingConfig",
    "SamplingVerifier",
    "PendingSequenceWatchdog",
]
