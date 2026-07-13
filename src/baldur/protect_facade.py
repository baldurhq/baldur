"""
Baldur resilience facade — one-call entry point for CB + Retry + Fallback + DLQ.

Composes existing ResiliencePolicy implementations via ``PolicyComposer`` /
``AsyncPolicyComposer``. No new resilience logic lives here — only wiring.

Public surface:
- ``protect(name, fn, *, fallback=None, dlq=False, retry=False, circuit_breaker=True, context=None)`` → T
- ``aprotect(name, fn, *, ...)`` → awaitable T
- ``protect_with_meta(...)`` / ``aprotect_with_meta(...)`` → ``ProtectResult[T]``
- ``@protected(name, ...)`` / ``@aprotected(name, ...)`` — decorator forms
- ``ProtectResult`` — dataclass exposed by the ``_with_meta`` variants
"""

# Reference: 429 — Part 1

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Generic, Literal, TypeVar

import structlog

from baldur.core.types import ALLOWED_PRIMITIVE_TYPES, is_primitive_annotation
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)
from baldur.resilience.policies.composer import (
    AsyncPolicyComposer,
    PolicyComposer,
)
from baldur.resilience.policies.fallback import AsyncFallbackPolicy, FallbackPolicy
from baldur.services.circuit_breaker.policy import (
    AsyncCircuitBreakerPolicy,
    CircuitBreakerPolicy,
)
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from baldur.services.retry_handler.sinks import DLQSink

logger = structlog.get_logger()

T = TypeVar("T")


__all__ = [
    "ProtectResult",
    "protect",
    "aprotect",
    "protect_with_meta",
    "aprotect_with_meta",
    "protected",
    "aprotected",
    "reset_protect_caches",
]

_TIMEOUT_UNSET: Any = object()


# =============================================================================
# CircuitBreakerPolicy per-name cache (DEC-1)
#
# protect()'s public surface exposes only ``circuit_breaker: bool`` (no per-call
# CB tuning kwargs), so ``CircuitBreakerPolicy(service_name=name)`` is fully
# determined by ``name``. Caching it removes per-call object allocation and the
# per-call ProviderRegistry lookup inside ``_create_default_service``.
#
# Reset chain: ``reset_protect_caches()`` clears the cache and the recorder
# state. ``reset_protect_settings()`` (settings/protect.py) calls it via lazy
# import to keep test isolation correct when CB-related settings change.
# =============================================================================

_cb_policy_cache: dict[str, CircuitBreakerPolicy] = {}
_cb_policy_lock = threading.Lock()

# Profile discriminator for the composer cache key (#499 D2). The Literal
# alias gives static-type safety against typos at the cache lookup +
# fast-path branch sites without paying for an Enum import/instance for
# two internal-only values.
ComposerProfile = Literal["default", "dlq_protect"]

# Default-kwargs PolicyComposer cache (#481 DEC-2, extended by #482 D5,
# extended by #499 D2 to cover the ``@dlq_protect`` profile).
#
# Populated when ``_build_sync_composer`` is called with a cacheable
# profile. Key is ``(name, timeout_seconds, profile)`` where ``profile``
# is one of ``"default"`` (CB-only, post-#481/#482 canonical) or
# ``"dlq_protect"`` (CB + Retry + DLQ-sink, the canonical zero-message-loss
# decorator profile from #499). ``timeout_seconds`` may be a float
# (``TimeoutPolicy`` included in chain) or ``None`` (omitted).
# Non-cacheable profiles fall through to per-call construction untouched.
#
# Reuses ``_cb_policy_lock`` — both caches are invalidated together by
# ``reset_protect_caches()``, so a single lock keeps the invariants simple.
_composer_cache: dict[tuple[str, float | None, ComposerProfile], PolicyComposer] = {}

# Module-level singleton for ``DLQSink`` (#499 D1). ``DLQSink`` carries no
# instance state — ``handle_failure`` reads only ``policy_result.metadata``
# and delegates to ``baldur.dlq.helpers.store_to_dlq``. Sharing a single
# instance across all cached and slow-path composers eliminates the per-call
# instance allocation that dominated 7B.5's within-cycle RSS climb.
_DLQ_SINK = DLQSink()

# Fires at most once per process the first time a ``dlq=True`` composer is wired
# while no DLQ store backs ``store_to_dlq`` (e.g. OSS, ``baldur_pro`` absent).
# Without this, ``dlq=True`` is accepted with no signal yet the store silently
# no-ops — a false "failures are captured" guarantee (claim-wiring integrity). Reset by
# ``reset_protect_caches()`` in lockstep with ``_composer_cache`` so tests that
# flip PRO presence re-evaluate. The flag, not the log, enforces once-per-process.
_dlq_no_backing_warned = False


def _warn_if_dlq_backing_absent() -> None:
    """Warn once if a DLQ sink is being wired without a real store behind it.

    No-ops when a store is available (PRO present) or after the first warning.
    Logs at WARNING, never aborts: the sink stays wired (fail-open) and final
    failures still raise to the caller — only durable DLQ capture is missing.
    """
    global _dlq_no_backing_warned
    if _dlq_no_backing_warned:
        return
    from baldur.dlq.helpers import dlq_backing_available

    if dlq_backing_available():
        return
    _dlq_no_backing_warned = True
    logger.warning(
        "protect.dlq_requested_without_backing",
        hint=(
            "dlq=True was requested but no DLQ store is installed "
            "(baldur_pro absent); final failures still raise to the caller but "
            "are NOT durably captured for replay. Install baldur_pro to enable "
            "DLQ persistence, or unset dlq= to silence this warning."
        ),
    )


def _get_or_build_cb_policy(name: str) -> CircuitBreakerPolicy:
    """Return the cached ``CircuitBreakerPolicy`` for ``name``, building once.

    Double-checked locking — fast path is a dict membership test without lock.
    Concurrent first-call from N threads results in exactly one constructor
    invocation; the late arrivals see the cached instance.
    """
    cached = _cb_policy_cache.get(name)
    if cached is not None:
        return cached
    with _cb_policy_lock:
        cached = _cb_policy_cache.get(name)
        if cached is not None:
            return cached
        policy: CircuitBreakerPolicy = CircuitBreakerPolicy(service_name=name)
        _cb_policy_cache[name] = policy
        return policy


def _get_or_build_default_composer(
    name: str, timeout_seconds: float | None
) -> PolicyComposer:
    """Return the cached default-kwargs ``PolicyComposer`` for
    ``(name, timeout_seconds, "default")``, building once.

    Default-kwargs profile only (CB on, no retry/fallback/dlq). The
    ``timeout_seconds`` arg may be a positive float (``TimeoutPolicy``
    included in the chain) or ``None`` (``TimeoutPolicy`` omitted — the
    canonical profile). Same DCL pattern as
    ``_get_or_build_cb_policy``. Non-default profiles do NOT consult this
    cache — they fall through to per-call construction in
    ``_build_sync_composer``.

    The CB policy lookup runs OUTSIDE this function's lock acquisition so
    that ``_get_or_build_cb_policy``'s own lock acquisition does not
    reentrantly collide — both helpers share the same non-reentrant
    ``_cb_policy_lock``.
    """
    key: tuple[str, float | None, ComposerProfile] = (
        name,
        timeout_seconds,
        "default",
    )
    cached = _composer_cache.get(key)
    if cached is not None:
        return cached
    # Resolve dependencies BEFORE entering the lock so the inner cache
    # helpers do not reentrantly acquire ``_cb_policy_lock``.
    cb_policy = _get_or_build_cb_policy(name)
    timeout_policy = None
    if timeout_seconds is not None:
        from baldur.resilience.policies.timeout import TimeoutPolicy

        timeout_policy = TimeoutPolicy(timeout_seconds=timeout_seconds)
    with _cb_policy_lock:
        cached = _composer_cache.get(key)
        if cached is not None:
            return cached
        composer: PolicyComposer = PolicyComposer()
        composer.add(cb_policy)
        if timeout_policy is not None:
            composer.add(timeout_policy)
        _composer_cache[key] = composer
        logger.debug(
            "protect.composer_built",
            name=name,
            timeout_seconds=timeout_seconds,
            policies=[p.name for p in composer._policies],
        )
        return composer


