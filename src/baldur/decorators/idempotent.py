"""``@idempotent`` — atomic dedup decorator over ``IdempotencyGate``.

Wraps a sync or async callable so concurrent or repeated invocations
with the same key short-circuit (raising ``IdempotencyDuplicateError``)
instead of running twice. Uses ``IdempotencyGate.check_and_acquire`` /
``mark_completed`` / ``mark_failed`` for atomic setnx-based delegation.
"""

# Reference: 458 §D1, §D3, §D5, §D6, §D8;
# 595 D1/D2/D7 (per-operation key
# identity, memory/execution window split, injective value escaping).

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.core.exceptions import (
    IdempotencyDuplicateError,
    IdempotencyUnavailableError,
)
from baldur.core.idempotency_gate import (
    IdempotencyCheckResult,
    IdempotencyDecision,
    IdempotencyGate,
)
from baldur.core.types import (
    ALLOWED_PRIMITIVE_TYPES as _ALLOWED_KEY_ARG_TYPES,
)
from baldur.core.types import (
    is_primitive_annotation as _is_primitive_annotation,
)
from baldur.services.idempotency._cache_resolver import (
    resolve_cache_via_registry,
)
from baldur.services.idempotency.models import IdempotencyDomain, IdempotencyKey

__all__ = ["idempotent"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Module-level fallback cache used when ProviderRegistry has no cache adapter
# registered (single-process testbed runs). Provides atomic setnx via internal
# threading.Lock — sufficient for in-process correctness.
_FALLBACK_CACHE = InMemoryCacheAdapter(key_prefix="idempotent_decorator:")


def _resolve_cache_via_registry() -> Any:
    """Resolve a cache adapter via ProviderRegistry with prod-aware fail-closed.

    Thin wrapper over :func:`baldur.services.idempotency._cache_resolver.
    resolve_cache_via_registry` — preserves this symbol so existing tests
    importing ``_resolve_cache_via_registry`` from the decorator module keep
    working. The behavioral asymmetry with :class:`IdempotencyService` lives
    in the shared helper's ``raise_on_prod_no_toggle`` parameter (True here,
    False for the service layer).
    """
    return resolve_cache_via_registry(
        layer="decorator",
        fallback_cache=_FALLBACK_CACHE,
        raise_on_prod_no_toggle=True,
    )


def _escape_key_value(value: str) -> str:
    """Backslash-escape the value-join separator so the key is injective.

    Raw pipe-joining is ambiguous — ``("us|er", "1")`` and ``("us", "er|1")``
    would assemble the same key, a false "already processed" between two
    genuinely different calls. Escaping ``\\`` then ``|`` makes the join
    unambiguous while leaving pipe-free values (the overwhelming common case)
    byte-identical and keys human-readable.
    """
    # Reference: 595 D7.
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _build_key_from_args(
    func: Callable[..., Any],
    args: tuple,
    kwargs: dict,
    key_args: list[str],
    domain: IdempotencyDomain,
    operation: str,
    *,
    annotated_primitive: dict[str, bool],
) -> str:
    """Extract values from bound arguments and assemble the cache key.

    The assembled key is ``idempotency:{domain}:{operation}:{value1|value2|...}``
    — the per-operation component keeps two different operations sharing a
    domain and key values from consuming each other's dedup verdicts.

    Performs runtime primitive validation for parameters whose annotation
    was missing or unrecognized at decoration time.
    """
    sig = inspect.signature(func)
    bound = sig.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    components: dict[str, Any] = {}
    values: list[str] = []
    for name in key_args:
        if name not in bound.arguments:
            raise TypeError(
                f"@idempotent key_args parameter '{name}' was not supplied "
                f"to {func.__qualname__}() and has no default."
            )
        value = bound.arguments[name]
        if not annotated_primitive.get(name, False) and not isinstance(
            value, _ALLOWED_KEY_ARG_TYPES
        ):
            raise TypeError(
                f"@idempotent key_args parameter '{name}' has non-primitive "
                f"value of type {type(value).__name__}; pass key_fn=lambda "
                f"*a, **kw: ... instead."
            )
        components[name] = value
        values.append(_escape_key_value(str(value)))
    raw_key = f"{operation}:{'|'.join(values)}"
    return IdempotencyKey(domain=domain, key=raw_key, components=components).cache_key


def _coerce_key_fn_result(result: Any, domain: IdempotencyDomain) -> str:
    """Convert ``key_fn`` output to a fully-qualified cache key string."""
    if isinstance(result, IdempotencyKey):
        return result.cache_key
    if isinstance(result, str):
        return IdempotencyKey(domain=domain, key=result, components={}).cache_key
    raise TypeError(
        f"@idempotent key_fn must return str or IdempotencyKey; got "
        f"{type(result).__name__}."
    )


def idempotent(  # noqa: C901, PLR0915
    *,
    domain: IdempotencyDomain | str = IdempotencyDomain.CUSTOM,
    key_args: list[str] | None = None,
    key_fn: Callable[..., str | IdempotencyKey] | None = None,
    operation: str | None = None,
    ttl: timedelta | None = None,
    execution_ttl: timedelta | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Atomic do-not-run-twice decorator.

    Args:
        domain: ``IdempotencyDomain`` (or its string value) used to namespace
            the cache key. Defaults to ``IdempotencyDomain.CUSTOM``.
        key_args: List of parameter names to extract from the wrapped function's
            signature. The cache key combines the domain, the per-operation
            component (see ``operation``), and the extracted values — so two
            different operations sharing a domain and key values do not consume
            each other's dedup verdicts.
            Only primitive types are allowed (int/str/bool/UUID/etc.) — see
            ``_ALLOWED_KEY_ARG_TYPES``. Mutually exclusive with ``key_fn``.
        key_fn: ``(*args, **kwargs) -> str | IdempotencyKey`` callable that
            builds the key — the full-control escape hatch (no per-operation
            component is added). Mutually exclusive with ``key_args``.
        operation: Per-operation key component for the ``key_args`` form.
            Defaults to the decorated function's ``module.qualname``, so each
            function gets its own dedup key space with zero configuration.
            CAVEAT: the default identity is code-derived — renaming or moving
            the function resets that operation's dedup memory at the deploy.
            Set ``operation=`` explicitly for correctness-critical operations,
            and give two entry points the same label when they guard one
            logical operation (e.g. an HTTP handler and a worker both guarding
            the same charge). Must not contain ``:`` or ``|`` (the key
            separators). Not accepted with ``key_fn`` (the key is verbatim).
        ttl: Dedup memory window — how long a completed (or failed) operation
            is remembered, i.e. how long duplicates stay blocked after success.
            ``None`` uses the gate memory default
            (``BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS``, 30 minutes unless
            tuned).
        execution_ttl: In-flight execution window — how long a running claim
            is honored before a crashed attempt becomes retryable (and before
            a competing process may take the key over). ``None`` uses the gate
            execution default (30 minutes). Set ``execution_ttl`` >= the
            worst-case runtime of the wrapped function; a value below it risks
            a concurrent duplicate run via stale takeover.

    Returns:
        Decorator that auto-detects sync vs async.

    Raises (at decoration time):
        TypeError: If both ``key_args`` and ``key_fn`` are supplied; or if
            an annotated ``key_args`` parameter is non-primitive; or if neither
            is supplied; or if ``operation`` is empty / contains a separator /
            is combined with ``key_fn``; or if ``ttl`` / ``execution_ttl`` is
            not a positive ``timedelta``.

    Raises (at call time):
        IdempotencyDuplicateError: On SKIP (already completed) or ABORT
            (in-flight collision).
        IdempotencyUnavailableError: When the cache is unavailable (e.g. Redis
            down) during the check and ``fail_open_on_cache_error`` is False
            (the default fail-closed posture); opting in treats the
            unverifiable check as CONTINUE instead.
        TypeError: If an unannotated ``key_args`` value resolves to a
            non-primitive at runtime; or ``key_fn`` returns an unsupported type.

    Usage::

        @idempotent(domain=IdempotencyDomain.EXTERNAL_SERVICE, key_args=["order_id"])
        def charge(order_id: int) -> None:
            ...

        @idempotent(key_fn=lambda payload: payload["request_id"])
        async def handle(payload: dict) -> None:
            ...

        @idempotent(
            domain=IdempotencyDomain.EXTERNAL_SERVICE,
            key_args=["order_id"],
            operation="billing.charge",   # shared label across entry points
            ttl=timedelta(hours=2),       # remember completions for 2 h
            execution_ttl=timedelta(minutes=5),  # worst-case runtime bound
        )
        def charge_from_worker(order_id: int) -> None:
            ...
    """
    if key_args is not None and key_fn is not None:
        raise TypeError("@idempotent: key_args and key_fn are mutually exclusive.")
    if key_args is None and key_fn is None:
        raise TypeError(
            "@idempotent requires either key_args=[...] or key_fn=callable."
        )
    if operation is not None:
        # D1/D7 decoration-time validation: the operation component must keep
        # the assembled key parseable (it sits between the domain and value
        # segments), so the separator characters are rejected outright.
        if key_fn is not None:
            raise TypeError(
                "@idempotent: operation= applies only to the key_args form; "
                "key_fn builds the key verbatim."
            )
        if not isinstance(operation, str) or not operation:
            raise TypeError(
                f"@idempotent operation must be a non-empty str. Got {operation!r}."
            )
        if ":" in operation or "|" in operation:
            raise TypeError(
                "@idempotent operation must not contain the key separator "
                f"characters ':' or '|'. Got {operation!r}."
            )
    for _ttl_label, _ttl_value in (("ttl", ttl), ("execution_ttl", execution_ttl)):
        if _ttl_value is not None and (
            not isinstance(_ttl_value, timedelta) or _ttl_value <= timedelta(0)
        ):
            raise TypeError(
                f"@idempotent {_ttl_label} must be a positive timedelta. "
                f"Got {_ttl_value!r}."
            )

    if isinstance(domain, str):
        try:
            resolved_domain = IdempotencyDomain(domain)
        except ValueError as exc:
            raise TypeError(
                f"@idempotent domain {domain!r} is not a valid IdempotencyDomain value."
            ) from exc
    else:
        resolved_domain = domain

    def decorator(func: Callable[..., T]) -> Callable[..., T]:  # noqa: C901, PLR0915
        # Per-operation key component (D1), computed once at decoration time.
        # Module-qualified (not bare __qualname__) so a cross-module
        # same-qualname pair cannot silently share a key. A functools.wraps-
        # using inner decorator copies __module__/__qualname__ from the
        # original, so stacking resolves to the original function's identity.
        resolved_operation = (
            operation
            if operation is not None
            else f"{func.__module__}.{func.__qualname__}"
        )

        # Decoration-time annotated-parameter validation for key_args.
        annotated_primitive: dict[str, bool] = {}
        if key_args is not None:
            sig = inspect.signature(func)
            for name in key_args:
                if name not in sig.parameters:
                    raise TypeError(
                        f"@idempotent key_args parameter '{name}' not found "
                        f"in {func.__qualname__}() signature."
                    )
                annotation = sig.parameters[name].annotation
                if annotation is inspect.Parameter.empty:
                    annotated_primitive[name] = False
                    continue
                if _is_primitive_annotation(annotation):
                    annotated_primitive[name] = True
                    continue
                # Concrete non-primitive class → reject at decoration time.
                # Unions/generics/forward refs → defer to runtime check.
                if isinstance(annotation, type):
                    raise TypeError(
                        f"@idempotent key_args parameter '{name}' has "
                        f"non-primitive type {annotation!r}; pass "
                        f"key_fn=lambda *a, **kw: ... instead."
                    )
                annotated_primitive[name] = False

        # Lazy gate state — initialized on first wrapper call (D6).
        # Lock-free per D6 rationale: the race is benign, and avoiding the
        # per-call lock is the point.
        gate_state: dict[str, Any] = {"initialized": False, "gate": None}

        def _ensure_gate() -> IdempotencyGate:
            if not gate_state["initialized"]:
                cache = _resolve_cache_via_registry()
                gate = IdempotencyGate(cache=cache)
                gate_state["gate"] = gate
                gate_state["initialized"] = True
            return gate_state["gate"]

        def _check_and_acquire(
            gate: IdempotencyGate, key: str
        ) -> IdempotencyCheckResult:
            """``check_and_acquire`` with the cache-error fail policy.

            Fail CLOSED by default: a cache I/O fault raises
            ``IdempotencyUnavailableError`` (wrapping the raw cache exception so
            a backend-specific error never leaks across the boundary). Fail OPEN
            (treat as CONTINUE) only when the global toggle opts in — so the
            decorator and the ``protect`` facade honor the same posture.
            """
            # execution_ttl (not ttl) bounds the EXECUTING claim — the memory
            # window threads to mark_* instead (595 D2 window decoupling).
            try:
                return gate.check_and_acquire(key, ttl=execution_ttl)
            except Exception as exc:
                # Cached layered read (686 D3/D5): a console edit of the
                # idempotency domain's fail_open_on_cache_error is observed within
                # the read-cache TTL; env base when no manager is registered.
                from baldur.settings.idempotency import IdempotencySettings
                from baldur.settings.layered_provider import (
                    get_layered_settings_cached,
                )

                if get_layered_settings_cached(
                    IdempotencySettings, "idempotency"
                ).fail_open_on_cache_error:
                    return IdempotencyCheckResult(decision=IdempotencyDecision.CONTINUE)
                raise IdempotencyUnavailableError(key=key, error=str(exc)) from exc

        def _mark_completed_fail_open(
            gate: IdempotencyGate, key: str, retry_count: int
        ) -> None:
            """Best-effort ``mark_completed`` — fail-open.

            Once the cache-error fail-open path runs the function against a
            still-down cache, ``mark_completed``'s ``cas_dict_field`` would raise
            on the outage AFTER the function already succeeded — violating
            fail-open. Swallow + log so the guarantee holds, matching the policy
            hook / replay service best-effort mark pattern.
            """
            try:
                gate.mark_completed(key, retry_count=retry_count, ttl=ttl)
            except Exception as exc:
                logger.warning(
                    "idempotency.mark_completed_failed",
                    extra={"key": key, "error": str(exc), "fail_open": True},
                )

        def _mark_failed_fail_open(
            gate: IdempotencyGate, key: str, error: str, retry_count: int
        ) -> None:
            """Best-effort ``mark_failed`` — fail-open.

            Symmetric to :func:`_mark_completed_fail_open`: a mark against a
            down cache must never mask the original function exception that is
            about to re-raise.
            """
            try:
                gate.mark_failed(key, error=error, retry_count=retry_count, ttl=ttl)
            except Exception as exc:
                logger.warning(
                    "idempotency.mark_failed_failed",
                    extra={"key": key, "error": str(exc), "fail_open": True},
                )

        def _resolve_key(args: tuple, kwargs: dict) -> str:
            if key_fn is not None:
                return _coerce_key_fn_result(key_fn(*args, **kwargs), resolved_domain)
            return _build_key_from_args(
                func,
                args,
                kwargs,
                key_args or [],
                resolved_domain,
                resolved_operation,
                annotated_primitive=annotated_primitive,
            )

        def _handle_decision(key: str, decision: IdempotencyDecision) -> None:
            """Log + raise on SKIP/ABORT."""
            if decision is IdempotencyDecision.SKIP:
                logger.warning(
                    "idempotency.duplicate_blocked",
                    extra={
                        "function": func.__qualname__,
                        "key": key,
                        "domain": resolved_domain.value,
                        "decision": "SKIP",
                    },
                )
                raise IdempotencyDuplicateError(
                    key=key,
                    domain=resolved_domain.value,
                    decision="SKIP",
                )
            if decision is IdempotencyDecision.ABORT:
                logger.warning(
                    "idempotency.execution_blocked",
                    extra={
                        "function": func.__qualname__,
                        "key": key,
                        "domain": resolved_domain.value,
                        "decision": "ABORT",
                    },
                )
                raise IdempotencyDuplicateError(
                    "Idempotency in-flight collision: another process is "
                    f"executing key={key!r}.",
                    key=key,
                    domain=resolved_domain.value,
                    decision="ABORT",
                )

        def _toggle_enabled() -> bool:
            from baldur.settings.idempotency import IdempotencySettings
            from baldur.settings.layered_provider import get_layered_settings_cached

            return get_layered_settings_cached(
                IdempotencySettings, "idempotency"
            ).enabled

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if not _toggle_enabled():
                    return await func(*args, **kwargs)
                key = _resolve_key(args, kwargs)
                gate = _ensure_gate()
                check = _check_and_acquire(gate, key)
                if check.decision is not IdempotencyDecision.CONTINUE:
                    _handle_decision(key, check.decision)
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    _mark_failed_fail_open(gate, key, str(exc), check.retry_count)
                    raise
                _mark_completed_fail_open(gate, key, check.retry_count)
                return result

            async_wrapper._reset_cached_gate = lambda: gate_state.update(  # type: ignore[attr-defined]
                {"initialized": False, "gate": None}
            )
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _toggle_enabled():
                return func(*args, **kwargs)
            key = _resolve_key(args, kwargs)
            gate = _ensure_gate()
            check = _check_and_acquire(gate, key)
            if check.decision is not IdempotencyDecision.CONTINUE:
                _handle_decision(key, check.decision)
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                _mark_failed_fail_open(gate, key, str(exc), check.retry_count)
                raise
            _mark_completed_fail_open(gate, key, check.retry_count)
            return result

        sync_wrapper._reset_cached_gate = lambda: gate_state.update(  # type: ignore[attr-defined]
            {"initialized": False, "gate": None}
        )
        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _reset_fallback_cache() -> None:
    """Test helper — clear the module-level fallback InMemoryCacheAdapter
    and the shared resolver's one-shot WARN guard.

    Resetting both in lockstep keeps the decorator test suite's autouse
    cleanup correct: clearing the fallback dict without clearing the
    warned-layers guard would silently drop the WARN on tests that run
    after the first prod-no-adapter test.
    """
    from baldur.services.idempotency._cache_resolver import _reset_warned_layers

    global _FALLBACK_CACHE
    _FALLBACK_CACHE = InMemoryCacheAdapter(key_prefix="idempotent_decorator:")
    _reset_warned_layers()
