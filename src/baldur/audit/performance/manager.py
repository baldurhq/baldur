"""
Performance Manager (Unified Access).

Provides unified management for all performance optimization components.
"""

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.performance.async_writer import AsyncAuditWriter
from baldur.audit.performance.batch_query import PipelineBatchQuery
from baldur.audit.performance.batch_writer import (
    BatchFlushConfig,
    BatchFlushWriter,
)
from baldur.audit.performance.lua_atomic import LuaAtomicHashChain
from baldur.audit.performance.sampling import SamplingVerifier
from baldur.audit.performance.watchdog import PendingSequenceWatchdog

logger = structlog.get_logger()


class HashChainPerformanceManager:
    """
    Unified manager for all performance optimization components.

    Provides lazy initialization and centralized access to:
    - lua_chain: LuaAtomicHashChain (5 RTT → 1 RTT)
    - batch_query: PipelineBatchQuery (batch state retrieval)
    - batch_writer: BatchFlushWriter (n×fsync → 1×fsync)
    - async_writer: AsyncAuditWriter (non-blocking writes)
    - sampler: SamplingVerifier (O(n) → O(k))
    - watchdog: PendingSequenceWatchdog (self-cleanup)

    Pattern source:
        adapters/django/middleware.py#L647 (lazy initialization)
    """

    def __init__(
        self,
        redis_client: Any | None = None,
        log_dir: Path | None = None,
        key_prefix: str = "baldur:",
    ):
        """
        Initialize performance manager.

        Args:
            redis_client: Redis client (required for distributed features)
            log_dir: Directory for log files
            key_prefix: Redis key prefix
        """
        self._redis = redis_client
        self._log_dir = Path(log_dir) if log_dir else Path("logs/audit")
        self._key_prefix = key_prefix
        self._lock = threading.RLock()

        # Lazy-initialized components
        self._lua_chain: LuaAtomicHashChain | None = None
        self._batch_query: PipelineBatchQuery | None = None
        self._batch_writer: BatchFlushWriter | None = None
        self._async_writer: AsyncAuditWriter | None = None
        self._sampler: SamplingVerifier | None = None
        self._watchdog: PendingSequenceWatchdog | None = None

    @property
    def lua_chain(self) -> LuaAtomicHashChain:
        """Lazy-init Lua atomic hash chain."""
        if self._lua_chain is None:
            with self._lock:
                if self._lua_chain is None:
                    if not self._redis:
                        raise ValueError("Redis client required for LuaAtomicHashChain")
                    self._lua_chain = LuaAtomicHashChain(
                        redis_client=self._redis,
                        key_prefix=self._key_prefix,
                    )
        return self._lua_chain

    @property
    def batch_query(self) -> PipelineBatchQuery:
        """Lazy-init pipeline batch query."""
        if self._batch_query is None:
            with self._lock:
                if self._batch_query is None:
                    if not self._redis:
                        raise ValueError("Redis client required for PipelineBatchQuery")
                    self._batch_query = PipelineBatchQuery(
                        redis_client=self._redis,
                        key_prefix=self._key_prefix,
                    )
        return self._batch_query

    @property
    def sampler(self) -> SamplingVerifier:
        """Lazy-init sampling verifier."""
        if self._sampler is None:
            with self._lock:
                if self._sampler is None:
                    self._sampler = SamplingVerifier()
        return self._sampler

    def get_batch_writer(
        self,
        file_path: Path,
        config: BatchFlushConfig | None = None,
    ) -> BatchFlushWriter:
        """Create batch writer for specific file."""
        return BatchFlushWriter(file_path, config)

    def get_async_writer(
        self,
        sync_writer: Callable[[dict[str, Any]], bool],
    ) -> AsyncAuditWriter:
        """Create async writer wrapping sync function."""
        return AsyncAuditWriter(sync_writer)

    def get_watchdog(self) -> PendingSequenceWatchdog:
        """Lazy-init pending sequence watchdog."""
        if self._watchdog is None:
            with self._lock:
                if self._watchdog is None:
                    if not self._redis:
                        raise ValueError(
                            "Redis client required for PendingSequenceWatchdog"
                        )
                    self._watchdog = PendingSequenceWatchdog(
                        redis_client=self._redis,
                        key_prefix=self._key_prefix,
                    )
        return self._watchdog

    def start_watchdog(self) -> None:
        """Start watchdog if not already running."""
        watchdog = self.get_watchdog()
        watchdog.start()

    def stop_all(self) -> None:
        """Stop all background components."""
        if self._watchdog and self._watchdog._is_running:
            self._watchdog.stop()

        if self._async_writer and self._async_writer._is_running:
            self._async_writer.stop()

        if self._batch_writer:
            self._batch_writer.close()

    def get_all_stats(self) -> dict[str, Any]:
        """Get statistics from all components."""
        stats = {}

        if self._batch_writer:
            stats["batch_writer"] = self._batch_writer.get_stats()

        if self._async_writer:
            stats["async_writer"] = self._async_writer.get_stats()

        if self._watchdog:
            stats["watchdog"] = self._watchdog.get_stats()

        return stats