def _get_or_build_dlq_protect_composer(
    name: str,
    timeout_seconds: float | None,
    retry_cfg: RetryPolicyConfig,
) -> PolicyComposer:
    """Return the cached ``@dlq_protect`` ``PolicyComposer`` for
    ``(name, timeout_seconds, "dlq_protect")``, building once.

    Profile pinned to ``dlq=True, retry=settings-derived, circuit_breaker=True,
    fallback=None`` — the canonical zero-message-loss decorator shape from
    ``@dlq_protect`` (`decorators/dlq_protect.py`). Same DCL pattern as
    ``_get_or_build_default_composer``: dependency construction happens
    BEFORE the lock to prevent reentrant ``_cb_policy_lock`` acquisition.

    The cached composer embeds a ``RetryPolicy`` constructed from
    ``retry_cfg`` (provided by ``_resolve_retry_stage`` with the
    settings-derived snapshot). Subsequent runtime-config mutations do NOT
    propagate until ``reset_protect_caches()`` is invoked — matches the
    existing CB cache contract.
    """
    key: tuple[str, float | None, ComposerProfile] = (
        name,
        timeout_seconds,
        "dlq_protect",
    )
    cached = _composer_cache.get(key)
    if cached is not None:
        return cached
    # Resolve dependencies BEFORE entering the lock so the inner cache
    # helpers do not reentrantly acquire ``_cb_policy_lock``.
    cb_policy = _get_or_build_cb_policy(name)
    timeout_policy = None
    if timeout_seconds is not None:
        from baldur.resilience.policies.timeout import TimeoutPolicy

        timeout_policy = TimeoutPolicy(timeout_seconds=timeout_seconds)
    retry_policy = RetryPolicy(config=retry_cfg)
    with _cb_policy_lock:
        cached = _composer_cache.get(key)
        if cached is not None:
            return cached
        composer: PolicyComposer = PolicyComposer()
        composer.add(cb_policy)
        if timeout_policy is not None:
            composer.add(timeout_policy)
        composer.add(retry_policy)
        composer.add_sink(_DLQ_SINK)
        _warn_if_dlq_backing_absent()
        _composer_cache[key] = composer
        logger.debug(
            "protect.composer_built",
            name=name,
            timeout_seconds=timeout_seconds,
            policies=[p.name for p in composer._policies],
        )
        return composer


def reset_protect_caches() -> None:
    """Reset all process-local protect() caches.

    Clears the per-name ``CircuitBreakerPolicy`` cache, the default-kwargs
    ``PolicyComposer`` cache, forwards to ``reset_protect_recorder()``
    (recorder singleton + sticky failure flag), and drains
    ``TimeoutPolicy``'s shared executor so the next test starts with a
    clean thread pool. Wired into ``reset_protect_settings()`` so test
    fixtures that reset settings automatically invalidate every piece of
    process-local state whose captured config snapshot would otherwise
    drift (CB service config, composer-bound TimeoutPolicy, recorder,
    executor worker count).
    """
    global _dlq_no_backing_warned
    with _cb_policy_lock:
        _cb_policy_cache.clear()
        _composer_cache.clear()
        # Re-arm the once-per-process DLQ-no-backing warning in lockstep with
        # the composer cache so a test that flips PRO presence re-evaluates.
        _dlq_no_backing_warned = False

    # #564 — invalidate the memoized cache-backed idempotency gate + replace
    # its in-process fallback cache so prior-test dedup state cannot leak into
    # the next test through the policy guard/hook.
    from baldur.resilience.policies.idempotency import _reset_policy_gate

    _reset_policy_gate()

    from baldur.metrics.recorders.protect import reset_protect_recorder

    reset_protect_recorder()

    # #485 D1a/G2 — invalidate the CB-blocked recorder sticky flag so
    # process-local state matches a fresh test fixture's expectations.
    from baldur.metrics.recorders.circuit_breaker import reset_blocked_recorder

    reset_blocked_recorder()

    # #485 D1d/G7 — also resets the new ``_metrics_init_failed`` sticky flag.
    from baldur.metrics.event_handlers import reset_event_handler_cache

    reset_event_handler_cache()

    # #485 D4/G6 — reset the overflow periodic-N counter + last-ratio cache
    # so each test starts at ``n=0`` with the always-check fast path.
    try:
        from baldur_pro.services.dlq.overflow import reset_overflow_state

        reset_overflow_state()
    except ImportError:
        pass

    # #486 D8 — drain pending outbox entries + stop worker + clear RingBuffer
    # so prior-test outbox state never leaks into the next test.
    try:
        from baldur.services.dlq_outbox import reset_dlq_outbox

        reset_dlq_outbox()
    except ImportError:
        pass

    from baldur.resilience.policies.timeout import TimeoutPolicy

    TimeoutPolicy.shutdown_executor()

    # 487 D3 — drain the EventBus dispatch executor so the next test
    # observes the current dispatch_workers / dispatch_mode settings.
    # Idempotent against the dual-invocation path through
    # reset_event_bus_settings(); both call sites land on the same
    # classmethod against a shared `_executor` slot.
    try:
        from baldur.services.event_bus.bus import shutdown_dispatch_executor

        shutdown_dispatch_executor()
    except ImportError:
        pass

    # 488 D7 — clear and rebuild the governance pipeline profile cache so
    # tests that swap singleton check identity observe a fresh set of
    # cached pipelines. Resolved through ProviderRegistry.governance so
    # the OSS NoOp default makes this a silent no-op when PRO is absent.
    try:
        from baldur.factory.registry import ProviderRegistry

        ProviderRegistry.governance.get().reset_governance_pipeline_cache()
    except Exception:
        pass


# =============================================================================
# ProtectResult — returned by the *_with_meta variants
# =============================================================================


@dataclass
class ProtectResult(Generic[T]):
    """Opt-in return type for ``protect_with_meta()`` callers who need
    fallback-used / attempts / duration visibility without subscribing to
    EventBus or Prometheus metrics.

    Attributes:
        value: Result value (either from ``fn`` or from ``fallback``).
        success: True when ``value`` came from either path without error.
        fallback_used: True when the fallback branch produced ``value``.
        attempts: Total policy attempts (1 when retry disabled).
        duration_seconds: Wall-clock duration of the call.
        error: Underlying exception when ``success`` is False (non-raising path).
        outcome: Raw ``PolicyOutcome`` for advanced callers.
        metadata: Pass-through of ``PolicyResult.metadata`` for debugging.
    """

    value: T | None = None
    success: bool = True
    fallback_used: bool = False
    attempts: int = 1
    duration_seconds: float = 0.0
    error: Exception | None = None
    outcome: PolicyOutcome = PolicyOutcome.SUCCESS
    metadata: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Composer builders — shared by sync/async paths
# =============================================================================


def _resolve_retry_stage(
    retry: bool | RetryPolicyConfig | ResiliencePolicy[T] | None,
    dlq_requested: bool,
    domain: str,
) -> tuple[RetryPolicyConfig | None, ResiliencePolicy[T] | None, bool]:
    """Resolve the ``retry=`` argument into the retry stage inputs.

    Returns ``(retry_cfg, retry_policy, settings_derived)`` where exactly
    one of the first two slots is non-None when retry is requested. The
    ``settings_derived`` flag is ``True`` iff ``retry_cfg`` was produced
    via the ``RetryPolicyConfig.from_settings(domain=name)`` path (i.e.
    caller passed ``retry=True`` or ``retry=None`` resolving to the
    settings default). It is consumed by ``_build_sync_composer``'s
    ``dlq_protect`` fast-path to gate cache eligibility.

    - ``retry=False`` → ``(None, None, False)`` (no retry stage)
    - ``retry=True`` →
      ``(RetryPolicyConfig.from_settings(domain=name), None, True)``
    - ``retry=RetryPolicyConfig(...)`` → ``(cfg, None, False)`` — explicit
      callers stay on the slow path; caching across name would collide.
    - ``retry=ResiliencePolicy(...)`` → ``(None, retry, False)`` (caller-supplied
      pre-built policy, e.g. ``TenacityBridgePolicy``)
    - ``retry=None`` → resolves against ``ProtectSettings.default_retry`` and
      falls into one of the branches above (``settings_derived`` follows
      the resolved branch).
    """
    from baldur.settings.protect import get_protect_settings

    if retry is None:
        retry = get_protect_settings().default_retry

    if isinstance(retry, ResiliencePolicy):
        return None, retry, False

    if isinstance(retry, RetryPolicyConfig):
        cfg = retry
        settings_derived = False
    elif retry is True:
        cfg = RetryPolicyConfig.from_settings(domain=domain)
        settings_derived = True
    else:
        return None, None, False

    if dlq_requested and not cfg.enable_dlq:
        from dataclasses import replace

        cfg = replace(cfg, enable_dlq=True)
    return cfg, None, settings_derived


