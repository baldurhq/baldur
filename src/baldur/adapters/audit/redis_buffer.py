"""
Redis-based distributed audit buffer.

Pattern references:
- CB Advanced Protection's Redis-First + WAL pattern
- RedisMetricSourceAdapter's Write-Through pattern

Features:
- Processing Queue pattern
- ActiveKeySet O(1) domain lookup
- Chunked pipeline writes
- LTRIM safety net
- Graceful shutdown
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    import redis

    from baldur.interfaces.audit_adapter import AuditEntry


logger = structlog.get_logger()


# Environment-variable defaults
_MAX_PIPELINE_CHUNK = int(os.environ.get("BALDUR_MAX_PIPELINE_CHUNK", "1000"))
_BUFFER_WARNING_THRESHOLD = int(os.environ.get("BALDUR_BUFFER_WARNING", "10000"))
_BUFFER_CRITICAL_THRESHOLD = int(os.environ.get("BALDUR_BUFFER_CRITICAL", "50000"))
_SAFETY_LTRIM_THRESHOLD = int(os.environ.get("BALDUR_SAFETY_LTRIM", "100000"))

# One-shot writer-footgun warning: emitted at most once per process when a
# buffer is constructed while the effective drain gate is OFF, so an operator
# who assembles a write-side buffer per the class docstring but forgets
# BALDUR_AUDIT_BUFFER_REDIS_ENABLED gets a loud signal instead of silent TTL
# expiry. A module flag (test-resettable) keeps it one-shot.
_drain_disabled_warning_emitted = False
_drain_disabled_warning_lock = threading.Lock()


def _reset_drain_disabled_warning() -> None:
    """Re-arm the one-shot drain-disabled warning (test isolation)."""
    global _drain_disabled_warning_emitted
    with _drain_disabled_warning_lock:
        _drain_disabled_warning_emitted = False


def _get_audit_buffer_ttl() -> int:
    """Read the Redis buffer TTL from AuditSettings."""
    try:
        from baldur.settings.audit import get_audit_settings

        return get_audit_settings().buffer_redis_ttl
    except Exception:
        return 86400  # 24-hour fallback


class AuditLogAdapterProtocol(Protocol):
    """Audit log adapter protocol.

    Only ``log()`` is required — ``log_raw()`` is a runtime-detected
    optional fast-path (see hasattr checks in flush_to_target).

    Matches the strict contract on ``AuditLogAdapter.log(entry: AuditEntry)``
    so FileAuditLogAdapter (and other concrete adapters) structurally satisfy
    this Protocol.
    """

    def log(self, entry: AuditEntry) -> None:
        """Log an entry."""
        ...


class RedisAuditBuffer:
    """
    Redis audit buffer for distributed deployments.

    Characteristics:
    - All instances share the same Redis buffer
    - LPUSH/RPOP preserves FIFO ordering
    - Automatic fallback to FileAuditLogAdapter on Redis failure
    - Local WAL → Redis resync once Redis recovers
    - Processing Queue pattern prevents data loss
    - ActiveKeySet gives O(1) domain lookup

    Usage:
        # 1. Enable the drain pipeline so the scheduled flush tasks actually
        #    move buffered entries to the audit sink (otherwise entries sit in
        #    Redis until the TTL expires and are silently lost):
        #        BALDUR_AUDIT_ENABLED=true
        #        BALDUR_AUDIT_BUFFER_REDIS_ENABLED=true
        # 2. Assemble the write-side buffer in the host application:
        redis_client = redis.from_url("redis://localhost:6379")
        file_fallback = FileAuditLogAdapter(file_path="/var/log/audit/audit.jsonl")

        buffer = RedisAuditBuffer(
            redis_client=redis_client,
            fallback_adapter=file_fallback,
        )

        buffer.log(entry, domain="payment")
    """

    DEFAULT_KEY_PREFIX = "audit:"
    DEFAULT_TTL_SECONDS = 86400  # Legacy constant kept for backward compatibility
    MAX_CONSECUTIVE_FAILURES = 3
    ACTIVE_DOMAINS_SET = "audit:active_domains"  # ActiveKeySet

    def __init__(
        self,
        redis_client: redis.Redis,
        fallback_adapter: AuditLogAdapterProtocol | None = None,
        key_prefix: str | None = None,
        ttl_seconds: int | None = None,
        on_fallback: Callable[[Exception], None] | None = None,
        enable_graceful_shutdown: bool = True,
    ):
        """
        Initialize the RedisAuditBuffer.

        Args:
            redis_client: Redis client
            fallback_adapter: Fallback adapter (file, etc.)
            key_prefix: Redis key prefix (None = default)
            ttl_seconds: TTL in seconds. None reads from settings.
            on_fallback: Callback invoked when a fallback occurs
            enable_graceful_shutdown: Whether to register shutdown hooks
        """
        # redis-py stub declares dual sync/async return unions; widening to Any
        # at the attribute keeps mypy out of every sync call site (see
        # airgap/redis_adapter.py for the same precedent).
        self._redis: Any = redis_client
        self._fallback = fallback_adapter
        self._key_prefix = (
            key_prefix if key_prefix is not None else self.DEFAULT_KEY_PREFIX
        )
        self._ttl_seconds = (
            ttl_seconds if ttl_seconds is not None else _get_audit_buffer_ttl()
        )
        self._on_fallback = on_fallback

        # Failure tracking (CB Advanced Protection pattern)
        self._consecutive_failures = 0
        self._lock = threading.Lock()

        # Fallback buffer (temporary storage while Redis is down)
        self._fallback_buffer: list[dict[str, Any]] = []
        self._fallback_lock = threading.Lock()
        self._max_fallback = int(os.environ.get("BALDUR_REDIS_MAX_FALLBACK", "10000"))

        # Worker identifier (Processing Queue pattern)
        self._worker_id = f"{socket.gethostname()}-{os.getpid()}"

        # Lua scripts (lazy init)
        self._lua_scripts = None

        # Statistics
        self._total_writes = 0
        self._total_fallbacks = 0
        self._total_flushes = 0
        self._total_batch_writes = 0
        self._total_batch_errors = 0
        self._total_safety_ltrim = 0

        # Graceful-shutdown hook registration
        self._shutdown_registered = False
        if enable_graceful_shutdown:
            self._register_shutdown_hooks()

        # Writer-footgun alarm: warn once if a buffer is assembled while the
        # drain pipeline is gated off (D3). The drain-side accessor only
        # constructs when the gate is ON, so this never fires a false positive
        # on the read path.
        self._warn_if_drain_disabled()

    def _warn_if_drain_disabled(self) -> None:
        """Emit a one-shot WARNING when the effective drain gate is OFF."""
        global _drain_disabled_warning_emitted
        if _drain_disabled_warning_emitted:
            return
        try:
            from baldur.settings.audit import is_redis_drain_enabled

            if is_redis_drain_enabled():
                return
            with _drain_disabled_warning_lock:
                if _drain_disabled_warning_emitted:
                    return
                _drain_disabled_warning_emitted = True
            logger.warning(
                "redis_audit_buffer.drain_disabled",
                hint=(
                    "RedisAuditBuffer constructed but the drain pipeline is "
                    "gated off; buffered entries will expire by TTL without "
                    "being flushed. Set BALDUR_AUDIT_ENABLED=true and "
                    "BALDUR_AUDIT_BUFFER_REDIS_ENABLED=true to drain."
                ),
            )
        except Exception:
            # Settings access must never break buffer construction.
            pass

    def log(self, entry: dict[str, Any], domain: str = "default") -> bool:
        """
        Log an audit entry.

        Args:
            entry: Audit entry dict
            domain: Domain (key partitioning)

        Returns:
            True if the Redis write succeeded, False if the fallback was used
        """
        key = f"{self._key_prefix}{{{domain}}}:buffer"

        try:
            payload = {
                "entry": entry,
                "timestamp": utc_now().isoformat(),
                "instance_id": self._get_instance_id(),
            }

            pipe = self._redis.pipeline()
            pipe.lpush(key, fast_dumps_str(payload, default=str))
            pipe.expire(key, self._ttl_seconds)
            pipe.execute()

            # Reset the failure count on success
            with self._lock:
                self._consecutive_failures = 0
                self._total_writes += 1

            return True

        except Exception as e:
            logger.warning(
                "redis_audit_buffer.redis_write_failed",
                error=e,
            )

            with self._lock:
                self._consecutive_failures += 1
                self._total_fallbacks += 1

            if self._on_fallback:
                try:
                    self._on_fallback(e)
                except Exception:
                    pass

            # Fallback to file
            if self._fallback:
                try:
                    if hasattr(self._fallback, "log_raw"):
                        self._fallback.log_raw(entry)
                    else:
                        # FileAuditLogAdapter.log handles dict+AuditEntry at
                        # runtime (isinstance check); the Protocol declares
                        # AuditEntry for the strict contract.
                        self._fallback.log(entry)  # type: ignore[arg-type]
                    logger.info("redis_audit_buffer.used_file_fallback")
                except Exception as fallback_error:
                    logger.exception(
                        "redis_audit_buffer.fallback_also_failed",
                        fallback_error=fallback_error,
                    )

            return False

    def log_batch(
        self,
        entries: list[dict[str, Any]],
        domain: str = "default",
    ) -> bool:
        """
        Batch logging — store multiple events via a chunked Redis pipeline.

        Cuts Redis RTT from N round-trips to one per chunk compared to
        individual log() calls. Splits into MAX_PIPELINE_CHUNK-sized
        chunks for memory protection.

        Args:
            entries: Audit entry dicts to store
            domain: Domain (key partitioning)

        Returns:
            True if Redis succeeded, False if the fallback buffer was used
        """
        if not entries:
            return True

        total = len(entries)
        success = True

        # Chunked writes
        for chunk_start in range(0, total, _MAX_PIPELINE_CHUNK):
            chunk_end = min(chunk_start + _MAX_PIPELINE_CHUNK, total)
            chunk = entries[chunk_start:chunk_end]

            if not self._log_batch_chunk(chunk, domain):
                success = False
                # Store failed chunks in the fallback buffer
                self._store_in_fallback_buffer(chunk, domain)

        return success

    def _log_batch_chunk(
        self,
        entries: list[dict[str, Any]],
        domain: str = "default",
    ) -> bool:
        """
        Process a single chunk (at most MAX_PIPELINE_CHUNK entries).

        Args:
            entries: Entries to store
            domain: Domain

        Returns:
            Whether the write succeeded
        """
        if not entries:
            return True

        key = f"{self._key_prefix}{{{domain}}}:buffer"
        timestamp = utc_now().isoformat()
        instance_id = self._get_instance_id()

        try:
            pipe = self._redis.pipeline(transaction=True)

            # LPUSH all items at once
            payloads = []
            for entry in entries:
                payload = {
                    "entry": entry,
                    "timestamp": timestamp,
                    "instance_id": instance_id,
                }
                payloads.append(fast_dumps_str(payload, default=str))

            if payloads:
                pipe.lpush(key, *payloads)
                pipe.expire(key, self._ttl_seconds)

                # Add the domain to the ActiveKeySet
                pipe.sadd(self.ACTIVE_DOMAINS_SET, domain)
                pipe.expire(self.ACTIVE_DOMAINS_SET, 86400)

                pipe.execute()

            # Update statistics on success
            with self._lock:
                self._consecutive_failures = 0
                self._total_batch_writes += 1
                self._total_writes += len(entries)

            logger.debug(
                "redis_audit_buffer.batch_chunk_logged_entries",
                entries_count=len(entries),
            )
            return True

        except Exception as e:
            logger.warning(
                f"[RedisAuditBuffer] Batch chunk failed: {e}",  # noqa: G004
                extra={"entries_count": len(entries), "domain": domain},
            )

            with self._lock:
                self._consecutive_failures += 1
                self._total_batch_errors += 1

            return False

    def _store_in_fallback_buffer(
        self,
        entries: list[dict[str, Any]],
        domain: str = "default",
    ) -> None:
        """
        Temporarily store entries in the in-memory fallback buffer.

        Args:
            entries: Entries to store
            domain: Domain
        """
        with self._fallback_lock:
            for entry in entries:
                self._fallback_buffer.append(
                    {
                        "entry": entry,
                        "domain": domain,
                        "timestamp": utc_now().isoformat(),
                    }
                )

            # Bound the fallback buffer size (memory protection)
            if len(self._fallback_buffer) > self._max_fallback:
                overflow = len(self._fallback_buffer) - self._max_fallback
                self._fallback_buffer = self._fallback_buffer[overflow:]
                logger.warning(
                    "redis_audit_buffer.fallback_buffer_overflow_dropped",
                    overflow=overflow,
                )

    def retry_fallback_buffer(self) -> int:
        """
        Retry writing fallback-buffer entries to Redis.

        Returns:
            Number of successfully recovered entries
        """
        with self._fallback_lock:
            if not self._fallback_buffer:
                return 0

            # Group by domain
            entries_by_domain: dict[str, list[dict[str, Any]]] = {}
            for item in self._fallback_buffer:
                domain = item.get("domain", "default")
                if domain not in entries_by_domain:
                    entries_by_domain[domain] = []
                entries_by_domain[domain].append(item["entry"])

            # Batch-process each domain
            recovered = 0
            failed_items: list[dict[str, Any]] = []

            for domain, domain_entries in entries_by_domain.items():
                try:
                    # Call _log_batch_chunk directly to avoid fallback-buffer
                    # re-entry (log_batch calls _store_in_fallback_buffer on
                    # failure, which would deadlock here)
                    if self._log_batch_chunk(domain_entries, domain):
                        recovered += len(domain_entries)
                    else:
                        # Keep failures in the fallback buffer
                        for entry in domain_entries:
                            failed_items.append(
                                {
                                    "entry": entry,
                                    "domain": domain,
                                    "timestamp": utc_now().isoformat(),
                                }
                            )
                except Exception:
                    for entry in domain_entries:
                        failed_items.append(
                            {
                                "entry": entry,
                                "domain": domain,
                                "timestamp": utc_now().isoformat(),
                            }
                        )

            # Retain only the failed items
            self._fallback_buffer = failed_items

            return recovered

    def get_fallback_buffer_size(self) -> int:
        """Current size of the fallback buffer."""
        with self._fallback_lock:
            return len(self._fallback_buffer)

    def get_buffer_stats(self) -> dict[str, Any]:
        """Query buffer statistics."""
        stats: dict[str, Any] = {
            "consecutive_failures": self._consecutive_failures,
            "total_writes": self._total_writes,
            "total_fallbacks": self._total_fallbacks,
            "total_flushes": self._total_flushes,
            "total_batch_writes": self._total_batch_writes,
            "total_batch_errors": self._total_batch_errors,
            "total_safety_ltrim": self._total_safety_ltrim,
            "fallback_buffer_size": self.get_fallback_buffer_size(),
            "domains": {},
        }

        try:
            for domain in self._get_active_domains():
                key = f"{self._key_prefix}{{{domain}}}:buffer"
                size = self._redis.llen(key)
                stats["domains"][domain] = size

                # Threshold check + alerting
                self._check_buffer_threshold(domain, size)
        except Exception as e:
            logger.debug(
                "redis_audit_buffer.stats_query_failed",
                error=e,
            )
            stats["error"] = str(e)

        return stats

    def _check_buffer_threshold(self, domain: str, size: int) -> None:
        """Check buffer thresholds and emit alerts."""
        try:
            from baldur.metrics.audit_buffer_metrics import (
                audit_buffer_backpressure,
                audit_buffer_size,
            )

            # Update metrics
            audit_buffer_size.labels(domain=domain).set(size)
            backpressure = min(1.0, size / max(1, _SAFETY_LTRIM_THRESHOLD))
            audit_buffer_backpressure.labels(domain=domain).set(backpressure)
        except ImportError:
            pass

        if size >= _BUFFER_CRITICAL_THRESHOLD:
            logger.exception(
                f"[RedisAuditBuffer] CRITICAL: Buffer overflow for {domain}",  # noqa: G004
                extra={
                    "domain": domain,
                    "size": size,
                    "threshold": _BUFFER_CRITICAL_THRESHOLD,
                },
            )
        elif size >= _BUFFER_WARNING_THRESHOLD:
            logger.warning(
                f"[RedisAuditBuffer] WARNING: Buffer high for {domain}",  # noqa: G004
                extra={
                    "domain": domain,
                    "size": size,
                    "threshold": _BUFFER_WARNING_THRESHOLD,
                },
            )

    def get_pending_count(self, domain: str = "default") -> int:
        """Pending entry count for a domain."""
        try:
            key = f"{self._key_prefix}{{{domain}}}:buffer"
            return self._redis.llen(key)
        except Exception:
            return -1

    def is_healthy(self) -> bool:
        """Check the Redis connection."""
        try:
            self._redis.ping()
            return True
        except Exception:
            return False

    def should_use_fallback(self) -> bool:
        """Whether consecutive failures force fallback use."""
        with self._lock:
            return self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES

    def _get_instance_id(self) -> str:
        """Identifier of the current instance."""
        return os.environ.get("HOSTNAME", os.environ.get("INSTANCE_ID", "unknown"))

    def _get_lua_scripts(self):
        """Lazily initialize the Lua scripts."""
        if self._lua_scripts is None:
            from baldur.audit.redis_batch_lua import AuditBatchLuaScripts

            self._lua_scripts = AuditBatchLuaScripts(self._redis)
        return self._lua_scripts

    def _get_active_domains(self) -> list[str]:
        """
        Query active domains (O(1)).

        Uses the ActiveKeySet for fast lookup.
        """
        try:
            domains = self._redis.smembers(self.ACTIVE_DOMAINS_SET)

            # Prune empty domains
            empty_domains = []
            result_domains = []

            for domain in domains:
                domain_str = domain.decode() if isinstance(domain, bytes) else domain
                key = f"{self._key_prefix}{{{domain_str}}}:buffer"

                if self._redis.llen(key) == 0:
                    empty_domains.append(domain_str)
                else:
                    result_domains.append(domain_str)

            # Remove empty domains from the set
            if empty_domains:
                self._redis.srem(self.ACTIVE_DOMAINS_SET, *empty_domains)

            return result_domains

        except Exception as e:
            logger.debug(
                "redis_audit_buffer.activekeyset_query_failed",
                error=e,
            )
            return self._get_active_domains_fallback()

    def _get_active_domains_fallback(self) -> list[str]:
        """scan_iter-based fallback (when the ActiveKeySet is unavailable)."""
        import re

        domains = set()
        try:
            for key in self._redis.scan_iter(f"{self._key_prefix}*:buffer"):
                key_str = key.decode() if isinstance(key, bytes) else key
                # Extract domain from "audit:{domain}:buffer"
                m = re.search(r"\{(.+?)\}", key_str)
                if m:
                    domains.add(m.group(1))
        except Exception:
            pass
        return list(domains)

    def _add_to_active_domains(self, domains: set[str]) -> None:
        """Add domains to the active-domain set."""
        if domains:
            try:
                pipe = self._redis.pipeline()
                pipe.sadd(self.ACTIVE_DOMAINS_SET, *domains)
                pipe.expire(self.ACTIVE_DOMAINS_SET, 86400)  # 24-hour TTL
                pipe.execute()
            except Exception as e:
                logger.debug(
                    "redis_audit_buffer.update_active_domains_failed",
                    error=e,
                )

    def apply_safety_ltrim(self) -> dict[str, int]:
        """
        Apply LTRIM when the buffer grows excessively.

        Returns:
            Trimmed item count per domain
        """
        trimmed = {}
        try:
            for domain in self._get_active_domains():
                key = f"{self._key_prefix}{{{domain}}}:buffer"
                size = self._redis.llen(key)

                if size > _SAFETY_LTRIM_THRESHOLD:
                    self._redis.ltrim(key, 0, _SAFETY_LTRIM_THRESHOLD - 1)
                    trimmed_count = size - _SAFETY_LTRIM_THRESHOLD

                    logger.warning(
                        f"[RedisAuditBuffer] Safety LTRIM: {domain}",  # noqa: G004
                        extra={
                            "domain": domain,
                            "original_size": size,
                            "trimmed_to": _SAFETY_LTRIM_THRESHOLD,
                            "dropped": trimmed_count,
                        },
                    )

                    trimmed[domain] = trimmed_count

                    with self._lock:
                        self._total_safety_ltrim += trimmed_count

                    # Record metrics
                    try:
                        from baldur.metrics.audit_buffer_metrics import (
                            audit_buffer_dropped_total,
                        )

                        audit_buffer_dropped_total.labels(domain=domain).inc(
                            trimmed_count
                        )
                    except ImportError:
                        pass

        except Exception as e:
            logger.exception(
                "redis_audit_buffer.safety_ltrim_failed",
                error=e,
            )

        return trimmed

    def flush_to_external_safe(
        self,
        target_adapter: AuditLogAdapterProtocol,
        batch_size: int = 500,
        domain: str | None = None,
    ) -> int:
        """
        Safe flush using the Processing Queue pattern.

        Transfers to external storage preserving order, without data loss.

        Args:
            target_adapter: Target adapter
            batch_size: Batch size
            domain: Restrict to one domain (None = all)

        Returns:
            Number of flushed entries
        """
        total_flushed = 0
        lua_scripts = self._get_lua_scripts()

        domains_to_process = [domain] if domain else self._get_active_domains()

        for current_domain in domains_to_process:
            try:
                # 1. Atomic move: buffer → processing queue
                moved = lua_scripts.atomic_batch_move(
                    domain=current_domain,
                    batch_size=batch_size,
                    worker_id=self._worker_id,
                )

                if moved == 0:
                    continue

                # 2. Read the data from the processing queue
                processing_key = f"{self._key_prefix}{{{current_domain}}}:processing"
                items = self._redis.lrange(processing_key, 0, moved - 1)

                entries = []
                for item in items:
                    try:
                        payload = fast_loads(item)
                        entries.append(payload.get("entry", payload))
                    except (ValueError, AttributeError):
                        entries.append(item)

                # 3. Store via the external adapter
                if hasattr(target_adapter, "log_batch"):
                    target_adapter.log_batch(entries)
                else:
                    for entry in entries:
                        if hasattr(target_adapter, "log_raw"):
                            target_adapter.log_raw(entry)
                        else:
                            target_adapter.log(entry)

                # 4. Clean up the processing queue on success
                lua_scripts.atomic_batch_complete(current_domain, moved)
                total_flushed += moved

                logger.debug(
                    "redis_audit_buffer.flushed_entries",
                    moved=moved,
                    current_domain=current_domain,
                )

            except Exception as e:
                logger.exception(
                    "redis_audit_buffer.flush_failed",
                    current_domain=current_domain,
                    error=e,
                )

                # 5. On failure, restore preserving order
                try:
                    restored = lua_scripts.atomic_batch_restore(current_domain)
                    logger.info(
                        "redis_audit_buffer.restored_items_buffer",
                        restored=restored,
                    )
                except Exception as restore_error:
                    logger.exception(
                        "redis_audit_buffer.restore_failed",
                        restore_error=restore_error,
                    )

        with self._lock:
            self._total_flushes += total_flushed

        return total_flushed

    def recover_orphaned_processing_queues(
        self,
        timeout_seconds: int = 300,
    ) -> int:
        """
        Recover timed-out orphaned processing queues.

        Args:
            timeout_seconds: Orphan threshold in seconds (default 5 minutes)

        Returns:
            Total number of recovered items
        """
        recovered_total = 0
        lua_scripts = self._get_lua_scripts()

        orphaned = lua_scripts.get_orphaned_processing_queues(timeout_seconds)

        for processing_key, worker_id, age in orphaned:
            logger.warning(
                "redis_audit_buffer.orphaned_queue_detected",
                processing_key=processing_key,
                worker_id=worker_id,
                age_seconds=age,
            )

            # Extract the domain from "audit:{domain}:processing"
            try:
                import re

                m = re.search(r"\{(.+?)\}", processing_key)
                domain = m.group(1) if m else processing_key.split(":")[-1]
                restored = lua_scripts.atomic_batch_restore(domain)
                recovered_total += restored
                logger.info(
                    "redis_audit_buffer.recovered_items",
                    restored=restored,
                    healing_domain=domain,
                )
            except Exception as e:
                logger.exception(
                    "watchdog.recovery_failed",
                    processing_key=processing_key,
                    error=e,
                )

        return recovered_total

    def _register_shutdown_hooks(self) -> None:
        """Register graceful-shutdown hooks (atexit + optional SIGTERM/SIGINT).

        Signal handlers CHAIN the previously installed disposition
        instead of replacing it: the fallback buffer is flushed first,
        then the prior handler runs, so the coordinator drain and a host
        server's own shutdown stay reachable. Skipped under gunicorn
        (master OR worker) — gunicorn's arbiter manages signal lifecycle
        and forwards SIGTERM to workers via ``worker_int``.
        ``is_under_gunicorn()`` is used instead of
        ``is_gunicorn_worker()`` because the latter relies on
        ``GUNICORN_WORKER=1`` which ``post_worker_init`` sets AFTER
        ``baldur.init()`` runs — in worker pre-post_worker_init, the
        handler would install before the env var exists. Atexit
        registration runs unconditionally so cleanup still fires from
        the gunicorn worker_exit path.
        """
        # Replace → chain conversion: 597 D7 (pattern precedent:
        # audit/persistence/disk_buffer_shutdown).
        if self._shutdown_registered:
            return

        try:
            atexit.register(self._graceful_shutdown)

            from baldur.core.process_utils import is_under_gunicorn

            if not is_under_gunicorn():
                # Windows may not support SIGTERM
                for sig_name in ("SIGTERM", "SIGINT"):
                    sig = getattr(signal, sig_name, None)
                    if sig is None:
                        continue
                    original = signal.getsignal(sig)
                    signal.signal(sig, self._make_chained_signal_handler(original))

            self._shutdown_registered = True
            logger.debug("redis_audit_buffer.shutdown_hooks_registered")
        except Exception as e:
            logger.debug(
                "redis_audit_buffer.register_shutdown_hooks",
                error=e,
            )

    def _make_chained_signal_handler(self, original: Any) -> Callable[[int, Any], None]:
        """Build a signal handler that flushes the fallback buffer and
        then invokes the previously installed handler."""

        def _chained_signal_handler(signum: int, frame: Any) -> None:
            logger.info(
                "redis_audit_buffer.received_signal",
                signum=signum,
            )
            self._graceful_shutdown()
            if callable(original):
                original(signum, frame)

        # Marker consumed by the coordinator's disposition chain-walk
        # (597 D2): classification follows it to the effective tail, so
        # this handler re-registering over the coordinator cannot flip
        # the chain/defer verdict.
        _chained_signal_handler._baldur_chained_original = original  # type: ignore[attr-defined]
        return _chained_signal_handler

    def _graceful_shutdown(self) -> None:
        """
        Graceful-shutdown step.

        Attempts to persist the in-memory fallback buffer to Redis.
        """
        # At atexit the logging stream may already be closed; suppress
        # "--- Logging error ---" output on logging failure. Save and restore
        # in finally (fix-356 contract, mirror of disk_buffer_shutdown): the
        # 597 D7 chained signal handler runs this at first signal delivery
        # while the process lives on through the drain window, so leaving
        # raiseExceptions=False would suppress logging-internal error reporting
        # process-wide for the remaining lifetime. The atexit path is
        # unaffected (restoring a flag in a dying process is harmless).
        original_raise = logging.raiseExceptions
        try:
            logging.raiseExceptions = False

            # In-memory buffer → Redis
            with self._fallback_lock:
                if self._fallback_buffer:
                    entries = list(self._fallback_buffer)

                    # Group by domain and batch-write
                    by_domain: dict[str, list] = {}
                    for item in entries:
                        domain = item.get("domain", "default")
                        if domain not in by_domain:
                            by_domain[domain] = []
                        by_domain[domain].append(item["entry"])

                    for domain, domain_entries in by_domain.items():
                        try:
                            # Call _log_batch_chunk directly (log_batch
                            # re-enters the fallback buffer)
                            self._log_batch_chunk(domain_entries, domain)
                        except Exception:
                            pass

                    self._fallback_buffer.clear()

        except Exception:
            pass
        finally:
            logging.raiseExceptions = original_raise

    def clear_domain(self, domain: str) -> int:
        """
        Delete all entries for a domain (test helper).

        Returns:
            Number of deleted entries
        """
        try:
            key = f"{self._key_prefix}{{{domain}}}:buffer"
            count = self._redis.llen(key)
            self._redis.delete(key)

            # Remove from the ActiveKeySet as well
            self._redis.srem(self.ACTIVE_DOMAINS_SET, domain)

            return count
        except Exception:
            return 0


# Factory
def create_redis_audit_buffer(
    redis_url: str,
    fallback_log_dir: str | None = None,
    **kwargs,
) -> RedisAuditBuffer | None:
    """
    RedisAuditBuffer factory.

    Args:
        redis_url: Redis URL (e.g. redis://localhost:6379)
        fallback_log_dir: Fallback log directory
        **kwargs: Extra RedisAuditBuffer arguments

    Returns:
        RedisAuditBuffer, or None when the Redis connection fails
    """
    try:
        from baldur.adapters.redis.connection_factory import (
            get_redis_connection_factory,
        )

        redis_client = get_redis_connection_factory().create(redis_url)
        redis_client.ping()  # Verify connectivity

        fallback = None
        if fallback_log_dir:
            try:
                from baldur.adapters.audit.file_adapter import FileAuditLogAdapter

                fallback_path = Path(fallback_log_dir) / "audit_fallback.jsonl"
                fallback = FileAuditLogAdapter(file_path=fallback_path)
            except ImportError:
                logger.debug("redis_audit_buffer.fileauditlogadapter_available")

        return RedisAuditBuffer(
            redis_client=redis_client,
            fallback_adapter=fallback,
            on_fallback=lambda e: logger.warning(
                "redis_audit_buffer.fallback_triggered", error=str(e)
            ),
            **kwargs,
        )

    except ImportError:
        logger.info("redis_audit_buffer.redis_package_installed")
        return None
    except Exception as e:
        logger.info(
            "redis_audit_buffer.redis_unavailable",
            error=e,
        )
        return None


# =============================================================================
# Process-lifetime drain-side accessor (D1)
#
# The periodic flush / orphan-recovery / safety-ltrim tasks acquire ONE shared
# buffer per worker process through this accessor instead of constructing a
# fresh buffer (and a fresh unclosed Redis client) on every run. State lives in
# a runtime-scoped holder with TTL negative caching (30 s, mirroring the
# adapters/redis/__init__.py _RedisClientState precedent) so a transient Redis
# outage at first access does not permanently disable the drain — a naive
# get_singleton(create_fn) would cache None forever via the _UNSET sentinel.
#
# enable_graceful_shutdown=False: the drain-side buffer never calls
# log()/log_batch(), so its in-memory _fallback_buffer can never fill and its
# atexit/signal hooks would protect nothing — registering zero hooks
# eliminates the per-run accumulation at the root. The default True is
# preserved for write-side / direct construction.
# =============================================================================

_REDIS_AUDIT_BUFFER_RETRY_INTERVAL: float = 30.0
_redis_audit_buffer_lock = threading.Lock()


class _RedisAuditBufferState:
    """Mutable drain-buffer state owned by the active ``BaldurRuntime``."""

    __slots__ = ("buffer", "unavailable", "fail_time")

    def __init__(self) -> None:
        self.buffer: RedisAuditBuffer | None = None
        self.unavailable: bool = False
        self.fail_time: float = 0.0


def _redis_audit_buffer_state() -> _RedisAuditBufferState:
    from baldur.runtime import get_runtime

    return get_runtime().get_singleton(
        "redis_audit_buffer_state", _RedisAuditBufferState
    )


def _build_drain_buffer() -> RedisAuditBuffer | None:
    """Construct the drain-side buffer via the ping-verified factory."""
    from baldur.settings.redis import get_redis_settings

    redis_url = get_redis_settings().url
    return create_redis_audit_buffer(redis_url, enable_graceful_shutdown=False)


def get_redis_audit_buffer() -> RedisAuditBuffer | None:
    """Return the process-lifetime drain-side ``RedisAuditBuffer``.

    Builds and caches one instance per worker process on first access. On a
    transient Redis failure the negative cache suppresses retries for
    ``_REDIS_AUDIT_BUFFER_RETRY_INTERVAL`` seconds, then a later call retries.

    Returns:
        The shared buffer, or ``None`` while Redis is unavailable.
    """
    state = _redis_audit_buffer_state()

    # Fast path: already built
    if state.buffer is not None:
        return state.buffer

    with _redis_audit_buffer_lock:
        # Double-check after acquiring the lock
        if state.buffer is not None:
            return state.buffer

        # Negative cache: suppress retries for the retry interval
        if state.unavailable:
            elapsed = time.monotonic() - state.fail_time
            if elapsed < _REDIS_AUDIT_BUFFER_RETRY_INTERVAL:
                return None
            # TTL expired — allow a retry
            state.unavailable = False

        buffer = _build_drain_buffer()
        if buffer is not None:
            state.buffer = buffer
            return buffer

        # Build failed — activate the negative cache
        state.unavailable = True
        state.fail_time = time.monotonic()
    return None


def reset_redis_audit_buffer() -> None:
    """Drop the cached drain-side buffer (test isolation + reconfiguration).

    Best-effort closes the underlying Redis client outside the runtime lock
    (factory contract: client close() is the caller's responsibility).
    """
    from baldur.runtime import get_runtime

    was_present, old_state = get_runtime().reset_singleton("redis_audit_buffer_state")
    if was_present and old_state is not None and old_state.buffer is not None:
        client = getattr(old_state.buffer, "_redis", None)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
