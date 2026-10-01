"""Idempotency Guard and Hook for PolicyComposer.

Two-phase idempotency enforcement:
- IdempotencyGuard (Phase 1): Pre-execution check+acquire via IdempotencyGate;
  on CONTINUE it opens the call's work scope (``baldur.core.abandoned_work``)
- IdempotencyHook (Phase 2): Post-execution mark. The function returned ->
  completed now. Otherwise the key follows the work the call abandoned: it
  stays held (a repeat reads ABORT) while any recorded work still runs, then
  is marked completed if the call ended on a timeout and its own timed-out
  work finished successfully, else failed (re-claimable). Work cancelled
  before it started, and an async timeout (which cancels the coroutine),
  leave nothing running, so the key is released at once.

Per-call record via context.extra: the guard writes the key, the per-call
retry count, the dedup memory window, the claim id and the work scope
(``_idempotency_key`` / ``_idempotency_retry_count`` / ``_idempotency_ttl`` /
``_idempotency_claim_id`` / ``_idempotency_scope``); the hook reads them when
the call ends, and a mark made later carries the values read then. Give each
keyed call its own ``PolicyContext``: calls that share one overwrite each
other's record.

Fail behavior:
- A gate *decision* of SKIP (already completed) or ABORT (a concurrent
  in-flight duplicate) is fail-CLOSED — the guard rejects so the side effect
  does not run twice, mirroring the ``@idempotent`` decorator's shared decision
  contract.
- A cache *I/O exception* during the check is fail-CLOSED by default (an
  explicit ``idempotency_key=`` is a "must not duplicate" signal, so a transient
  blip must not let a duplicate through); opt into fail-open via
  ``IdempotencySettings.fail_open_on_cache_error`` or the per-call
  ``fail_open`` override.
- The post-execution mark (hook) stays fail-open: a transient mark failure is
  logged but never blocks the already-completed call.

The cache-backed gate is resolved once (memoized) via the same ProviderRegistry
path the ``@idempotent`` decorator uses, so the guard/hook dedup against the
registered distributed cache (or a shared in-process fallback when none is
registered) instead of the bare ``cache=None`` singleton, which would never
block a duplicate. In production, a registry holding only Baldur's in-process
default counts as none registered, so a refused resolution memoizes nothing and
the first call after a shared cache is wired resolves it.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.adapters.cache.async_memory_adapter import AsyncInMemoryCacheAdapter
from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.core.abandoned_work import (
    WorkScope,
    WorkSummary,
    close_work_scope,
    open_work_scope,
)
from baldur.core.exceptions import TimeoutPolicyError
from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyOutcome,
    PolicyResult,
)

if TYPE_CHECKING:
    from contextvars import Token

    from baldur.core.idempotency_gate import AsyncIdempotencyGate, IdempotencyGate
    from baldur.interfaces.resilience_policy import PolicyContext

logger = structlog.get_logger()

__all__ = [
    "IdempotencyGuard",
    "IdempotencyHook",
    "AsyncIdempotencyGuard",
    "AsyncIdempotencyHook",
]

# Single source for the guard's ``name`` (D8). The facade's reject-mapping
# (protect_facade._finalize_value) imports and compares against this constant
# instead of re-typing the bare literal, so a guard rename cannot silently route
# idempotency rejects to the defensive fallback.
_GUARD_NAME = "idempotency"


# Module-level fallback cache used when ProviderRegistry has no cache adapter
# registered (single-process / OSS deployments). Distinct ``key_prefix`` from
# the decorator's ``_FALLBACK_CACHE`` and the service layer's so the three
# layers cannot collide on keys when all run in-process in a single worker.
_POLICY_FALLBACK_CACHE = InMemoryCacheAdapter(key_prefix="idempotency_policy:")

# Lazily-built, memoized cache-backed gate shared by the guard's Phase-1
# acquire and the hook's Phase-2 mark so both observe one cache. Lock-free —
# mirrors the decorator's per-wrapper ``gate_state`` rationale (the race is
# benign; the same gate would be built twice at worst, never inconsistently).
_policy_gate_state: dict[str, Any] = {"initialized": False, "gate": None}

# Async twins of the two above — the awaitable dedup gate for the async facade
# path (aprotect/aprotected). The async fallback is a SEPARATE in-process store
# from the sync one, so on the no-Redis single-process path a duplicate
# protected via BOTH sync protect() and async aprotect() on one key may run
# twice. This is production-unreachable (four conjunctive conditions: no Redis +
# single process + same key + one op via both facades); with Redis registered
# both facades hit the SAME Redis keys (async resolver reuses the sync key
# prefix), so they are fully cross-consistent, and production fail-closes
# without the explicit BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK escape hatch
# (whose "in-process-only" contract already covers this). Workaround: register
# Redis.
_ASYNC_POLICY_FALLBACK_CACHE = AsyncInMemoryCacheAdapter(
    key_prefix="idempotency_policy:"
)
_async_policy_gate_state: dict[str, Any] = {"initialized": False, "gate": None}

# Late async marks scheduled on a caller's loop (in-process async ledger only),
# held until done so the loop's weak task references cannot drop them.
_DEFERRED_MARK_TASKS: set[asyncio.Task[None]] = set()


def _ensure_policy_gate() -> IdempotencyGate:
    """Return the memoized cache-backed ``IdempotencyGate`` for the policy layer.

    Builds the gate once from a ProviderRegistry-resolved cache (or the shared
    in-process fallback when no adapter is registered), reusing the decorator's
    proven resolver path. In production with no distributed cache adapter
    registered (none, or only Baldur's in-process default) and the escape hatch
    off, :func:`resolve_cache_via_registry` raises ``ConfigurationError``
    (fail-closed) and nothing is memoized.
    """
    if not _policy_gate_state["initialized"]:
        from baldur.core.idempotency_gate import IdempotencyGate
        from baldur.services.idempotency._cache_resolver import (
            resolve_cache_via_registry,
        )

        cache = resolve_cache_via_registry(
            layer="policy",
            fallback_cache=_POLICY_FALLBACK_CACHE,
            raise_on_prod_no_toggle=True,
        )
        _policy_gate_state["gate"] = IdempotencyGate(cache=cache)
        _policy_gate_state["initialized"] = True
    return _policy_gate_state["gate"]


def _ensure_async_policy_gate() -> AsyncIdempotencyGate:
    """Return the memoized async cache-backed ``AsyncIdempotencyGate``.

    Async sibling of :func:`_ensure_policy_gate`. Resolves the async cache via
    :func:`resolve_async_cache` — which reuses the sync resolver's
    production-fail-closed decision, then selects an ``AsyncRedisCacheAdapter``
    (Redis registered) or the async in-process fallback. In production with no
    distributed cache adapter the async path can share (none, the in-process
    default, or a non-Redis one) and the escape hatch off, it raises
    ``ConfigurationError`` here (fail-closed) — the same posture as the sync
    gate — and nothing is memoized.
    """
    if not _async_policy_gate_state["initialized"]:
        from baldur.core.idempotency_gate import AsyncIdempotencyGate
        from baldur.services.idempotency._cache_resolver import resolve_async_cache

        cache = resolve_async_cache(
            layer="policy",
            sync_fallback_cache=_POLICY_FALLBACK_CACHE,
            async_fallback_cache=_ASYNC_POLICY_FALLBACK_CACHE,
            raise_on_prod_no_toggle=True,
        )
        _async_policy_gate_state["gate"] = AsyncIdempotencyGate(cache=cache)
        _async_policy_gate_state["initialized"] = True
    return _async_policy_gate_state["gate"]


def _reset_policy_gate() -> None:
    """Test helper — clear the memoized gate, replace the fallback cache, and
    clear the shared resolver's one-shot WARN guard.

    Replacing ``_POLICY_FALLBACK_CACHE`` (rather than only clearing the gate)
    ensures prior-test dedup state cannot leak into the next test — mirroring
    the decorator's ``_reset_fallback_cache``. Wired into
    ``reset_protect_caches()`` so settings/cache resets between tests invalidate
    the policy gate too.
    """
    from baldur.services.idempotency._cache_resolver import _reset_warned_layers

    global _POLICY_FALLBACK_CACHE, _ASYNC_POLICY_FALLBACK_CACHE
    _POLICY_FALLBACK_CACHE = InMemoryCacheAdapter(key_prefix="idempotency_policy:")
    _ASYNC_POLICY_FALLBACK_CACHE = AsyncInMemoryCacheAdapter(
        key_prefix="idempotency_policy:"
    )
    _policy_gate_state["initialized"] = False
    _policy_gate_state["gate"] = None
    _async_policy_gate_state["initialized"] = False
    _async_policy_gate_state["gate"] = None
    _reset_warned_layers()


@dataclass(frozen=True)
class _KeyedCall:
    """One keyed call's record, read from ``context.extra`` when it ends.

    A mark made later carries these values and never re-reads the slots, so a
    later keyed call that reuses the ``PolicyContext`` cannot redirect it.
    """

    key: str
    retry_count: int
    ttl: timedelta | None
    claim_id: str | None
    scope: WorkScope | None
    token: Token[WorkScope | None] | None


def _write_keyed_call(
    context: PolicyContext,
    key: str,
    retry_count: int,
    ttl: timedelta | None,
    claim_id: str | None,
) -> None:
    """Guard CONTINUE: write the per-call record and open the work scope."""
    context.extra["_idempotency_key"] = key
    context.extra["_idempotency_retry_count"] = retry_count
    context.extra["_idempotency_ttl"] = ttl
    context.extra["_idempotency_claim_id"] = claim_id
    context.extra["_idempotency_scope"] = open_work_scope(origin=context)


def _read_keyed_call(context: PolicyContext | None) -> _KeyedCall | None:
    """Read the per-call record the guard wrote; None when the call is unkeyed."""
    if context is None:
        return None
    extra = context.extra or {}
    key = extra.get("_idempotency_key")
    if not key:
        return None
    scope_slot = extra.get("_idempotency_scope")
    scope, token = scope_slot if scope_slot is not None else (None, None)
    return _KeyedCall(
        key=key,
        retry_count=extra.get("_idempotency_retry_count", 0),
        ttl=extra.get("_idempotency_ttl"),
        claim_id=extra.get("_idempotency_claim_id"),
        scope=scope,
        token=token,
    )


def _settled_as_completed(timed_out: bool, summary: WorkSummary) -> bool:
    """The key rule for a call that did not return.

    Completed only when the call ended on a timeout and its own timed-out work
    finished successfully; any other work only extends the hold.
    """
    return timed_out and summary.own_succeeded


def _close_call_scope(
    call: _KeyedCall,
    on_settled: Callable[[WorkSummary], None] | None,
) -> WorkSummary | None:
    """Close the call's work scope; the summary when nothing it holds runs."""
    if call.scope is None:
        return WorkSummary()
    return close_work_scope(call.scope, call.token, on_settled)


def _close_unsettled_call_scope(context: PolicyContext | None) -> None:
    """Close the work scope a keyed call left open; mark nothing.

    The hooks run only when the call ends by an ``Exception`` or a result. A
    call ended by a ``BaseException`` (cancelled from outside, a gevent
    timeout) leaves the scope its guard opened current in the caller's
    context, where a long-lived task or thread would chain one more scope per
    such exit. The claim is left as it is. A scope the hook already closed is
    untouched — it may still be holding for a late mark.
    """
    call = _read_keyed_call(context)
    if call is None or call.scope is None or call.scope.closed:
        return
    close_work_scope(call.scope, call.token)


def _timed_out_trigger(result: PolicyResult) -> bool:
    """True when a fallback answered a timeout in the function's place."""
    return result.metadata.get("fallback_trigger") == PolicyOutcome.TIMEOUT.value


def _async_ledger_is_process_local(gate: AsyncIdempotencyGate) -> bool:
    """True when the async gate dedups in this process's own memory.

    The shared (Redis) async ledger reuses the sync policy layer's keys, so a
    late mark can go through the sync gate and survive the loop's teardown;
    the in-process async ledger is reachable only from its own loop.
    """
    cache = gate._unwrap_cache(gate._cache) if gate._cache is not None else None
    return isinstance(cache, AsyncInMemoryCacheAdapter)


def _mark_sync(
    gate: IdempotencyGate, call: _KeyedCall, completed: bool, error: str
) -> None:
    if completed:
        gate.mark_completed(
            call.key, retry_count=call.retry_count, ttl=call.ttl, claim_id=call.claim_id
        )
    else:
        gate.mark_failed(
            call.key,
            error=error,
            retry_count=call.retry_count,
            ttl=call.ttl,
            claim_id=call.claim_id,
        )


async def _mark_async(
    gate: AsyncIdempotencyGate, call: _KeyedCall, completed: bool, error: str
) -> None:
    if completed:
        await gate.mark_completed(
            call.key, retry_count=call.retry_count, ttl=call.ttl, claim_id=call.claim_id
        )
    else:
        await gate.mark_failed(
            call.key,
            error=error,
            retry_count=call.retry_count,
            ttl=call.ttl,
            claim_id=call.claim_id,
        )


def _log_immediate_mark_failure(key: str, completed: bool, e: Exception) -> None:
    # Fail-open: the call's outcome has already been served.
    logger.warning(
        "idempotency.mark_completed_failed"
        if completed
        else "idempotency.mark_failed_failed",
        key=key,
        error=str(e),
        fail_open=True,
    )


def _log_deferred_mark_failure(key: str, e: Exception) -> None:
    logger.warning(
        "idempotency.deferred_mark_failed",
        key=key,
        error=str(e),
        error_type=type(e).__name__,
    )


def _log_mark_deferred(call: _KeyedCall) -> None:
    logger.info(
        "idempotency.mark_deferred",
        key=call.key,
        pieces=call.scope.running_count if call.scope is not None else 0,
    )


async def _run_deferred_async_mark(
    gate: AsyncIdempotencyGate, call: _KeyedCall, completed: bool, error: str
) -> None:
    try:
        await _mark_async(gate, call, completed, error)
    except Exception as e:
        _log_deferred_mark_failure(call.key, e)


def _spawn_deferred_async_mark(
    gate: AsyncIdempotencyGate, call: _KeyedCall, completed: bool, error: str
) -> None:
    """Run on the caller's loop: start the late mark and hold its task."""
    task = asyncio.ensure_future(_run_deferred_async_mark(gate, call, completed, error))
    _DEFERRED_MARK_TASKS.add(task)
    task.add_done_callback(_DEFERRED_MARK_TASKS.discard)


class IdempotencyGuard:
    """Pre-execution idempotency check guard.

    Phase 1: Checks whether the operation is already completed (SKIP) or being
    executed concurrently (ABORT) via IdempotencyGate. On a CONTINUE decision it
    stores the per-call record (key, claim id, windows) in context.extra and
    opens the call's work scope for IdempotencyHook to complete Phase 2; on
    SKIP/ABORT it rejects (fail-closed). A cache I/O error fails
    closed by default — opt into fail-open via ``fail_open`` /
    ``IdempotencySettings.fail_open_on_cache_error``.

    ``ttl`` is the dedup memory window (how long a completed/failed record
    blocks duplicates); the guard stores it in ``context.extra`` on CONTINUE
    so the hook's ``mark_*`` uses the same window — the guard is the single
    source, making a guard/hook window mismatch structurally impossible.
    ``execution_ttl`` is the in-flight execution window passed to
    ``check_and_acquire`` (claim TTL + stale-takeover bound). ``None`` for
    either defers to the gate defaults.
    """

    # Reference: 595 D4 — same
    # threading channel as _idempotency_key / _idempotency_retry_count.

    def __init__(
        self,
        key_generator: Callable[[PolicyContext], str],
        fail_open: bool | None = None,
        ttl: timedelta | None = None,
        execution_ttl: timedelta | None = None,
    ) -> None:
        # Cached layered read (686 D3/D5) so a console edit of the idempotency
        # domain is observed within the read-cache TTL; env base when no
        # RuntimeConfigManager is registered.
        from baldur.settings.idempotency import IdempotencySettings
        from baldur.settings.layered_provider import get_layered_settings_cached

        settings = get_layered_settings_cached(IdempotencySettings, "idempotency")
        self._globally_enabled = settings.enabled
        # Cache-error fail direction (D9). ``None`` consults the global posture;
        # an explicit per-call bool (threaded from the facade) overrides it.
        self._fail_open_on_cache_error = (
            settings.fail_open_on_cache_error if fail_open is None else fail_open
        )
        self._key_fn = key_generator
        self._ttl = ttl
        self._execution_ttl = execution_ttl
        # Resolve the cache-backed gate at construction so a production
        # misconfiguration (no distributed cache adapter + escape hatch off)
        # surfaces loudly here — propagating out of the facade's composer
        # build — rather than being swallowed by the fail-open ``check()``.
        # Idempotency is a correctness gate, not a side-effect, so it is
        # fail-closed in prod. Gated on ``enabled`` so a globally-disabled
        # feature never raises.
        if self._globally_enabled:
            _ensure_policy_gate()

    @property
    def name(self) -> str:
        return _GUARD_NAME

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        if context is None:
            return GuardResult(allowed=True)

        if not self._globally_enabled:
            return GuardResult(allowed=True)

        key = ""
        try:
            from baldur.core.idempotency_gate import IdempotencyDecision

            key = self._key_fn(context)
            gate = _ensure_policy_gate()
            result = gate.check_and_acquire(key, ttl=self._execution_ttl)
            if result.decision == IdempotencyDecision.SKIP:
                # Block: already completed. WARN reuses the decorator's exact
                # event name so one log query catches a block on either surface.
                logger.warning(
                    "idempotency.duplicate_blocked",
                    key=key,
                    decision="SKIP",
                )
                return GuardResult(
                    allowed=False,
                    reason=f"Already processed (idempotency key: {key})",
                    metadata={
                        "idempotency_decision": result.decision.name,
                        "idempotency_key": key,
                        "cached_result": result.cached_result,
                    },
                )
            if result.decision == IdempotencyDecision.ABORT:
                # Block: a concurrent process holds the key (in-doubt window).
                logger.warning(
                    "idempotency.execution_blocked",
                    key=key,
                    decision="ABORT",
                )
                return GuardResult(
                    allowed=False,
                    reason=f"Another process is executing (idempotency key: {key})",
                    metadata={
                        "idempotency_decision": result.decision.name,
                        "idempotency_key": key,
                    },
                )
            # CONTINUE — store the per-call record for the hook (the guard is
            # the single window source) and open the call's work scope, whose
            # own work is what this context's timeout stage records.
            _write_keyed_call(
                context, key, result.retry_count, self._ttl, result.claim_id
            )
            return GuardResult(allowed=True)
        except Exception as e:
            # Cache I/O fault (e.g. Redis down) or key-generation error. Log the
            # fail-open/closed decision so a silent degradation is observable.
            # Fail CLOSED by default to prevent a duplicate side effect on a
            # blip; opt-in fail-open trades that guarantee for availability.
            logger.warning(
                "idempotency.guard_check_failed",
                error=str(e),
                fail_open=self._fail_open_on_cache_error,
            )
            if self._fail_open_on_cache_error:
                return GuardResult(allowed=True)
            return GuardResult(
                allowed=False,
                reason="Idempotency check unavailable (cache error); failing closed.",
                metadata={
                    "idempotency_unavailable": True,
                    "idempotency_key": key,
                    "error": str(e),
                },
            )


class IdempotencyHook:
    """Post-execution idempotency mark hook (fail-open).

    Phase 2 marks the key through IdempotencyGate by one rule, shared with
    :class:`AsyncIdempotencyHook` and the ``@idempotent`` decorator:

    - The function returned -> completed now; a repeat is refused for the
      memory window.
    - Otherwise (the call raised, timed out or was refused, with or without a
      fallback answer) the key follows the work the call abandoned. While any
      recorded work still runs, the claim stays executing and a repeat reads
      ABORT. When nothing runs (at once, or when the last piece ends) the key
      is marked completed if the call ended on a timeout and its own timed-out
      work finished successfully, else failed — re-claimable by the next call.

    Every mark is scoped to the claim the guard took, so a late mark never
    lands on a later claim of the same key. A late mark runs on the thread
    that finished the last piece, in a copy of the caller's context.
    """

    def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        call = _read_keyed_call(context)
        if call is None:
            return
        if result.outcome == PolicyOutcome.SUCCESS:
            _close_call_scope(call, None)
            try:
                _mark_sync(_ensure_policy_gate(), call, True, "")
            except Exception as e:
                _log_immediate_mark_failure(call.key, True, e)
            return
        self._settle(
            call,
            timed_out=_timed_out_trigger(result),
            error=str(result.metadata.get("original_error", "")),
        )

    def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        call = _read_keyed_call(context)
        if call is None:
            return
        self._settle(
            call,
            timed_out=isinstance(error, TimeoutPolicyError),
            error=str(error),
        )

    @staticmethod
    def _settle(call: _KeyedCall, *, timed_out: bool, error: str) -> None:
        """Mark now if nothing the call abandoned runs, else when it ends."""
        captured = contextvars.copy_context()

        def deferred(summary: WorkSummary) -> None:
            def body() -> None:
                try:
                    completed = _settled_as_completed(timed_out, summary)
                    _mark_sync(_ensure_policy_gate(), call, completed, error)
                except Exception as e:
                    _log_deferred_mark_failure(call.key, e)

            captured.copy().run(body)

        summary = _close_call_scope(call, deferred)
        if summary is None:
            _log_mark_deferred(call)
            return
        completed = _settled_as_completed(timed_out, summary)
        try:
            _mark_sync(_ensure_policy_gate(), call, completed, error)
        except Exception as e:
            _log_immediate_mark_failure(call.key, completed, e)

    def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        pass

    def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        pass

    def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        pass


class AsyncIdempotencyGuard:
    """Async twin of :class:`IdempotencyGuard` (implements ``AsyncPolicyGuard``).

    Awaited natively by ``AsyncPolicyComposer`` — zero thread hop — driving the
    awaitable :class:`AsyncIdempotencyGate`. Same two-phase model, same
    fail-CLOSED-by-default posture, same per-call record in ``context.extra``
    and the same work scope as the sync guard, so the async hook marks the
    claim it took with the exact window the caller requested. A ``CancelledError`` raised while awaiting the gate is a
    ``BaseException`` and escapes the fail-open ``except Exception``, so
    cancellation still propagates.
    """

    def __init__(
        self,
        key_generator: Callable[[PolicyContext], str],
        fail_open: bool | None = None,
        ttl: timedelta | None = None,
        execution_ttl: timedelta | None = None,
    ) -> None:
        # Cached layered read (686 D3/D5) so a console edit of the idempotency
        # domain is observed within the read-cache TTL; env base when no
        # RuntimeConfigManager is registered.
        from baldur.settings.idempotency import IdempotencySettings
        from baldur.settings.layered_provider import get_layered_settings_cached

        settings = get_layered_settings_cached(IdempotencySettings, "idempotency")
        self._globally_enabled = settings.enabled
        self._fail_open_on_cache_error = (
            settings.fail_open_on_cache_error if fail_open is None else fail_open
        )
        self._key_fn = key_generator
        self._ttl = ttl
        self._execution_ttl = execution_ttl
        # Resolve the async cache-backed gate at construction so a production
        # misconfiguration (no distributed cache adapter + escape hatch off)
        # surfaces loudly out of the facade's composer build — a correctness
        # gate fails closed. Gated on ``enabled`` so a disabled feature never
        # raises. Construction opens no socket (redis.asyncio connects lazily).
        if self._globally_enabled:
            _ensure_async_policy_gate()

    @property
    def name(self) -> str:
        return _GUARD_NAME

    async def check(self, context: PolicyContext | None = None) -> GuardResult:
        if context is None:
            return GuardResult(allowed=True)

        if not self._globally_enabled:
            return GuardResult(allowed=True)

        key = ""
        try:
            from baldur.core.idempotency_gate import IdempotencyDecision

            key = self._key_fn(context)
            gate = _ensure_async_policy_gate()
            result = await gate.check_and_acquire(key, ttl=self._execution_ttl)
            if result.decision == IdempotencyDecision.SKIP:
                logger.warning(
                    "idempotency.duplicate_blocked",
                    key=key,
                    decision="SKIP",
                )
                return GuardResult(
                    allowed=False,
                    reason=f"Already processed (idempotency key: {key})",
                    metadata={
                        "idempotency_decision": result.decision.name,
                        "idempotency_key": key,
                        "cached_result": result.cached_result,
                    },
                )
            if result.decision == IdempotencyDecision.ABORT:
                logger.warning(
                    "idempotency.execution_blocked",
                    key=key,
                    decision="ABORT",
                )
                return GuardResult(
                    allowed=False,
                    reason=f"Another process is executing (idempotency key: {key})",
                    metadata={
                        "idempotency_decision": result.decision.name,
                        "idempotency_key": key,
                    },
                )
            # CONTINUE — store the per-call record and open the work scope.
            _write_keyed_call(
                context, key, result.retry_count, self._ttl, result.claim_id
            )
            return GuardResult(allowed=True)
        except Exception as e:
            # Cache I/O fault or key-generation error. Fail CLOSED by default to
            # prevent a duplicate side effect on a blip; opt into fail-open.
            logger.warning(
                "idempotency.guard_check_failed",
                error=str(e),
                fail_open=self._fail_open_on_cache_error,
            )
            if self._fail_open_on_cache_error:
                return GuardResult(allowed=True)
            return GuardResult(
                allowed=False,
                reason="Idempotency check unavailable (cache error); failing closed.",
                metadata={
                    "idempotency_unavailable": True,
                    "idempotency_key": key,
                    "error": str(e),
                },
            )


class AsyncIdempotencyHook:
    """Async twin of :class:`IdempotencyHook` (implements ``AsyncPolicyHook``).

    Phase 2, awaited natively, by the sync hook's rule. An async timeout
    cancels the coroutine before the hook runs, so it leaves nothing running
    and the key is released at once; sync timed work the coroutine started
    (directly, or inside ``asyncio.to_thread``) that is still running holds the
    key until it ends. A late mark goes through the sync policy gate when the
    async ledger is the shared (Redis) one — it survives the loop's teardown —
    and is scheduled on the caller's loop when the ledger is in-process.
    Fail-open — a transient mark failure is logged but never raises.
    """

    async def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        call = _read_keyed_call(context)
        if call is None:
            return
        if result.outcome == PolicyOutcome.SUCCESS:
            _close_call_scope(call, None)
            try:
                await _mark_async(_ensure_async_policy_gate(), call, True, "")
            except Exception as e:
                _log_immediate_mark_failure(call.key, True, e)
            return
        await self._settle(
            call,
            timed_out=_timed_out_trigger(result),
            error=str(result.metadata.get("original_error", "")),
        )

    async def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        call = _read_keyed_call(context)
        if call is None:
            return
        await self._settle(
            call,
            timed_out=isinstance(error, TimeoutPolicyError),
            error=str(error),
        )

    @staticmethod
    async def _settle(call: _KeyedCall, *, timed_out: bool, error: str) -> None:
        """Mark now if nothing the call abandoned runs, else when it ends."""
        loop = asyncio.get_running_loop()
        captured = contextvars.copy_context()

        def deferred(summary: WorkSummary) -> None:
            def body() -> None:
                completed = _settled_as_completed(timed_out, summary)
                try:
                    async_gate = _ensure_async_policy_gate()
                    if _async_ledger_is_process_local(async_gate):
                        loop.call_soon_threadsafe(
                            _spawn_deferred_async_mark,
                            async_gate,
                            call,
                            completed,
                            error,
                            context=captured.copy(),
                        )
                    else:
                        _mark_sync(_ensure_policy_gate(), call, completed, error)
                except Exception as e:
                    _log_deferred_mark_failure(call.key, e)

            captured.copy().run(body)

        summary = _close_call_scope(call, deferred)
        if summary is None:
            _log_mark_deferred(call)
            return
        completed = _settled_as_completed(timed_out, summary)
        try:
            await _mark_async(_ensure_async_policy_gate(), call, completed, error)
        except Exception as e:
            _log_immediate_mark_failure(call.key, completed, e)

    async def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        pass

    async def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        pass

    async def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        pass