def _resolve_flags(
    dlq: bool | None,
    circuit_breaker: bool | None,
) -> tuple[bool, bool]:
    """Resolve dlq / circuit_breaker flags against ProtectSettings defaults."""
    from baldur.settings.protect import get_protect_settings

    settings = get_protect_settings()
    dlq_flag = settings.default_dlq if dlq is None else bool(dlq)
    cb_flag = (
        settings.default_circuit_breaker
        if circuit_breaker is None
        else bool(circuit_breaker)
    )
    return dlq_flag, cb_flag


def _resolve_timeout(timeout: Any) -> float | None:
    """Resolve timeout sentinel against ProtectSettings default.

    Three-state resolution: ``_TIMEOUT_UNSET`` reads
    ``ProtectSettings.default_timeout_seconds`` (``None`` by
    default, opt-in via env var); explicit ``None`` always wins regardless
    of the resolved setting value (caller's intent to disable timeout); an
    explicit float passes through unchanged.
    """
    if timeout is _TIMEOUT_UNSET:
        from baldur.settings.protect import get_protect_settings

        return get_protect_settings().default_timeout_seconds
    return timeout


# =============================================================================
# Idempotency stage (#564) — opt-in guard+hook bracket for protect()
#
# Spans the WHOLE call (acquire before the policy chain, mark after), so dedup
# is once-per-operation, not per-retry-attempt. The guard/hook resolve a real
# cache-backed gate (resilience/policies/idempotency.py) instead of the bare
# ``cache=None`` singleton, so a duplicate key is actually blocked.
# =============================================================================

# PolicyContext dataclass field names — used to resolve the ``str`` field-name
# form of ``idempotency_key`` against a named context attribute before falling
# back to ``extra``. ``extra`` itself is excluded (it is the open-ended
# container, not a key source).
_POLICY_CONTEXT_FIELDS: frozenset[str] = frozenset(PolicyContext.__dataclass_fields__)


def _read_context_field(context: PolicyContext, field: str) -> Any:
    """Resolve ``field`` against a ``PolicyContext`` for the str-form key.

    Lookup order: the named PolicyContext field (``order_id`` /
    ``payment_id`` / ``user_id`` / …), then ``extra["request_data"][field]``
    (the per-call payload snapshot ``@protected`` writes), then ``extra[field]``.
    Returns ``None`` when unresolved — the caller fail-fasts on that.
    """
    if field in _POLICY_CONTEXT_FIELDS and field != "extra":
        value = getattr(context, field)
        if value is not None:
            return value
    extra = context.extra or {}
    request_data = extra.get("request_data")
    if isinstance(request_data, dict) and field in request_data:
        return request_data[field]
    return extra.get(field)


def _field_key_generator(name: str, field: str) -> Callable[[PolicyContext], str]:
    """Build a ``key_generator`` for the str (field-name) form of ``idempotency_key``.

    The returned closure reads ``field`` from the ``PolicyContext`` (see
    :func:`_read_context_field`) and returns the service-name-namespaced key
    ``f"{name}:{value}"`` so the same ``order_id`` across two different protected
    operations does not collide. Fail-fast: a missing/``None`` field, or a
    non-primitive value, raises ``ValueError`` rather than producing a key —
    silently coercing (``f"{name}:None"``, ``str(some_dict)``) would collapse
    every field-less call onto one key, causing mass false-dedup.
    """

    def _generate(context: PolicyContext) -> str:
        value = _read_context_field(context, field)
        # None is a member of ALLOWED_PRIMITIVE_TYPES (type(None)), so the
        # None check MUST precede the primitive check.
        if value is None:
            raise ValueError(
                f"idempotency_key={field!r} resolved to no value on the "
                f"PolicyContext for protect(name={name!r}); set the field on the "
                f"context / call args, or pass a Callable[[PolicyContext], str] "
                f"for a composite key."
            )
        if not isinstance(value, ALLOWED_PRIMITIVE_TYPES):
            # ValueError (not TypeError): both key-resolution failures raise the
            # same type so callers catch one exception for "cannot resolve key".
            raise ValueError(  # noqa: TRY004
                f"idempotency_key={field!r} resolved to a non-primitive "
                f"{type(value).__name__} for protect(name={name!r}); pass a "
                f"Callable[[PolicyContext], str] to build the key explicitly."
            )
        return f"{name}:{value}"

    return _generate


def _resolve_idempotency_key_generator(
    name: str,
    idempotency_key: str | Callable[[PolicyContext], str] | None,
    context: PolicyContext | None,
    idempotency_ttl: timedelta | None,
    idempotency_execution_ttl: timedelta | None,
) -> Callable[[PolicyContext], str] | None:
    """Validate idempotency wiring and resolve the key generator (shared).

    The sync/async stage builders differ only in which guard/hook classes they
    construct; the wiring-time fail-fast and key-generator resolution are
    identical, so they live here.

    Returns ``None`` when ``idempotency_key`` is ``None`` (no stage). Otherwise:

    - ``idempotency_key is not None and context is None`` → ``ValueError``
      (every key form requires a context).
    - non-positive / non-``timedelta`` ttls → ``ValueError``.
    - str field-name form → eagerly validated against ``context`` (a
      missing/``None``/non-primitive field raises ``ValueError`` here rather
      than being swallowed by the composer's fail-open guard loop).
    - Callable form → returned verbatim (trusted; NOT eagerly invoked since it
      may carry side effects — a runtime fault surfaces in ``guard.check()``,
      fail-closed by default).
    """
    # Reference: 564 D2/D5;
    # 567 D9;
    # 595 D4 (TTL threading).
    if idempotency_key is None:
        return None
    if context is None:
        raise ValueError(
            "idempotency_key requires a PolicyContext; pass context= or use "
            "@protected/@aprotected"
        )
    for _ttl_label, _ttl_value in (
        ("idempotency_ttl", idempotency_ttl),
        ("idempotency_execution_ttl", idempotency_execution_ttl),
    ):
        if _ttl_value is not None and (
            not isinstance(_ttl_value, timedelta) or _ttl_value <= timedelta(0)
        ):
            raise ValueError(
                f"{_ttl_label} must be a positive timedelta for "
                f"protect(name={name!r}); got {_ttl_value!r}."
            )

    if callable(idempotency_key):
        return idempotency_key
    key_generator = _field_key_generator(name, idempotency_key)
    # Eager validation against the supplied context (fail-fast).
    key_generator(context)
    return key_generator


def _build_idempotency_stage(
    name: str,
    idempotency_key: str | Callable[[PolicyContext], str] | None,
    context: PolicyContext | None,
    idempotency_fail_open: bool | None = None,
    idempotency_ttl: timedelta | None = None,
    idempotency_execution_ttl: timedelta | None = None,
) -> tuple[Any, Any] | None:
    """Resolve ``idempotency_key`` into a sync ``(IdempotencyGuard,
    IdempotencyHook)`` pair, or ``None`` when no idempotency is requested.

    Wiring-time fail-fast + key-generator resolution live in
    :func:`_resolve_idempotency_key_generator`. Constructing
    :class:`IdempotencyGuard` resolves the cache-backed gate: in production with
    no registered cache adapter and the escape hatch off, that raises
    ``ConfigurationError`` here — a correctness gate fails closed.

    ``idempotency_fail_open`` sets the guard's cache-error fail direction;
    ``idempotency_ttl`` / ``idempotency_execution_ttl`` thread the memory /
    execution windows to the guard.
    """
    key_generator = _resolve_idempotency_key_generator(
        name, idempotency_key, context, idempotency_ttl, idempotency_execution_ttl
    )
    if key_generator is None:
        return None

    from baldur.resilience.policies.idempotency import (
        IdempotencyGuard,
        IdempotencyHook,
    )

    return (
        IdempotencyGuard(
            key_generator=key_generator,
            fail_open=idempotency_fail_open,
            ttl=idempotency_ttl,
            execution_ttl=idempotency_execution_ttl,
        ),
        IdempotencyHook(),
    )


