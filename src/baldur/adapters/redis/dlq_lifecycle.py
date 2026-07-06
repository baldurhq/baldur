"""
Redis DLQ Lifecycle — state transitions and replay management.

Extracted from RedisDLQRepository for single-responsibility.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationStatus,
)
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.adapters.redis.dlq import RedisDLQRepository

logger = structlog.get_logger()

__all__ = ["RedisDLQLifecycle"]


class RedisDLQLifecycle:
    """DLQ item state transitions and replay management."""

    def __init__(self, repository: RedisDLQRepository) -> None:
        self._repo = repository

    def mark_as_resolved(
        self,
        id: str,
        resolution_type: str,
        resolution_note: str = "",
        resolved_by_id: int | None = None,
    ) -> bool:
        """Mark a failed operation as resolved."""
        return self._repo._update(
            entry_id=id,
            status=FailedOperationStatus.RESOLVED.value,
            resolution_type=resolution_type,
            resolution_note=resolution_note,
            resolved_at=utc_now(),
            resolved_by_id=resolved_by_id,
        )

    def mark_rejected(
        self,
        id: str,
        reason: str = "",
        rejected_by_id: int | None = None,
    ) -> bool:
        """Mark entry as rejected."""
        return self._repo._update(
            entry_id=id,
            status=FailedOperationStatus.REJECTED.value,
            resolution_type="rejected",
            resolution_note=reason,
            resolved_at=utc_now(),
            resolved_by_id=rejected_by_id,
        )

    def increment_retry_count(self, id: str) -> bool:
        """Increment retry count and update last_retry_at.

        Returns the actual result of the underlying ``_update`` write — True
        when Redis confirms the update, False when the entry is missing or
        the write failed. Callers (e.g. DLQ replay) rely on this to detect
        Redis-write failures rather than report false success.
        """
        data = self._repo._decode_entry(self._repo._load_blob(id))
        if not data:
            return False

        try:
            current_count = int(data.get("retry_count", 0))
        except (ValueError, TypeError):
            current_count = 0
        new_count = current_count + 1

        try:
            max_retries = int(data.get("max_retries", 2))
        except (ValueError, TypeError):
            max_retries = 2
        status = (
            FailedOperationStatus.REQUIRES_REVIEW.value
            if new_count >= max_retries
            else None
        )

        return self._repo._update(
            entry_id=id,
            retry_count=new_count,
            last_retry_at=utc_now(),
            status=status,
        )

    def try_acquire_for_replay(
        self,
        id: str,
        max_retries: int,
        force: bool = False,
    ) -> FailedOperationData | None:
        """Acquire a DLQ entry for replay atomically.

        Uses Redis WATCH/MULTI/EXEC on the entry key when available so the
        check-and-acquire is race-free across concurrent workers. Falls
        back to a non-atomic Python read-modify-write when the raw Redis
        client is unavailable (degraded mode / in-memory backends).

        ``force=True`` is the operator cap-override: it accepts a
        {PENDING, REQUIRES_REVIEW} source, derives the ZREM source-status
        index from the entry's actual status (so a REQUIRES_REVIEW → REPLAYING
        move clears the right per-status / composite index), resets retry_count
        to a fresh budget, and stamps the metadata history scar. See
        ``FailedOperationRepository.try_acquire_for_replay``.
        """
        start = time.monotonic()
        domain = "unknown"
        try:
            return self._try_acquire_atomic(
                id, max_retries, domain_out := [], force=force
            )
        finally:
            if domain_out:
                domain = domain_out[0]
            duration = time.monotonic() - start
            try:
                from baldur.metrics.prometheus import get_metrics

                metrics = get_metrics()
                if metrics and hasattr(metrics, "dlq"):
                    metrics.dlq.record_acquire_duration(domain, duration)
            except Exception:
                pass

    # Force-redrive accepts an at-cap entry parked in REQUIRES_REVIEW in
    # addition to the normal PENDING source.
    _FORCE_ACQUIRABLE = frozenset(
        {
            FailedOperationStatus.PENDING.value,
            FailedOperationStatus.REQUIRES_REVIEW.value,
        }
    )

    def _full_source_status_key(self, source_status: str):
        """Backend-prefixed per-status ZSET key for the entry's source status.

        PENDING lives in its own dedicated PENDING_KEY; every other status
        (e.g. REQUIRES_REVIEW under force) lives in ``dlq:status:{status}``.
        """
        backend = self._repo._backend
        if source_status == FailedOperationStatus.PENDING.value:
            return backend._get_full_key(self._repo.PENDING_KEY)
        return backend._get_full_key(self._repo._status_key(source_status))

    def _try_acquire_atomic(  # noqa: C901, PLR0912, PLR0915
        self,
        id: str,
        max_retries: int,
        domain_out: list,
        force: bool = False,
    ) -> FailedOperationData | None:
        """Atomic check-and-acquire via Redis WATCH/MULTI/EXEC.

        State machine inside the watched section:
            1. GET entry blob; absent → NOT_FOUND
            2. decode + check status acceptable → STATUS_MISMATCH
            3. (normal only) retry_count < max_retries → MAX_RETRIES_EXCEEDED
            4. transition to REPLAYING + retry_count (++ or reset) + last_retry_at
            5. MULTI: ZREM source-status index + SET re-encoded blob +
               ZADD REPLAYING + EXEC
            6. EXEC returns None on WATCH conflict → retry

        ``force=True`` accepts a {PENDING, REQUIRES_REVIEW} source, derives the
        ZREM source-status index from the entry's actual status, resets
        retry_count to a fresh budget, and stamps the metadata history scar.
        The normal PENDING path is behaviour-identical (the source-status index
        resolves to PENDING_KEY and the force-only branches are unreachable).
        """
        if not self._repo._ensure_redis_available():
            return self._try_acquire_python(id, max_retries, domain_out, force=force)

        raw_client = self._repo._raw_redis_client
        if raw_client is None:
            return self._try_acquire_python(id, max_retries, domain_out, force=force)

        backend = self._repo._backend
        full_entry_key = backend._get_full_key(self._repo._make_key(id))
        # 541 D6: this atomic block is REPLAYING's sole normal-mode writer (it
        # bypasses _update's index maintenance), so it must index REPLAYING
        # itself. Backend-prefixed full key for the raw pipe.
        full_replaying_key = backend._get_full_key(
            self._repo._status_key(FailedOperationStatus.REPLAYING.value)
        )
        # 544 D4: composite-key maintenance carries inside the same
        # WATCH/MULTI block alongside zrem source + zadd REPLAYING + set
        # blob, so the source→REPLAYING transition is atomic on both
        # the per-status and composite indexes. Full keys are computed
        # inside the loop once ``domain`` + source status are read.

        # WATCH conflict retry — bounded so a hot key under contention
        # cannot spin forever; falls through to None on exhaustion.
        max_attempts = 5
        for _ in range(max_attempts):
            try:
                with raw_client.pipeline(transaction=True) as pipe:
                    pipe.watch(full_entry_key)

                    blob = pipe.get(full_entry_key)
                    data = self._repo._decode_entry(blob)
                    if not data:
                        pipe.unwatch()
                        return None

                    if data.get("domain") and not domain_out:
                        domain_out.append(data["domain"])

                    source_status = data.get("status")
                    if force:
                        if source_status not in self._FORCE_ACQUIRABLE:
                            pipe.unwatch()
                            return None
                    elif source_status != FailedOperationStatus.PENDING.value:
                        pipe.unwatch()
                        return None

                    try:
                        current_retry = int(data.get("retry_count", 0))
                    except (ValueError, TypeError):
                        current_retry = 0
                    if not force and current_retry >= max_retries:
                        pipe.unwatch()
                        return None

                    now = utc_now()
                    data["status"] = FailedOperationStatus.REPLAYING.value
                    if force:
                        # D3/G5: stamp history then grant a fresh budget
                        # (retry_count == 1 == this redrive attempt).
                        metadata = data.get("metadata")
                        if not isinstance(metadata, dict):
                            metadata = {}
                        metadata["previous_total_retries"] = (
                            metadata.get("previous_total_retries", 0) + current_retry
                        )
                        metadata["force_redrive_count"] = (
                            metadata.get("force_redrive_count", 0) + 1
                        )
                        data["metadata"] = metadata
                        data["retry_count"] = 1
                    else:
                        data["retry_count"] = current_retry + 1
                    data["last_retry_at"] = now.isoformat()
                    data["updated_at"] = now.isoformat()

                    new_blob = self._repo._encode_entry(data)

                    # Score the REPLAYING index by created_at epoch (matching
                    # the create / _update convention), not the transition
                    # time, so per-status find stays created_at-ordered.
                    created_raw = data.get("created_at")
                    try:
                        replaying_score = (
                            datetime.fromisoformat(created_raw).timestamp()
                            if created_raw
                            else time.time()
                        )
                    except (ValueError, TypeError):
                        replaying_score = time.time()

                    # Source-status index (PENDING_KEY for the normal path,
                    # dlq:status:{status} for a force-from-REQUIRES_REVIEW move).
                    full_source_status_key = self._full_source_status_key(source_status)

                    # 544 D4: composite-key full keys are derived from
                    # the entry's domain (already captured via
                    # ``domain_out`` above). When ``domain`` is missing
                    # (degenerate entry), the composite ops are skipped
                    # to avoid creating a ``:`` -suffixed registry-like
                    # key; the per-status REPLAYING index still picks
                    # the entry up.
                    domain = data.get("domain", "")
                    full_composite_source = (
                        backend._get_full_key(
                            self._repo._status_domain_key(source_status, domain)
                        )
                        if domain
                        else None
                    )
                    full_composite_replaying = (
                        backend._get_full_key(
                            self._repo._status_domain_key(
                                FailedOperationStatus.REPLAYING.value, domain
                            )
                        )
                        if domain
                        else None
                    )

                    pipe.multi()
                    pipe.zrem(full_source_status_key, str(id))
                    pipe.set(full_entry_key, new_blob)
                    pipe.zadd(full_replaying_key, {str(id): replaying_score})
                    if full_composite_source is not None:
                        pipe.zrem(full_composite_source, str(id))
                    if full_composite_replaying is not None:
                        pipe.zadd(full_composite_replaying, {str(id): replaying_score})
                    result = pipe.execute()

                    if result is None:
                        # Defensive: a nil EXEC reply. redis-py normally raises
                        # WatchError on a watched-key change (handled below), so
                        # this guard rarely fires — kept belt-and-suspenders.
                        continue

                    return self._repo._to_data(data)

            except Exception as exc:
                # Classify per the prior Lua-path policy. A WATCH conflict
                # surfaces as a redis-py WatchError (NOT a None EXEC reply), so
                # it is the bounded-retry signal — re-read the now-mutated entry
                # on the next iteration (a concurrent winner makes the loser
                # return None at the status/cap gate). Connection/timeout errors
                # degrade to the Python fallback; anything else (e.g.
                # ResponseError) propagates so the caller learns.
                try:
                    import redis as redis_lib

                    if isinstance(exc, redis_lib.WatchError):
                        continue
                    if isinstance(
                        exc, (redis_lib.ConnectionError, redis_lib.TimeoutError)
                    ):
                        logger.warning(
                            "dlq.acquire_degraded",
                            entry_id=id,
                            error=str(exc),
                        )
                        return self._try_acquire_python(
                            id, max_retries, domain_out, force=force
                        )
                except ImportError:
                    pass
                raise

        logger.debug("dlq.acquire_watch_exhausted", entry_id=id)
        return None

    def _try_acquire_python(
        self,
        id: str,
        max_retries: int,
        domain_out: list,
        force: bool = False,
    ) -> FailedOperationData | None:
        """Non-atomic Python read-modify-write for degraded mode.

        Used when Redis is unreachable so the in-memory backend keeps
        operating. Single-process degraded mode does not need WATCH —
        the only concurrency comes from this worker's threads, and
        ``_update`` is the sole writer for the entry blob.

        ``force=True`` mirrors the atomic block: accept {PENDING,
        REQUIRES_REVIEW}, reset to a fresh budget, stamp the metadata scar.
        """
        entry = self._repo.get_by_id(id)
        if not entry:
            return None

        if not domain_out:
            domain_out.append(entry.domain)

        if force:
            if entry.status not in self._FORCE_ACQUIRABLE:
                return None
        else:
            if entry.status != FailedOperationStatus.PENDING.value:
                return None
            if entry.retry_count >= max_retries:
                return None

        now = utc_now()
        if force:
            metadata = dict(entry.metadata or {})
            metadata["previous_total_retries"] = (
                metadata.get("previous_total_retries", 0) + entry.retry_count
            )
            metadata["force_redrive_count"] = metadata.get("force_redrive_count", 0) + 1
            success = self._repo._update(
                entry_id=id,
                status=FailedOperationStatus.REPLAYING.value,
                retry_count=1,
                last_retry_at=now,
                metadata=metadata,
            )
        else:
            success = self._repo._update(
                entry_id=id,
                status=FailedOperationStatus.REPLAYING.value,
                retry_count=entry.retry_count + 1,
                last_retry_at=now,
            )

        if success:
            return self._repo.get_by_id(id)

        return None

    def complete_replay(
        self,
        id: str,
        success: bool,
        resolution_type: str = "",
        note: str = "",
        resolved_by_id: int | None = None,
        error_details: dict | None = None,
    ) -> bool:
        """Complete a replay operation by updating the final status."""
        if success:
            # Fall back to the entry's existing resolution_type (memory-adapter
            # parity), never a fabricated blanket value — the service layer
            # always supplies the provenance-specific trigger on success.
            resolved_entry = self._repo.get_by_id(id)
            prior = resolved_entry.resolution_type if resolved_entry else ""
            return self.mark_as_resolved(
                id=id,
                resolution_type=resolution_type or prior,
                resolution_note=note,
                resolved_by_id=resolved_by_id,
            )
        entry = self._repo.get_by_id(id)
        if entry and entry.retry_count >= entry.max_retries:
            return self._repo._update(
                entry_id=id,
                status=FailedOperationStatus.REQUIRES_REVIEW.value,
                resolution_note=note,
                metadata=error_details,
            )
        return self._repo._update(
            entry_id=id,
            status=FailedOperationStatus.PENDING.value,
            resolution_note=note,
            metadata=error_details,
        )

    def release_stale_replaying(self, older_than_minutes: int = 30) -> int:
        """Release DLQ entries stuck in REPLAYING state.

        Returns the count of entries actually released — i.e. entries for
        which the underlying ``_update`` write succeeded. Failed writes are
        excluded so the count reflects real state changes, not attempts.
        Mirrors the ``bulk_update_status`` accounting pattern below.
        """
        replaying = self._repo.query.by_status(
            FailedOperationStatus.REPLAYING.value, limit=1000
        )

        released = 0
        cutoff = utc_now() - timedelta(minutes=older_than_minutes)

        for entry in replaying:
            if (
                entry.updated_at
                and entry.updated_at < cutoff
                and self._repo._update(
                    entry_id=entry.id,
                    status=FailedOperationStatus.PENDING.value,
                    metadata={"released_from_stale": True},
                )
            ):
                released += 1

        return released

    def bulk_update_status(self, ids: list[str], status: str) -> int:
        """Bulk update status for multiple operations."""
        updated_count = 0
        for id in ids:
            if self._repo.update_status(id, status):
                updated_count += 1
        return updated_count
