"""Unified idempotency gate for step-level deduplication.

Provides check-and-acquire, mark-completed, and mark-failed operations
using CacheProviderInterface.setnx() for atomic acquisition.

Used by Saga, Runbook, and other step-based execution engines.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

from baldur.core.exceptions import ConfigurationError

if TYPE_CHECKING:
    from baldur.interfaces.cache_provider import (
        AsyncCacheProviderInterface,
        CacheProviderInterface,
    )

__all__ = [
    "IdempotencyDecision",
    "IdempotencyCheckResult",
    "IdempotencyGate",
    "AsyncIdempotencyGate",
    "IDEMPOTENCY_DEFAULT_TTL_SECONDS",
]

logger = logging.getLogger(__name__)

IDEMPOTENCY_DEFAULT_TTL_SECONDS: int = 1800  # 30 minutes


class IdempotencyDecision(str, Enum):
    """Result of an idempotency check."""

    CONTINUE = "continue"  # Proceed with execution
    SKIP = "skip"  # Already completed, use cached result
    ABORT = "abort"  # Another process is executing (in-doubt)


@dataclass
class IdempotencyCheckResult:
    """Idempotency check result."""

    decision: IdempotencyDecision
    cached_result: dict[str, Any] | None = None
    retry_count: int = 0


class IdempotencyGate:
    """Step-level idempotency gate — unified model.

    Key generation is the caller's responsibility.
    The gate performs check + state transitions only.

    Uses CacheProviderInterface.setnx() for atomic check-and-acquire.

    A dedup record lives under two decoupled windows:

    - **Execution window** (``execution_ttl_seconds``) — how long an
      in-flight EXECUTING claim is honored before a competing process may
      stale-take it over. Sized to worst-case operation duration. Used by
      ``check_and_acquire`` when no per-call ``ttl`` is supplied.
    - **Memory window** (``memory_ttl_seconds``) — how long a
      completed/failed record is remembered for dedup. Used by
      ``mark_completed`` / ``mark_failed`` when no per-call ``ttl`` is
      supplied. ``None`` (default) resolves per use from
      ``IdempotencySettings.gate_memory_ttl_seconds`` so every construction
      site honors the operator-tunable setting without threading.
    """

    # Reference: 595 D2/D3/D5.

    def __init__(
        self,
        cache: CacheProviderInterface | None = None,
        execution_ttl_seconds: int = IDEMPOTENCY_DEFAULT_TTL_SECONDS,
        memory_ttl_seconds: int | None = None,
    ) -> None:
        self._cache = cache
        self._execution_ttl_seconds = execution_ttl_seconds
        self._memory_ttl_seconds = memory_ttl_seconds
        if cache is not None:
            # Validate the concrete adapter, not the metrics decorator.
            # Registry-resolved caches arrive wrapped in
            # ``MetricsAwareCacheAdapter`` (which overrides setnx/cas_dict_field
            # to delegate), so an unwrapped check would always pass and silently
            # admit a non-atomic underlying adapter.
            concrete = self._unwrap_cache(cache)
            self._validate_atomic_setnx(concrete)
            self._validate_atomic_cas_dict_field(concrete)
            self._validate_atomic_cas_takeover(concrete)

    @staticmethod
    def _unwrap_cache(cache: CacheProviderInterface) -> CacheProviderInterface:
        """Walk decorator delegates to the concrete adapter for capability checks.

        The atomicity validators below must inspect the concrete adapter rather
        than a delegating decorator: ``MetricsAwareCacheAdapter`` overrides
        ``setnx`` / ``cas_dict_field`` to forward to its delegate, so
        ``type(decorator).setnx is not CacheProviderInterface.setnx`` always
        holds regardless of whether the underlying adapter is atomic. Duck-typed
        on ``_delegate`` so this core module stays decoupled from the adapters
        layer (the metrics decorator is the only delegate-bearing wrapper today).
        """
        seen: set[int] = set()
        while hasattr(cache, "_delegate") and id(cache) not in seen:
            seen.add(id(cache))
            cache = cache._delegate
        return cache

    @staticmethod
    def _validate_atomic_setnx(cache: CacheProviderInterface) -> None:
        """Verify that the cache provides an atomic setnx() implementation.

        The base CacheProviderInterface.setnx() is a non-atomic
        exists() -> set() two-step. All production implementations
        (Redis, Memory) override with atomic versions, but a new
        implementation could silently inherit the non-atomic default.
        """
        from baldur.interfaces.cache_provider import CacheProviderInterface

        if type(cache).setnx is CacheProviderInterface.setnx:
            raise ConfigurationError(
                "IdempotencyGate requires an atomic setnx() implementation. "
                f"{type(cache).__name__} uses the non-atomic default."
            )

    @staticmethod
    def _validate_atomic_cas_dict_field(cache: CacheProviderInterface) -> None:
        """Verify that the cache provides an atomic cas_dict_field() implementation.

        Symmetric to _validate_atomic_setnx — mark_completed / mark_failed
        rely on cas_dict_field for single-RTT, race-free EXECUTING ->
        COMPLETED / FAILED transitions. The base interface default is a
        non-atomic get -> check -> set; production adapters (Redis Lua,
        Memory lock-wrapped) override.
        """
        from baldur.interfaces.cache_provider import CacheProviderInterface

        if type(cache).cas_dict_field is CacheProviderInterface.cas_dict_field:
            raise ConfigurationError(
                "IdempotencyGate requires an atomic cas_dict_field() "
                "implementation. "
                f"{type(cache).__name__} uses the non-atomic default."
            )

    @staticmethod
    def _validate_atomic_cas_takeover(cache: CacheProviderInterface) -> None:
        """Verify that the cache provides an atomic cas_takeover() implementation.

        Symmetric to the setnx / cas_dict_field validators — the retry-path
        takeover (failed / stale-executing) relies on cas_takeover for a
        single-RTT, single-winner re-acquire. The base interface default is a
        non-atomic get -> check -> set; production adapters (Redis Lua, Memory
        lock-wrapped) override. Runs after the setnx validator, so a non-atomic
        setnx adapter is rejected at the earlier check.
        """
        from baldur.interfaces.cache_provider import CacheProviderInterface

        if type(cache).cas_takeover is CacheProviderInterface.cas_takeover:
            raise ConfigurationError(
                "IdempotencyGate requires an atomic cas_takeover() "
                "implementation. "
                f"{type(cache).__name__} uses the non-atomic default."
            )

    def _stale_before(self, effective_ttl: timedelta) -> float:
        """App-computed staleness threshold with a clock-skew margin.

        A stored ``started_at`` (written with the app clock) is compared against
        ``now - execution_ttl - clock_skew_tolerance``. Subtracting the tolerance
        makes takeover conservative by that margin so a peer whose wall clock
        runs ahead cannot judge a still-running claim stale early and
        double-execute. Read per-call via the cached layered provider (mirroring
        ``_effective_memory_ttl``), so a console edit of the idempotency domain
        retunes it within the read-cache TTL (env + restart otherwise).
        """
        # Per-use lazy core->settings import — same acyclic precedent as
        # ``_effective_memory_ttl`` (no module-level settings import here).
        from baldur.settings.idempotency import IdempotencySettings
        from baldur.settings.layered_provider import get_layered_settings_cached

        tolerance = get_layered_settings_cached(
            IdempotencySettings, "idempotency"
        ).clock_skew_tolerance_seconds
        return time.time() - effective_ttl.total_seconds() - tolerance

    def _effective_memory_ttl(self) -> timedelta:
        """Resolve the dedup memory window for ``mark_*`` default paths.

        An explicit ``memory_ttl_seconds`` constructor override wins;
        otherwise the settings field is read per use (not at init) via the cached
        layered provider, so a console edit of the idempotency domain retunes the
        window within the read-cache TTL (env + restart otherwise).
        """
        # Per-use lazy core→settings import — the established precedent is
        # core/backoff.py; acyclic because this module has no module-level
        # settings import.
        if self._memory_ttl_seconds is not None:
            return timedelta(seconds=self._memory_ttl_seconds)
        from baldur.settings.idempotency import IdempotencySettings
        from baldur.settings.layered_provider import get_layered_settings_cached

        return timedelta(
            seconds=get_layered_settings_cached(
                IdempotencySettings, "idempotency"
            ).gate_memory_ttl_seconds
        )

    def check_and_acquire(
        self,
        key: str,
        ttl: timedelta | None = None,
    ) -> IdempotencyCheckResult:
        """Check idempotency and acquire EXECUTING state.

        The initial acquisition uses atomic setnx(). Retry paths
        (failed / stale-executing) use a single atomic cas_takeover() that
        rewrites the record only if it is still failed / stale, so exactly
        one competing process wins — losers receive ABORT. The atomicity is
        what closes the retry-path double-execute: a plain delete()+setnx()
        two-step could interleave and let two retries both re-acquire.

        ``ttl`` bounds the EXECUTING claim (execution window): the claim's
        cache TTL and the stale-takeover threshold. ``None`` uses the gate's
        execution default. Size it to worst-case operation duration, not to
        the dedup horizon — the completed-record memory window is governed
        separately by ``mark_completed`` / ``mark_failed``.

        Returns:
            CONTINUE — execution may proceed (EXECUTING state acquired)
            SKIP — already completed (cached_result included)
            ABORT — another process is executing (in-doubt window)
        """
        if self._cache is None:
            # Unconfigured / test no-op path. Deliberately un-metered: a
            # ``record_gate_decision("continue")`` here would conflate "no gate
            # installed" with "a real gate said continue" in the decision
            # counter. Metering happens only on the real-cache path below.
            return IdempotencyCheckResult(decision=IdempotencyDecision.CONTINUE)

        result = self._check_and_acquire(self._cache, key, ttl)
        self._record_gate_decision(result.decision)
        return result

    def _check_and_acquire(  # noqa: C901
        self,
        cache: CacheProviderInterface,
        key: str,
        ttl: timedelta | None,
    ) -> IdempotencyCheckResult:
        """Real-cache check-and-acquire (``cache`` guaranteed non-None)."""
        effective_ttl = ttl or timedelta(seconds=self._execution_ttl_seconds)
        record_value: dict[str, Any] = {
            "status": "executing",
            "started_at": time.time(),
            "retry_count": 0,
        }

        acquired = cache.setnx(key, record_value, ttl=effective_ttl)
        if acquired:
            return IdempotencyCheckResult(decision=IdempotencyDecision.CONTINUE)

        # Key already exists — check its status
        existing = cache.get(key)
        if existing is None:
            # Race: key expired between setnx and get — treat as CONTINUE
            retry_acquired = cache.setnx(key, record_value, ttl=effective_ttl)
            if retry_acquired:
                return IdempotencyCheckResult(decision=IdempotencyDecision.CONTINUE)
            return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)

        if not isinstance(existing, dict):
            return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)

        status = existing.get("status", "")

        if status == "completed":
            return IdempotencyCheckResult(
                decision=IdempotencyDecision.SKIP,
                cached_result=existing.get("result"),
                retry_count=existing.get("retry_count", 0),
            )

        if status == "failed":
            # Previous attempt failed — atomic cas_takeover for a safe retry.
            # Only one competing process wins the takeover; losers ABORT.
            record_value["retry_count"] = existing.get("retry_count", 0) + 1
            if cache.cas_takeover(
                key,
                record_value,
                stale_before=self._stale_before(effective_ttl),
                ttl=effective_ttl,
            ):
                self._record_takeover("failed")
                return IdempotencyCheckResult(
                    decision=IdempotencyDecision.CONTINUE,
                    retry_count=record_value["retry_count"],
                )
            return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)

        if status == "executing":
            # In-doubt: check if stale (TTL + clock-skew-based crash recovery).
            # Compute stale_before once and hand the SAME threshold to the
            # atomic op so the Python selector and the atomic guard agree.
            started_at = existing.get("started_at", 0)
            stale_before = self._stale_before(effective_ttl)
            if started_at < stale_before:
                # Stale — atomic cas_takeover for a safe retry.
                record_value["retry_count"] = existing.get("retry_count", 0) + 1
                if cache.cas_takeover(
                    key,
                    record_value,
                    stale_before=stale_before,
                    ttl=effective_ttl,
                ):
                    self._record_takeover("stale")
                    return IdempotencyCheckResult(
                        decision=IdempotencyDecision.CONTINUE,
                        retry_count=record_value["retry_count"],
                    )
                return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)
            return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)

        # Unknown status — abort defensively
        return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)

    @staticmethod
    def _record_gate_decision(decision: IdempotencyDecision) -> None:
        """Record the gate decision via the idempotency metric recorder.

        Lazy import + swallow-on-error, mirroring
        ``_cache_resolver._record_fallback_metric`` and the established
        core→metrics lazy-import precedent — the dedup hot path must never be
        broken by an observability failure.
        """
        try:
            from baldur.metrics.prometheus import get_metrics

            rec = getattr(get_metrics(), "idempotency", None)
            if rec is not None:
                rec.record_gate_decision(decision.value)
        except Exception:
            pass

    @staticmethod
    def _record_takeover(reason: str) -> None:
        """Record a won failed / stale takeover via the idempotency recorder.

        A takeover returns CONTINUE (indistinguishable from a fresh acquire on
        the decision counter alone), so it is metered separately —
        ``baldur_idempotency_gate_takeover_total{reason}``. A rising rate is the
        primary ``execution_ttl``-tuning signal (undersized window, or a worker
        that died silently mid-claim). Same lazy-import + swallow-on-error path
        as ``_record_gate_decision`` — observability must never break the dedup
        hot path.

        Args:
            reason: ``failed`` | ``stale`` — which takeover branch won.
        """
        try:
            from baldur.metrics.prometheus import get_metrics

            rec = getattr(get_metrics(), "idempotency", None)
            if rec is not None:
                rec.record_takeover(reason)
        except Exception:
            pass

    def mark_completed(
        self,
        key: str,
        result: dict[str, Any] | None = None,
        retry_count: int = 0,
        ttl: timedelta | None = None,
    ) -> None:
        """Transition EXECUTING -> COMPLETED. Cache the result.

        Atomically replaces the record only if its current status is
        ``executing``. ``retry_count`` is supplied by the caller (forwarded
        from ``IdempotencyCheckResult.retry_count``) so the success path
        does not re-read the record before writing.

        ``ttl`` bounds the dedup memory window — how long this completed
        record blocks duplicates. ``None`` uses the gate's memory default
        (``IdempotencySettings.gate_memory_ttl_seconds`` unless overridden
        at construction).
        """
        if self._cache is None:
            return
        effective_ttl = ttl or self._effective_memory_ttl()
        new_record = {
            "status": "completed",
            "completed_at": time.time(),
            "result": result or {},
            "retry_count": retry_count,
        }
        success = self._cache.cas_dict_field(
            key, "status", "executing", new_record, effective_ttl
        )
        if not success:
            logger.info(
                "idempotency_gate.mark_completed_cas_conflict",
                extra={"key": key},
            )

    def release(self, key: str) -> None:
        """Delete the record for ``key``, re-arming a future acquisition.

        Unlike :meth:`mark_completed` (which leaves a COMPLETED record that
        makes subsequent ``check_and_acquire`` calls SKIP), this clears the key
        entirely. Used when the same logical key must be re-acquirable later —
        e.g. recovery compensation that shares a ``trigger_id``-scoped key across
        resumed sessions and must re-run if a resumed session fails again.

        Idempotent and best-effort: a missing key or cache error is a no-op.
        """
        if self._cache is None:
            return
        try:
            self._cache.delete(key)
        except Exception:
            logger.info(
                "idempotency_gate.release_failed",
                extra={"key": key},
            )

    def mark_failed(
        self,
        key: str,
        error: str = "",
        retry_count: int = 0,
        ttl: timedelta | None = None,
    ) -> None:
        """Transition EXECUTING -> FAILED.

        Atomically replaces the record only if its current status is
        ``executing``. ``retry_count`` is supplied by the caller (forwarded
        from ``IdempotencyCheckResult.retry_count``) so the failure path
        does not re-read the record before writing.

        ``ttl`` bounds the dedup memory window for the failed record (the
        retryable-state retention). ``None`` uses the gate's memory default
        (``IdempotencySettings.gate_memory_ttl_seconds`` unless overridden
        at construction).
        """
        if self._cache is None:
            return
        effective_ttl = ttl or self._effective_memory_ttl()
        new_record = {
            "status": "failed",
            "failed_at": time.time(),
            "error": error,
            "retry_count": retry_count,
        }
        success = self._cache.cas_dict_field(
            key, "status", "executing", new_record, effective_ttl
        )
        if not success:
            logger.info(
                "idempotency_gate.mark_failed_cas_conflict",
                extra={"key": key},
            )


class AsyncIdempotencyGate:
    """Awaitable sibling of :class:`IdempotencyGate` for the async policy path.

    Mirrors ``IdempotencyGate`` exactly — same decisions, same TTL windows,
    same fail-closed strong consistency — but awaits an
    :class:`AsyncCacheProviderInterface` (``asetnx`` / ``aget`` /
    ``acas_dict_field`` / ``adelete``) instead of the sync
    ``CacheProviderInterface``. This is the Foundation-B consumer that removes
    671's ``to_thread`` offload for the one per-request loop-blocking dedup
    channel: the acquire is awaited inline (never ``create_task`` /
    ``wait_for``-timeout'd) so two concurrent requests cannot double-execute.
    """

    def __init__(
        self,
        cache: AsyncCacheProviderInterface | None = None,
        execution_ttl_seconds: int = IDEMPOTENCY_DEFAULT_TTL_SECONDS,
        memory_ttl_seconds: int | None = None,
    ) -> None:
        self._cache = cache
        self._execution_ttl_seconds = execution_ttl_seconds
        self._memory_ttl_seconds = memory_ttl_seconds
        if cache is not None:
            concrete = self._unwrap_cache(cache)
            self._validate_atomic_asetnx(concrete)
            self._validate_atomic_acas_dict_field(concrete)
            self._validate_atomic_acas_takeover(concrete)

    @staticmethod
    def _unwrap_cache(
        cache: AsyncCacheProviderInterface,
    ) -> AsyncCacheProviderInterface:
        """Walk decorator delegates to the concrete adapter for capability checks.

        Symmetric to :meth:`IdempotencyGate._unwrap_cache`. The async adapters
        ship unwrapped today (no async metrics decorator exists), but the walk
        keeps the validator honest if one is added later.
        """
        seen: set[int] = set()
        while hasattr(cache, "_delegate") and id(cache) not in seen:
            seen.add(id(cache))
            cache = cache._delegate
        return cache

    @staticmethod
    def _validate_atomic_asetnx(cache: AsyncCacheProviderInterface) -> None:
        """Verify the async cache overrides ``asetnx`` with an atomic impl.

        Async analog of ``IdempotencyGate._validate_atomic_setnx``: the base
        ``AsyncCacheProviderInterface.asetnx`` is a non-atomic (raising)
        placeholder; a subclass that inherits it would fail to dedup, so this
        rejects it at construction (fail-closed) before any request runs.
        """
        from baldur.interfaces.cache_provider import AsyncCacheProviderInterface

        if type(cache).asetnx is AsyncCacheProviderInterface.asetnx:
            raise ConfigurationError(
                "AsyncIdempotencyGate requires an atomic asetnx() implementation. "
                f"{type(cache).__name__} uses the non-atomic default."
            )

    @staticmethod
    def _validate_atomic_acas_dict_field(cache: AsyncCacheProviderInterface) -> None:
        """Verify the async cache overrides ``acas_dict_field`` atomically."""
        from baldur.interfaces.cache_provider import AsyncCacheProviderInterface

        if type(cache).acas_dict_field is AsyncCacheProviderInterface.acas_dict_field:
            raise ConfigurationError(
                "AsyncIdempotencyGate requires an atomic acas_dict_field() "
                "implementation. "
                f"{type(cache).__name__} uses the non-atomic default."
            )

    @staticmethod
    def _validate_atomic_acas_takeover(cache: AsyncCacheProviderInterface) -> None:
        """Verify the async cache overrides ``acas_takeover`` atomically.

        Async analog of ``IdempotencyGate._validate_atomic_cas_takeover``: the
        base ``AsyncCacheProviderInterface.acas_takeover`` is a raising
        placeholder, so a subclass that inherits it is rejected at construction
        (fail-closed) before any request runs.
        """
        from baldur.interfaces.cache_provider import AsyncCacheProviderInterface

        if type(cache).acas_takeover is AsyncCacheProviderInterface.acas_takeover:
            raise ConfigurationError(
                "AsyncIdempotencyGate requires an atomic acas_takeover() "
                "implementation. "
                f"{type(cache).__name__} uses the non-atomic default."
            )

    def _stale_before(self, effective_ttl: timedelta) -> float:
        """App-computed staleness threshold with a clock-skew margin.

        Async twin of :meth:`IdempotencyGate._stale_before` — same
        ``now - execution_ttl - clock_skew_tolerance`` formula, same per-call
        settings read.
        """
        from baldur.settings.idempotency import IdempotencySettings
        from baldur.settings.layered_provider import get_layered_settings_cached

        tolerance = get_layered_settings_cached(
            IdempotencySettings, "idempotency"
        ).clock_skew_tolerance_seconds
        return time.time() - effective_ttl.total_seconds() - tolerance

    def _effective_memory_ttl(self) -> timedelta:
        """Resolve the dedup memory window (constructor override else settings)."""
        if self._memory_ttl_seconds is not None:
            return timedelta(seconds=self._memory_ttl_seconds)
        from baldur.settings.idempotency import IdempotencySettings
        from baldur.settings.layered_provider import get_layered_settings_cached

        return timedelta(
            seconds=get_layered_settings_cached(
                IdempotencySettings, "idempotency"
            ).gate_memory_ttl_seconds
        )

    async def check_and_acquire(
        self,
        key: str,
        ttl: timedelta | None = None,
    ) -> IdempotencyCheckResult:
        """Check idempotency and acquire EXECUTING state (awaitable).

        Same three-way decision as the sync gate — CONTINUE / SKIP / ABORT —
        with the acquire awaited inline so concurrent duplicates cannot both
        proceed.
        """
        if self._cache is None:
            return IdempotencyCheckResult(decision=IdempotencyDecision.CONTINUE)

        result = await self._check_and_acquire(self._cache, key, ttl)
        IdempotencyGate._record_gate_decision(result.decision)
        return result

    async def _check_and_acquire(  # noqa: C901
        self,
        cache: AsyncCacheProviderInterface,
        key: str,
        ttl: timedelta | None,
    ) -> IdempotencyCheckResult:
        """Real-cache awaited check-and-acquire (``cache`` guaranteed non-None)."""
        effective_ttl = ttl or timedelta(seconds=self._execution_ttl_seconds)
        record_value: dict[str, Any] = {
            "status": "executing",
            "started_at": time.time(),
            "retry_count": 0,
        }

        acquired = await cache.asetnx(key, record_value, ttl=effective_ttl)
        if acquired:
            return IdempotencyCheckResult(decision=IdempotencyDecision.CONTINUE)

        existing = await cache.aget(key)
        if existing is None:
            retry_acquired = await cache.asetnx(key, record_value, ttl=effective_ttl)
            if retry_acquired:
                return IdempotencyCheckResult(decision=IdempotencyDecision.CONTINUE)
            return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)

        if not isinstance(existing, dict):
            return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)

        status = existing.get("status", "")

        if status == "completed":
            return IdempotencyCheckResult(
                decision=IdempotencyDecision.SKIP,
                cached_result=existing.get("result"),
                retry_count=existing.get("retry_count", 0),
            )

        if status == "failed":
            # Previous attempt failed — atomic acas_takeover for a safe retry.
            record_value["retry_count"] = existing.get("retry_count", 0) + 1
            if await cache.acas_takeover(
                key,
                record_value,
                stale_before=self._stale_before(effective_ttl),
                ttl=effective_ttl,
            ):
                IdempotencyGate._record_takeover("failed")
                return IdempotencyCheckResult(
                    decision=IdempotencyDecision.CONTINUE,
                    retry_count=record_value["retry_count"],
                )
            return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)

        if status == "executing":
            started_at = existing.get("started_at", 0)
            stale_before = self._stale_before(effective_ttl)
            if started_at < stale_before:
                record_value["retry_count"] = existing.get("retry_count", 0) + 1
                if await cache.acas_takeover(
                    key,
                    record_value,
                    stale_before=stale_before,
                    ttl=effective_ttl,
                ):
                    IdempotencyGate._record_takeover("stale")
                    return IdempotencyCheckResult(
                        decision=IdempotencyDecision.CONTINUE,
                        retry_count=record_value["retry_count"],
                    )
                return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)
            return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)

        return IdempotencyCheckResult(decision=IdempotencyDecision.ABORT)

    async def mark_completed(
        self,
        key: str,
        result: dict[str, Any] | None = None,
        retry_count: int = 0,
        ttl: timedelta | None = None,
    ) -> None:
        """Transition EXECUTING -> COMPLETED and cache the result (awaitable)."""
        if self._cache is None:
            return
        effective_ttl = ttl or self._effective_memory_ttl()
        new_record = {
            "status": "completed",
            "completed_at": time.time(),
            "result": result or {},
            "retry_count": retry_count,
        }
        success = await self._cache.acas_dict_field(
            key, "status", "executing", new_record, effective_ttl
        )
        if not success:
            logger.info(
                "idempotency_gate.mark_completed_cas_conflict",
                extra={"key": key},
            )

    async def release(self, key: str) -> None:
        """Delete the record for ``key``, re-arming a future acquisition.

        Awaitable sibling of :meth:`IdempotencyGate.release`. Idempotent and
        best-effort: a missing key or cache error is a no-op.
        """
        if self._cache is None:
            return
        try:
            await self._cache.adelete(key)
        except Exception:
            logger.info(
                "idempotency_gate.release_failed",
                extra={"key": key},
            )

    async def mark_failed(
        self,
        key: str,
        error: str = "",
        retry_count: int = 0,
        ttl: timedelta | None = None,
    ) -> None:
        """Transition EXECUTING -> FAILED (awaitable)."""
        if self._cache is None:
            return
        effective_ttl = ttl or self._effective_memory_ttl()
        new_record = {
            "status": "failed",
            "failed_at": time.time(),
            "error": error,
            "retry_count": retry_count,
        }
        success = await self._cache.acas_dict_field(
            key, "status", "executing", new_record, effective_ttl
        )
        if not success:
            logger.info(
                "idempotency_gate.mark_failed_cas_conflict",
                extra={"key": key},
            )


# ── Singleton ────────────────────────────────────────────────

from baldur.utils.singleton import make_singleton_factory

get_idempotency_gate, configure_idempotency_gate, reset_idempotency_gate = (
    make_singleton_factory("idempotency_gate", IdempotencyGate)
)