def _build_async_idempotency_stage(
    name: str,
    idempotency_key: str | Callable[[PolicyContext], str] | None,
    context: PolicyContext | None,
    idempotency_fail_open: bool | None = None,
    idempotency_ttl: timedelta | None = None,
    idempotency_execution_ttl: timedelta | None = None,
) -> tuple[Any, Any] | None:
    """Resolve ``idempotency_key`` into an async ``(AsyncIdempotencyGuard,
    AsyncIdempotencyHook)`` pair for the async facade path, or ``None``.

    Async sibling of :func:`_build_idempotency_stage` — same wiring-time
    fail-fast (shared resolver) but constructs the async guard/hook that
    ``AsyncPolicyComposer`` awaits natively (zero thread hop) over
    ``AsyncIdempotencyGate``. Constructing :class:`AsyncIdempotencyGuard`
    resolves the async cache-backed gate; a production misconfiguration
    (no adapter + escape hatch off) raises ``ConfigurationError`` here.
    """
    key_generator = _resolve_idempotency_key_generator(
        name, idempotency_key, context, idempotency_ttl, idempotency_execution_ttl
    )
    if key_generator is None:
        return None

    from baldur.resilience.policies.idempotency import (
        AsyncIdempotencyGuard,
        AsyncIdempotencyHook,
    )

    return (
        AsyncIdempotencyGuard(
            key_generator=key_generator,
            fail_open=idempotency_fail_open,
            ttl=idempotency_ttl,
            execution_ttl=idempotency_execution_ttl,
        ),
        AsyncIdempotencyHook(),
    )


def _build_sync_composer(
    name: str,
    fallback: Callable[[], T] | Callable[[Exception], T] | None,
    dlq: bool,
    retry_cfg: RetryPolicyConfig | None,
    circuit_breaker: bool,
    timeout_seconds: float | None,
    retry_policy: ResiliencePolicy[T] | None = None,
    retry_settings_derived: bool = False,
    idempotency_stage: tuple[Any, Any] | None = None,
) -> PolicyComposer[T]:
    """Assemble sync policy chain: Fallback → CB → Timeout → Retry, with optional DLQ sink.

    Policy add-order is outer→inner execution order. Fallback sits OUTERMOST so
    it is the last resort covering every inner outcome — a plain failure, retry
    exhaustion, a timeout, and a CB-open rejection all route to it. CB sits
    inside the fallback (so absorbed failures still count toward the breaker) but
    outside Retry (so one exhausted retry-sequence is a single CB failure).
    Timeout wraps the retry stage to bound the whole sequence.

    ``retry_policy`` (when non-None) replaces the native ``RetryPolicy``
    construction — ``retry_cfg`` MUST be None in that case (caller supplies
    a pre-built policy such as ``TenacityBridgePolicy``).

    ``retry_settings_derived`` flags retry_cfg objects produced by
    the ``RetryPolicyConfig.from_settings(domain=name)`` path. Only those
    can hit the ``dlq_protect`` cache fast-path; explicit
    ``RetryPolicyConfig(...)`` callers stay on the slow path to prevent
    cross-callsite cache collision on the same ``name``.

    ``idempotency_stage`` (when non-None, an ``(IdempotencyGuard,
    IdempotencyHook)`` pair from :func:`_build_idempotency_stage`) is appended
    as a guard+hook bracket and forces the slow path — an idempotency-enabled
    call must never return a cached idempotency-less composer.
    """
    # Default-kwargs fast-path: 5 conditions match the canonical
    # ``protect("name", fn)`` profile → return the per-(name, timeout)
    # cached composer (#481 DEC-2, extended by #482 D5 to cover the
    # post-flip ``timeout_seconds is None`` default). Saves the per-call
    # PolicyComposer (+ TimeoutPolicy when non-None) construction.
    # Non-default profiles fall through to per-call build below.
    # ``idempotency_stage is None`` (#564 D3): an idempotency-enabled call
    # carries a non-hashable key generator + a per-call guard/hook, so it
    # bypasses the composer cache and builds per-call.
    if (
        circuit_breaker
        and retry_cfg is None
        and retry_policy is None
        and fallback is None
        and not dlq
        and idempotency_stage is None
    ):
        return _get_or_build_default_composer(name, timeout_seconds)  # type: ignore[return-value]

    # ``@dlq_protect`` fast-path (#499 D3+D4): matches the canonical
    # zero-message-loss decorator profile. Eligibility requires the retry
    # config to be settings-derived so explicit ``RetryPolicyConfig`` callers
    # do not collide with ``@dlq_protect("X")`` on the same cache key.
    if (
        circuit_breaker
        and dlq
        and retry_cfg is not None
        and retry_settings_derived
        and retry_policy is None
        and fallback is None
        and idempotency_stage is None
    ):
        return _get_or_build_dlq_protect_composer(  # type: ignore[return-value]
            name, timeout_seconds, retry_cfg
        )

    composer: PolicyComposer[T] = PolicyComposer()
    if idempotency_stage is not None:
        guard, hook = idempotency_stage
        composer.add_guard(guard)
        composer.add_hook(hook)
    if fallback is not None:
        composer.add(FallbackPolicy(fallback_fn=fallback))
    if circuit_breaker:
        composer.add(_get_or_build_cb_policy(name))
    if timeout_seconds is not None:
        from baldur.resilience.policies.timeout import TimeoutPolicy

        composer.add(TimeoutPolicy(timeout_seconds=timeout_seconds))
    if retry_policy is not None:
        composer.add(retry_policy)
    elif retry_cfg is not None:
        composer.add(RetryPolicy(config=retry_cfg))
    if dlq:
        composer.add_sink(_DLQ_SINK)
        _warn_if_dlq_backing_absent()
    logger.debug(
        "protect.composer_built",
        name=name,
        timeout_seconds=timeout_seconds,
        policies=[p.name for p in composer._policies],
    )
    return composer


def _guard_async_unsupported(
    retry: bool | RetryPolicyConfig | ResiliencePolicy[Any] | None,
) -> None:
    """Reject an arbitrary sync ``ResiliencePolicy`` passed as ``retry=``.

    First-party async CB + retry are wired (``AsyncCircuitBreakerPolicy`` +
    ``AsyncRetryPolicy``), and a pre-built ``TenacityBridgePolicy`` is now
    **auto-converted** to ``AsyncTenacityBridgePolicy`` on the async path (see
    :func:`_build_async_composer`). The only unsupported shape is any OTHER
    sync ``ResiliencePolicy`` — one that cannot be safely awaited under an async
    loop. Silent degradation is forbidden in a resilience framework, so this
    fails loud rather than running ``fn`` unprotected.
    """
    if not isinstance(retry, ResiliencePolicy):
        return
    try:
        from baldur.bridges.tenacity.policy import TenacityBridgePolicy
    except ImportError:
        TenacityBridgePolicy = None  # type: ignore[assignment,misc]
    if TenacityBridgePolicy is not None and isinstance(retry, TenacityBridgePolicy):
        # Supported — auto-converted to AsyncTenacityBridgePolicy downstream.
        return
    raise NotImplementedError(
        "aprotect(retry=<ResiliencePolicy>) is only supported for a "
        "TenacityBridgePolicy (auto-converted to AsyncTenacityBridgePolicy). "
        "For an arbitrary sync ResiliencePolicy, use the sync protect(), or "
        "wrap fn with your own async retry loop."
    )


def _build_async_composer(
    name: str,
    fallback: Callable[[], Awaitable[T]] | Callable[[Exception], Awaitable[T]] | None,
    dlq: bool,
    retry_cfg: RetryPolicyConfig | None,
    circuit_breaker: bool,
    timeout_seconds: float | None,
    retry_policy: ResiliencePolicy[T] | None = None,
    idempotency_stage: tuple[Any, Any] | None = None,
) -> AsyncPolicyComposer[T]:
    """Assemble async policy chain: Fallback → CB → Timeout → Retry, with optional DLQ sink.

    Sync-mirrored add-order (outer→inner execution): Fallback sits OUTERMOST as
    the last resort covering every inner outcome (plain failure, retry
    exhaustion, timeout, CB-open). CB sits inside the fallback (absorbed failures
    still count toward the breaker) but outside Retry so one exhausted
    retry-sequence counts as a single CB failure — matching the sync
    ``_build_sync_composer`` semantics — and Timeout wraps the whole retry
    chain to bound the entire sequence (a global, not per-attempt, timeout).

    The circuit breaker is obtained via ``_get_or_build_cb_policy(name)`` and
    wrapped in ``AsyncCircuitBreakerPolicy`` so sync and async ``protect(name=…)``
    share one ``CircuitBreakerService`` — failures accumulate across both call
    styles so the breaker actually opens. Building a fresh breaker per call
    would leave it permanently closed.

    ``retry_policy`` (when non-None) is a caller-supplied pre-built retry stage.
    A sync ``TenacityBridgePolicy`` is **auto-converted** to
    ``AsyncTenacityBridgePolicy`` (same stop/wait/retry/collaborators) so a user
    builds ONE sync bridge object and it works on either path. ``retry_cfg`` and
    ``retry_policy`` are mutually exclusive (``_resolve_retry_stage`` populates
    exactly one).

    ``idempotency_stage`` (an ``(AsyncIdempotencyGuard, AsyncIdempotencyHook)``
    pair from :func:`_build_async_idempotency_stage`) is awaited **natively** by
    ``AsyncPolicyComposer`` — the async guard drives the awaitable
    ``AsyncIdempotencyGate`` with zero thread hop.
    """
    composer: AsyncPolicyComposer[T] = AsyncPolicyComposer()
    if idempotency_stage is not None:
        guard, hook = idempotency_stage
        composer.add_guard(guard)
        composer.add_hook(hook)
    if fallback is not None:
        composer.add(AsyncFallbackPolicy(fallback_fn=fallback))
    if circuit_breaker:
        composer.add(AsyncCircuitBreakerPolicy(_get_or_build_cb_policy(name)))
    if timeout_seconds is not None:
        from baldur.resilience.policies.timeout import AsyncTimeoutPolicy

        composer.add(AsyncTimeoutPolicy(timeout_seconds=timeout_seconds))
    if retry_policy is not None:
        from baldur.bridges.tenacity.policy import (
            AsyncTenacityBridgePolicy,
            TenacityBridgePolicy,
        )

        if isinstance(retry_policy, AsyncTenacityBridgePolicy):
            composer.add(retry_policy)
        elif isinstance(retry_policy, TenacityBridgePolicy):
            # Auto-convert the sync bridge to its async twin (same config).
            composer.add(AsyncTenacityBridgePolicy.from_sync(retry_policy))
        else:
            # Unreachable — _guard_async_unsupported rejects any non-tenacity
            # ResiliencePolicy before we reach the composer build. Defensive.
            raise NotImplementedError(
                "aprotect(retry=<ResiliencePolicy>) is only supported for a "
                "TenacityBridgePolicy."
            )
    elif retry_cfg is not None:
        from baldur.resilience.policies.async_retry import AsyncRetryPolicy

        composer.add(AsyncRetryPolicy.from_policy_config(retry_cfg))
    if dlq:
        composer.add_sink(_DLQ_SINK)
        _warn_if_dlq_backing_absent()
    logger.debug(
        "protect.composer_built",
        name=name,
        timeout_seconds=timeout_seconds,
        policies=[p.name for p in composer._policies],
    )
    return composer


# =============================================================================
# Core execution helpers — single source of truth for both public variants
# =============================================================================


def _outcome_label(outcome: PolicyOutcome) -> str:
    """Map PolicyOutcome to Prometheus label value."""
    if outcome == PolicyOutcome.SUCCESS:
        return "success"
    if outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK:
        return "fallback"
    if outcome == PolicyOutcome.REJECTED:
        return "rejected"
    if outcome == PolicyOutcome.TIMEOUT:
        return "timeout"
    return "failure"


def _record_metrics(
    name: str,
    result: PolicyResult[Any],
    duration_seconds: float,
    mode: str = "sync",
) -> None:
    """Emit protect-scope Prometheus metrics. Fail-open.

    ``mode`` is ``"sync"`` for the ``protect``/``protect_with_meta`` funnel and
    ``"async"`` for ``aprotect``/``aprotect_with_meta``, so a ``name`` protected
    via both facades is separable on the ``mode`` label.
    """
    try:
        from baldur.metrics.recorders.protect import get_protect_recorder

        recorder = get_protect_recorder()
        if recorder is None:
            return
        recorder.record(
            name=name,
            outcome=_outcome_label(result.outcome),
            attempts=max(result.total_attempts, 1),
            duration_seconds=duration_seconds,
            fallback_used=result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK,
            mode=mode,
        )
    except Exception as e:
        logger.debug("protect.metrics_record_failed", error=e)


def _to_protect_result(
    result: PolicyResult[T],
    duration_seconds: float,
) -> ProtectResult[T]:
    """Convert the internal PolicyResult into the public ProtectResult DTO."""
    return ProtectResult(
        value=result.value,
        success=result.success,
        fallback_used=result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK,
        attempts=max(result.total_attempts, 1),
        duration_seconds=duration_seconds,
        error=result.error,
        outcome=result.outcome,
        metadata=dict(result.metadata),
    )


def _finalize_value(result: PolicyResult[T]) -> T:
    """Return the success value or raise the underlying error.

    Called by the raw ``protect()`` / ``aprotect()`` entry points. A guard
    short-circuit (``REJECTED`` with no captured error) is mapped to a domain
    exception: an idempotency block raises ``IdempotencyDuplicateError`` (the
    same type ``@idempotent`` raises), and a cache-unavailable fail-closed
    raises ``IdempotencyUnavailableError`` — so the caller never sees an opaque
    ``RuntimeError`` and can distinguish "deduped (safe)" from "couldn't verify".
    """
    if result.success:
        return result.value  # type: ignore[return-value]
    if result.error is not None:
        raise result.error

    # REJECTED with no captured error == a guard short-circuited the chain.
    if result.outcome is PolicyOutcome.REJECTED:
        from baldur.resilience.policies.idempotency import _GUARD_NAME

        if result.metadata.get("rejected_by") == _GUARD_NAME:
            key = str(result.metadata.get("idempotency_key", ""))
            if result.metadata.get("idempotency_unavailable"):
                from baldur.core.exceptions import IdempotencyUnavailableError

                raise IdempotencyUnavailableError(
                    key=key,
                    error=str(result.metadata.get("error", "")),
                )
            from baldur.core.exceptions import IdempotencyDuplicateError

            raise IdempotencyDuplicateError(
                key=key,
                decision=str(result.metadata.get("idempotency_decision", "")),
            )

    # Defensive fallback — idempotency is the facade's sole guard today, so a
    # non-idempotency REJECTED with no error is unreachable; raise a domain
    # exception (not bare RuntimeError) per the exception-hierarchy rule. A typed
    # reject exception belongs to whatever future guard first makes this branch
    # reachable.
    from baldur.core.exceptions import BaldurError

    raise BaldurError(
        f"protect() failed with outcome={result.outcome.value} and no error captured"
    )


# =============================================================================
# Sync public API
# =============================================================================


def protect(  # verified-by: test_concurrent_duplicates_run_side_effect_exactly_once
    name: str,
    fn: Callable[[], T],
    *,
    fallback: Callable[[], T] | Callable[[Exception], T] | None = None,
    dlq: bool | None = None,
    retry: bool | RetryPolicyConfig | ResiliencePolicy[T] | None = None,
    circuit_breaker: bool | None = None,
    timeout: float | None = _TIMEOUT_UNSET,
    idempotency_key: str | Callable[[PolicyContext], str] | None = None,
    idempotency_fail_open: bool | None = None,
    idempotency_ttl: timedelta | None = None,
    idempotency_execution_ttl: timedelta | None = None,
    context: PolicyContext | None = None,
) -> T:
    """Run ``fn`` under Baldur's composed resilience pipeline and return its value.

    Composition order (outer→inner): Fallback → CircuitBreaker → Timeout →
    Retry. ``fn`` runs the full ``retry.max_attempts`` before the fallback
    serves; the fallback is the last resort covering retry exhaustion, a
    timeout, AND a CB-open rejection. On all-failed WITHOUT a fallback the
    original exception is re-raised; final failures optionally flow through
    ``DLQSink``.

    Args:
        name: Service identifier. Used as the Circuit Breaker key, Retry
            domain, and Prometheus label — keep it stable per downstream.
        fn: Zero-argument callable to protect. Must be idempotent when
            ``retry`` is enabled — or supply ``idempotency_key=`` to dedup
            duplicate executions.
        fallback: Optional callable invoked when the protected call fails and
            the pipeline falls back; its return value is returned. Accepts
            either zero arguments (``fallback()``) or one positional
            (``fallback(error)`` — the absorbed exception, so it can branch on
            failure type and re-raise to decline). Runs OUTSIDE the timeout
            clock, so keep it cheap and local.
        dlq: When True, final failures flow into the DLQ repository resolved
            via ``ProviderRegistry``. ``None`` uses ``ProtectSettings.default_dlq``.
        retry: ``True`` uses ``RetryPolicyConfig.from_settings(domain=name)``;
            pass a ``RetryPolicyConfig`` for explicit control;
            pass a pre-built ``ResiliencePolicy`` (e.g.
            ``TenacityBridgePolicy``) to use it as the retry stage directly.
            ``False``/``None`` disables retry (``None`` consults
            ``ProtectSettings.default_retry``).
        circuit_breaker: When True, wraps ``fn`` in ``CircuitBreakerPolicy``.
            ``None`` uses ``ProtectSettings.default_circuit_breaker``.
        idempotency_key: Opt into composed dedup so a retried (or re-submitted)
            operation runs its side effect once, and a concurrent in-flight
            duplicate (double-submit, duplicate webhook) is blocked rather than
            executed in parallel. A ``str`` is read as a ``PolicyContext`` field
            name (e.g. ``"order_id"``) and namespaced as ``f"{name}:{value}"``;
            a ``Callable[[PolicyContext], str]`` builds a composite/custom key
            verbatim. Requires a ``context`` (raises ``ValueError`` otherwise);
            a missing/``None``/non-primitive field also raises ``ValueError``
            rather than producing a false-dedup key. ``None`` (default) adds no
            idempotency stage. Note: dedup is exactly-once for the concurrent
            case; across a process crash between the side effect and the
            post-execution mark the operation may run again after the gate
            record's TTL expires (an essential at-least-once limit — true
            exactly-once requires a transactional outbox spanning your own side
            effect).
        idempotency_fail_open: Cache-error fail direction for the dedup gate.
            ``None`` (default) consults
            ``IdempotencySettings.fail_open_on_cache_error`` (default
            fail-closed). ``False`` forces fail-closed (a cache I/O error during
            the check raises ``IdempotencyUnavailableError``); ``True`` forces
            fail-open (the unverifiable check proceeds). Ignored when
            ``idempotency_key`` is ``None``.
        idempotency_ttl: Dedup memory window — how long a completed operation
            is remembered (how long duplicates stay blocked after success).
            ``None`` (default) uses the gate memory default
            (``BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS``, 30 minutes unless
            tuned). Ignored when ``idempotency_key`` is ``None``.
        idempotency_execution_ttl: In-flight execution window — how long a
            running claim is honored before a crashed attempt becomes
            retryable. ``None`` (default) uses the gate execution default
            (30 minutes). Set it >= the worst-case runtime of ``fn``; a value
            below it risks a concurrent duplicate run via stale takeover.
            Ignored when ``idempotency_key`` is ``None``.
        context: Optional ``PolicyContext`` carrying business identifiers
            (order_id, user_id, trace_id) for Guard/Hook/Sink propagation.
            Required when ``idempotency_key`` is supplied.

    Returns:
        Whatever ``fn`` (or ``fallback``) returned on the succeeding branch.

    Raises:
        Exception: Re-raises the underlying error when all branches fail and
            no fallback produced a value.
        IdempotencyDuplicateError: A duplicate was blocked by the dedup gate —
            already completed (SKIP) or a concurrent in-flight call (ABORT). The
            same type ``@idempotent`` raises.
        IdempotencyUnavailableError: A cache I/O error prevented the dedup check
            and ``idempotency_fail_open`` resolved to fail-closed (the default).
        ValueError: ``idempotency_key`` supplied without a ``context``, its
            field resolves to a missing/non-primitive value, or
            ``idempotency_ttl`` / ``idempotency_execution_ttl`` is not a
            positive ``timedelta``.
    """
    from baldur.observability.structlog_config import configure_structlog

    configure_structlog()

    from baldur.settings.protect import get_protect_settings

    if not get_protect_settings().enabled:
        return fn()

    dlq_flag, cb_flag = _resolve_flags(dlq, circuit_breaker)
    timeout_seconds = _resolve_timeout(timeout)
    retry_cfg, retry_policy_obj, retry_settings_derived = _resolve_retry_stage(
        retry, dlq_requested=dlq_flag, domain=name
    )
    idempotency_stage = _build_idempotency_stage(
        name,
        idempotency_key,
        context,
        idempotency_fail_open,
        idempotency_ttl,
        idempotency_execution_ttl,
    )

    composer = _build_sync_composer(
        name=name,
        fallback=fallback,
        dlq=dlq_flag,
        retry_cfg=retry_cfg,
        circuit_breaker=cb_flag,
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy_obj,
        retry_settings_derived=retry_settings_derived,
        idempotency_stage=idempotency_stage,
    )

    start = time.perf_counter()
    result: PolicyResult[T] = composer.execute(fn, context=context)
    duration = time.perf_counter() - start
    _record_metrics(name, result, duration, mode="sync")
    return _finalize_value(result)


def protect_with_meta(
    name: str,
    fn: Callable[[], T],
    *,
    fallback: Callable[[], T] | Callable[[Exception], T] | None = None,
    dlq: bool | None = None,
    retry: bool | RetryPolicyConfig | ResiliencePolicy[T] | None = None,
    circuit_breaker: bool | None = None,
    timeout: float | None = _TIMEOUT_UNSET,
    idempotency_key: str | Callable[[PolicyContext], str] | None = None,
    idempotency_fail_open: bool | None = None,
    idempotency_ttl: timedelta | None = None,
    idempotency_execution_ttl: timedelta | None = None,
    context: PolicyContext | None = None,
) -> ProtectResult[T]:
    """Non-raising variant of ``protect()`` — returns a ``ProtectResult`` DTO.

    Use when the caller needs to inspect fallback-used / attempts / duration
    without raising on failure. The rest of the contract matches ``protect()``,
    including ``idempotency_key`` / ``idempotency_fail_open`` (a dedup-blocked
    duplicate surfaces as ``PolicyOutcome.REJECTED`` on the returned
    ``ProtectResult``; ``metadata["idempotency_decision"]`` is ``"SKIP"`` or
    ``"ABORT"``, and a cache-unavailable fail-closed sets
    ``metadata["idempotency_unavailable"] = True`` — so a non-raising caller can
    distinguish "already completed" from "in progress, retry later" from
    "couldn't verify").
    """
    from baldur.observability.structlog_config import configure_structlog

    configure_structlog()

    from baldur.settings.protect import get_protect_settings

    if not get_protect_settings().enabled:
        start = time.perf_counter()
        try:
            value = fn()
            return ProtectResult(
                value=value,
                success=True,
                attempts=1,
                duration_seconds=time.perf_counter() - start,
            )
        except Exception as e:
            return ProtectResult(
                value=None,
                success=False,
                attempts=1,
                duration_seconds=time.perf_counter() - start,
                error=e,
                outcome=PolicyOutcome.FAILURE,
            )

    dlq_flag, cb_flag = _resolve_flags(dlq, circuit_breaker)
    timeout_seconds = _resolve_timeout(timeout)
    retry_cfg, retry_policy_obj, retry_settings_derived = _resolve_retry_stage(
        retry, dlq_requested=dlq_flag, domain=name
    )
    idempotency_stage = _build_idempotency_stage(
        name,
        idempotency_key,
        context,
        idempotency_fail_open,
        idempotency_ttl,
        idempotency_execution_ttl,
    )

    composer = _build_sync_composer(
        name=name,
        fallback=fallback,
        dlq=dlq_flag,
        retry_cfg=retry_cfg,
        circuit_breaker=cb_flag,
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy_obj,
        retry_settings_derived=retry_settings_derived,
        idempotency_stage=idempotency_stage,
    )

    start = time.perf_counter()
    result: PolicyResult[T] = composer.execute(fn, context=context)
    duration = time.perf_counter() - start
    _record_metrics(name, result, duration, mode="sync")
    return _to_protect_result(result, duration)


# =============================================================================
# Async public API
# =============================================================================


async def aprotect(  # verified-by: test_concurrent_duplicates_run_side_effect_exactly_once
    name: str,
    fn: Callable[[], Awaitable[T]],
    *,
    fallback: Callable[[], Awaitable[T]]
    | Callable[[Exception], Awaitable[T]]
    | None = None,
    dlq: bool | None = None,
    retry: bool | RetryPolicyConfig | ResiliencePolicy[T] | None = None,
    circuit_breaker: bool | None = None,
    timeout: float | None = _TIMEOUT_UNSET,
    idempotency_key: str | Callable[[PolicyContext], str] | None = None,
    idempotency_fail_open: bool | None = None,
    idempotency_ttl: timedelta | None = None,
    idempotency_execution_ttl: timedelta | None = None,
    context: PolicyContext | None = None,
) -> T:
    """Async counterpart of ``protect()`` — full CB + Retry + Fallback + DLQ parity.

    This is the async twin of ``protect()`` — the split is intentional (matching
    httpx/redis-py/SQLAlchemy), so await-ness is explicit at the call site rather
    than inferred. Use ``protect()`` for a sync callable, ``aprotect()`` for a
    coroutine; ``@protected`` auto-dispatches between them.

    ``circuit_breaker`` and ``retry`` apply on the async path exactly as in
    ``protect()``: ``None`` resolves against ``ProtectSettings.default_*`` (CB on
    by default, retry off), an explicit ``True`` / ``RetryPolicyConfig`` opts in.
    Composition order mirrors sync — Fallback (outermost) → CB → Timeout →
    Retry — so retry runs its full ``max_attempts`` before the fallback serves,
    one exhausted retry-sequence is a single CB failure, and the timeout bounds
    the whole sequence (a global, not per-attempt, timeout). The fallback is the
    last resort covering retry exhaustion, timeout, AND CB-open. Async
    ``dlq=True`` routes an exhausted failure to the DLQ sink (the async retry
    stage arms ``should_dlq``).

    A pre-built ``TenacityBridgePolicy`` passed as ``retry=`` is auto-converted
    to ``AsyncTenacityBridgePolicy`` and runs under ``tenacity.AsyncRetrying``.
    Any OTHER sync ``ResiliencePolicy`` passed as ``retry=`` raises
    ``NotImplementedError`` (it cannot be safely awaited).

    Idempotency (``idempotency_key`` / ``idempotency_fail_open`` /
    ``idempotency_ttl`` / ``idempotency_execution_ttl``) behaves as in
    ``protect``, driven by an async-native guard/hook that
    ``AsyncPolicyComposer`` awaits directly over ``AsyncIdempotencyGate`` (zero
    thread hop; awaitable ``redis.asyncio`` dedup I/O). Dedup is exactly-once for
    the concurrent case; across a process crash between the side effect and the
    post-execution mark the operation may run again after the gate record's TTL
    expires (an essential at-least-once limit — see ``protect``).
    """
    from baldur.observability.structlog_config import configure_structlog

    configure_structlog()

    from baldur.settings.protect import get_protect_settings

    settings = get_protect_settings()
    if not settings.enabled:
        return await fn()

    _guard_async_unsupported(retry)
    dlq_flag, cb_flag = _resolve_flags(dlq, circuit_breaker)
    timeout_seconds = _resolve_timeout(timeout)
    retry_cfg, retry_policy_obj, _retry_settings_derived = _resolve_retry_stage(
        retry, dlq_requested=dlq_flag, domain=name
    )
    idempotency_stage = _build_async_idempotency_stage(
        name,
        idempotency_key,
        context,
        idempotency_fail_open,
        idempotency_ttl,
        idempotency_execution_ttl,
    )

    composer = _build_async_composer(
        name=name,
        fallback=fallback,
        dlq=dlq_flag,
        retry_cfg=retry_cfg,
        circuit_breaker=cb_flag,
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy_obj,
        idempotency_stage=idempotency_stage,
    )

    start = time.perf_counter()
    result: PolicyResult[T] = await composer.execute(fn, context=context)
    duration = time.perf_counter() - start
    _record_metrics(name, result, duration, mode="async")
    return _finalize_value(result)


async def aprotect_with_meta(
    name: str,
    fn: Callable[[], Awaitable[T]],
    *,
    fallback: Callable[[], Awaitable[T]]
    | Callable[[Exception], Awaitable[T]]
    | None = None,
    dlq: bool | None = None,
    retry: bool | RetryPolicyConfig | ResiliencePolicy[T] | None = None,
    circuit_breaker: bool | None = None,
    timeout: float | None = _TIMEOUT_UNSET,
    idempotency_key: str | Callable[[PolicyContext], str] | None = None,
    idempotency_fail_open: bool | None = None,
    idempotency_ttl: timedelta | None = None,
    idempotency_execution_ttl: timedelta | None = None,
    context: PolicyContext | None = None,
) -> ProtectResult[T]:
    """Non-raising async variant — returns a ``ProtectResult`` DTO.

    The async twin of ``protect_with_meta`` (see ``aprotect`` for the sync/async
    split rationale). CB + Retry + Fallback + DLQ parity matches ``aprotect``: a
    pre-built ``TenacityBridgePolicy`` is auto-converted to
    ``AsyncTenacityBridgePolicy``. The one surviving raise is any OTHER sync
    ``ResiliencePolicy`` passed as ``retry=`` (→ ``NotImplementedError``); the
    raise is kept even in the non-raising variant because it is a programming
    error, not an operational failure, and must surface to the caller.
    ``idempotency_key`` / ``idempotency_fail_open`` behave as in ``aprotect``
    (async-native guard/hook; a dedup-blocked duplicate surfaces as
    ``PolicyOutcome.REJECTED``; ``metadata`` carries ``idempotency_decision`` /
    ``idempotency_unavailable`` as in ``protect_with_meta``).
    """
    from baldur.observability.structlog_config import configure_structlog

    configure_structlog()

    from baldur.settings.protect import get_protect_settings

    settings = get_protect_settings()
    if not settings.enabled:
        start = time.perf_counter()
        try:
            value = await fn()
            return ProtectResult(
                value=value,
                success=True,
                attempts=1,
                duration_seconds=time.perf_counter() - start,
            )
        except Exception as e:
            return ProtectResult(
                value=None,
                success=False,
                attempts=1,
                duration_seconds=time.perf_counter() - start,
                error=e,
                outcome=PolicyOutcome.FAILURE,
            )

    _guard_async_unsupported(retry)
    dlq_flag, cb_flag = _resolve_flags(dlq, circuit_breaker)
    timeout_seconds = _resolve_timeout(timeout)
    retry_cfg, retry_policy_obj, _retry_settings_derived = _resolve_retry_stage(
        retry, dlq_requested=dlq_flag, domain=name
    )
    idempotency_stage = _build_async_idempotency_stage(
        name,
        idempotency_key,
        context,
        idempotency_fail_open,
        idempotency_ttl,
        idempotency_execution_ttl,
    )

    composer = _build_async_composer(
        name=name,
        fallback=fallback,
        dlq=dlq_flag,
        retry_cfg=retry_cfg,
        circuit_breaker=cb_flag,
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy_obj,
        idempotency_stage=idempotency_stage,
    )

    start = time.perf_counter()
    result: PolicyResult[T] = await composer.execute(fn, context=context)
    duration = time.perf_counter() - start
    _record_metrics(name, result, duration, mode="async")
    return _to_protect_result(result, duration)


# =============================================================================
# Decorator forms — thin wrappers over the facade
# =============================================================================

# Sentinel for the ``context_from=`` opt-out (#504 D6). ``False`` is the
# documented sentinel; ``None`` (default) means auto-extract.
_CONTEXT_AUTO_EXTRACT_FIELDS: tuple[str, ...] = ("order_id", "user_id")


def _build_context_from_callsite(
    sig: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    context_from: Callable[..., PolicyContext] | None | Literal[False],
    annotated_primitive: dict[str, bool],
) -> PolicyContext | None:
    """Build a ``PolicyContext`` from the wrapped function's call site.

    Three dispatch modes:
        - ``context_from is False`` → return None (caller passes ``context=None``
          to ``protect()``; opt-out for privacy-sensitive callsites).
        - ``context_from`` is callable → invoke ``context_from(*args, **kwargs)``
          and forward its ``PolicyContext`` return. Raises ``TypeError`` if the
          callable returns a non-``PolicyContext``.
        - ``context_from is None`` (default) → auto-extract. Binds args via
          ``sig.bind_partial(...).apply_defaults()``, writes the consumer-named
          fields (``order_id`` / ``user_id``) to the ``PolicyContext`` named
          fields, and folds every primitive-typed bound arg into a fresh
          ``extra["request_data"]`` dict for full payload visibility in the DLQ.

    Primitive judgment is dual-gate: ``annotated_primitive[name]``
    populated at decoration time short-circuits the runtime check; if the
    annotation was missing / generic, runtime ``isinstance`` against
    ``ALLOWED_PRIMITIVE_TYPES`` is the final gate. Non-primitive args are
    dropped from both named fields and ``request_data``; one DEBUG event per
    drop (``dlq_protect.context_capture_skipped``) keeps the surface in caplog
    without dominating operator logs (the suffix tier from LOGGING_STANDARDS).

    ``sig.bind_partial`` failures (TypeError on truly weird signatures —
    Risk row 2) are caught, logged at WARNING as
    ``dlq_protect.context_capture_failed``, and return an empty
    ``PolicyContext()`` so the pipeline continues fail-open.

    Note: ``sig`` and ``annotated_primitive`` are pre-computed at decoration
    time so the runtime hot path executes only the bind + dict walk.
    """
    # D8: dual-gate primitive judgment. D9: sig + annotation map precomputed
    # at decoration time to keep the runtime hot path bind-only.
    if context_from is False:
        return None
    if callable(context_from):
        result = context_from(*args, **kwargs)
        if not isinstance(result, PolicyContext):
            raise TypeError(
                "context_from callable must return PolicyContext; got "
                f"{type(result).__name__}"
            )
        return result

    try:
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
    except TypeError as exc:
        logger.warning("dlq_protect.context_capture_failed", reason=str(exc))
        return PolicyContext()

    named_fields: dict[str, Any] = {}
    request_data: dict[str, Any] = {}
    for arg_name, value in bound.arguments.items():
        if annotated_primitive.get(arg_name, False):
            is_primitive = True
        else:
            is_primitive = isinstance(value, ALLOWED_PRIMITIVE_TYPES)
        if not is_primitive:
            logger.debug(
                "dlq_protect.context_capture_skipped",
                arg=arg_name,
                type=type(value).__name__,
            )
            continue
        request_data[arg_name] = value
        if arg_name in _CONTEXT_AUTO_EXTRACT_FIELDS:
            named_fields[arg_name] = value

    return PolicyContext(extra={"request_data": request_data}, **named_fields)


def _precompute_signature_cache(
    func: Callable[..., Any],
) -> tuple[inspect.Signature, dict[str, bool]]:
    """Decoration-time cache: signature + per-param annotation primitive map.

    Both are immutable for the lifetime of the decorated function. Keeps
    the runtime hot path free of ``inspect.signature`` walks.
    """
    # D9: immutable per-function cache populated once at decoration time.
    sig = inspect.signature(func)
    annotated_primitive: dict[str, bool] = {
        param_name: is_primitive_annotation(param.annotation)
        for param_name, param in sig.parameters.items()
    }
    return sig, annotated_primitive


def protected(
    name: str,
    *,
    fallback: Callable[[], Any] | Callable[[Exception], Any] | None = None,
    dlq: bool | None = None,
    retry: bool | RetryPolicyConfig | ResiliencePolicy[Any] | None = None,
    circuit_breaker: bool | None = None,
    timeout: float | None = _TIMEOUT_UNSET,
    idempotency_key: str | Callable[[PolicyContext], str] | None = None,
    idempotency_fail_open: bool | None = None,
    idempotency_ttl: timedelta | None = None,
    idempotency_execution_ttl: timedelta | None = None,
    context_from: Callable[..., PolicyContext] | None | Literal[False] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator form of ``protect()``.

    Auto-detects coroutine functions and dispatches to ``aprotect()`` when
    appropriate, so ``@protected(...)`` works uniformly for sync and async
    callables. Arguments passed to the decorated function are forwarded to
    ``fn`` via partial binding.

    Args:
        name: Service identifier — see ``protect()`` docstring.
        fallback: Same as ``protect()``.
        dlq: Same as ``protect()``.
        retry: Same as ``protect()``.
        circuit_breaker: Same as ``protect()``.
        timeout: Same as ``protect()``.
        idempotency_key: Same as ``protect()``. The decorator auto-builds the
            ``PolicyContext`` from the call site, so ``idempotency_key="order_id"``
            reads the wrapped function's ``order_id`` argument. Incompatible with
            ``context_from=False`` (no context to read — raises ``ValueError``).
        idempotency_fail_open: Same as ``protect()`` — cache-error fail
            direction for the dedup gate (``None`` consults the global setting).
        idempotency_ttl: Same as ``protect()`` — dedup memory window for the
            completed-operation record.
        idempotency_execution_ttl: Same as ``protect()`` — in-flight execution
            window; set it >= the wrapped function's worst-case runtime.
        context_from: Controls auto-population of ``PolicyContext`` from the
            wrapped function's bound arguments. ``None`` (default) →
            auto-extract: ``order_id`` / ``user_id`` flow to the named fields,
            every primitive-typed bound arg flows into ``extra["request_data"]``
            so DLQ entries carry searchable business identifiers and the full
            payload snapshot. ``Callable[..., PolicyContext]`` → custom extract.
            ``False`` → skip extraction; pass ``context=None`` to ``protect()``.
            Use the ``False`` sentinel at privacy-sensitive callsites (e.g.,
            ``@protected("auth.verify_password", context_from=False)``).
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        sig, annotated_primitive = _precompute_signature_cache(func)

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> T:
                ctx = _build_context_from_callsite(
                    sig, args, kwargs, context_from, annotated_primitive
                )
                bound = functools.partial(func, *args, **kwargs)
                return await aprotect(  # type: ignore[return-value]
                    name=name,
                    fn=bound,
                    fallback=fallback,
                    dlq=dlq,
                    retry=retry,
                    circuit_breaker=circuit_breaker,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                    idempotency_fail_open=idempotency_fail_open,
                    idempotency_ttl=idempotency_ttl,
                    idempotency_execution_ttl=idempotency_execution_ttl,
                    context=ctx,
                )

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> T:
            ctx = _build_context_from_callsite(
                sig, args, kwargs, context_from, annotated_primitive
            )
            bound = functools.partial(func, *args, **kwargs)
            return protect(
                name=name,
                fn=bound,
                fallback=fallback,
                dlq=dlq,
                retry=retry,
                circuit_breaker=circuit_breaker,
                timeout=timeout,
                idempotency_key=idempotency_key,
                idempotency_fail_open=idempotency_fail_open,
                idempotency_ttl=idempotency_ttl,
                idempotency_execution_ttl=idempotency_execution_ttl,
                context=ctx,
            )

        return sync_wrapper

    return decorator


def aprotected(
    name: str,
    *,
    fallback: Callable[[], Awaitable[Any]]
    | Callable[[Exception], Awaitable[Any]]
    | None = None,
    dlq: bool | None = None,
    retry: bool | RetryPolicyConfig | ResiliencePolicy[Any] | None = None,
    circuit_breaker: bool | None = None,
    timeout: float | None = _TIMEOUT_UNSET,
    idempotency_key: str | Callable[[PolicyContext], str] | None = None,
    idempotency_fail_open: bool | None = None,
    idempotency_ttl: timedelta | None = None,
    idempotency_execution_ttl: timedelta | None = None,
    context_from: Callable[..., PolicyContext] | None | Literal[False] = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Async-only decorator. Use ``@protected`` for mixed sync/async callsites;
    prefer ``@aprotected`` when you want a type-checker error on misuse
    against a sync function.

    ``context_from``, ``idempotency_key``, ``idempotency_fail_open``,
    ``idempotency_ttl``, and ``idempotency_execution_ttl`` behave
    identically to ``@protected`` (async parity). ``circuit_breaker`` and
    ``retry`` apply on the async path just as on the sync one (CB on by default),
    so DLQ entries from async pipelines carry the captured context.
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        if not asyncio.iscoroutinefunction(func):
            raise TypeError(
                f"@aprotected requires a coroutine function; got {func!r}. "
                f"Use @protected instead."
            )
        sig, annotated_primitive = _precompute_signature_cache(func)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            ctx = _build_context_from_callsite(
                sig, args, kwargs, context_from, annotated_primitive
            )
            bound = functools.partial(func, *args, **kwargs)
            return await aprotect(
                name=name,
                fn=bound,
                fallback=fallback,
                dlq=dlq,
                retry=retry,
                circuit_breaker=circuit_breaker,
                timeout=timeout,
                idempotency_key=idempotency_key,
                idempotency_fail_open=idempotency_fail_open,
                idempotency_ttl=idempotency_ttl,
                idempotency_execution_ttl=idempotency_execution_ttl,
                context=ctx,
            )

        return wrapper

    return decorator
