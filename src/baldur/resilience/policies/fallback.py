"""
Fallback Policy — 실패 시 대체 응답 제공.

현재 3곳에 분산된 Fallback 로직을 통합하는 순수 Policy 구현:
- core/fallback_strategy.py의 FallbackStrategy ABC + 3개 구현체
- services/circuit_breaker/service.py의 should_allow_with_fallback()
- resilience/bulkhead/decorator.py의 @bulkhead(fallback=...)

기존 FallbackStrategy 구현체를 래핑하지 않고,
네이티브 fallback_chain + predicate 기반으로 새로 작성한다.
RetryPolicy가 기존 RetryHandler를 재사용하지 않은 선례와 동일하다.

구성:
- FallbackPolicy: 동기 Fallback (ResiliencePolicy Protocol 구현)
- AsyncFallbackPolicy: 비동기 Fallback (AsyncResiliencePolicy Protocol 구현)
- partition_aware_chain(): PartitionState Provider 기반 동적 fallback chain 생성기
- _FALLBACK_MODE_TO_OUTCOME: FallbackMode → PolicyOutcome 하위 호환 매핑

두 가지 실행 경로:
- execute(func): 단독 사용 — func 실행 후 실패 시 Fallback
- _apply_fallback(error): Composer 전용 — func 재실행 없이 Fallback만 시도
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import structlog

from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)
from baldur.resilience.policies.composer import _classify_exception_outcome

__GENERIC = (
    Generic  # placeholder so ruff doesn't strip the import before class defs use it
)

if TYPE_CHECKING:
    from baldur.core.connection_health import PartitionState
    from baldur.core.fallback_strategy import (
        FallbackResult,
        FallbackStrategy,
    )

logger = structlog.get_logger()

T = TypeVar("T")

# Bounded so a per-call lambda fallback (the composer is NOT cached when a
# fallback is set) cannot grow the cache without bound; module-level callables
# — the common case — stay resident and pay the signature walk once process-wide.
_FALLBACK_ARITY_CACHE_SIZE = 1024


@functools.lru_cache(maxsize=_FALLBACK_ARITY_CACHE_SIZE)
def _fallback_accepts_error(fn: Callable[..., Any]) -> bool:
    """Return True if the fallback callable takes the caught error positionally.

    Resolved once per callable identity (memoized). Call shapes:

    - zero required positional (and no ``*args``) → legacy ``fb()``;
    - exactly one required positional, or ``*args``, or ``**kwargs``-only →
      error-accepting ``fb(error)``;
    - >= 2 required positional → ``ValueError`` at construction (fail loud, not a
      runtime ``TypeError`` mid-incident);
    - signature-uninspectable (C builtins, exotic ``__call__``, ``Mock``) →
      legacy ``fb()`` — any signature-parse failure degrades safely to zero-arg
      rather than raising mid-construction.
    """
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False

    required_positional = 0
    has_var_positional = False
    has_var_keyword = False
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            if param.default is inspect.Parameter.empty:
                required_positional += 1
        elif param.kind is inspect.Parameter.VAR_POSITIONAL:
            has_var_positional = True
        elif param.kind is inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True

    if required_positional >= 2:
        raise ValueError(
            f"Fallback callable {getattr(fn, '__name__', fn)!r} declares "
            f"{required_positional} required positional parameters; a fallback "
            f"takes either zero arguments or one (the caught error)."
        )
    if required_positional == 1:
        return True
    return has_var_positional or has_var_keyword


# =============================================================================
# FallbackMode → PolicyOutcome 하위 호환 매핑
# =============================================================================

# FallbackMode(str, Enum) 값을 키로 사용하여 런타임 import 없이 매핑한다.
# core/fallback_strategy.py의 FallbackMode 값과 1:1 대응한다.
_FALLBACK_MODE_TO_OUTCOME: dict[str, PolicyOutcome] = {
    "fail_fast": PolicyOutcome.FAILURE,
    "use_cache": PolicyOutcome.SUCCESS_WITH_FALLBACK,
    "use_default": PolicyOutcome.SUCCESS_WITH_FALLBACK,
    "degrade": PolicyOutcome.SUCCESS_WITH_FALLBACK,
    "retry_alt": PolicyOutcome.SUCCESS_WITH_FALLBACK,
    "hedge": PolicyOutcome.SUCCESS_WITH_FALLBACK,
}


# =============================================================================
# FallbackPolicy — 동기 Fallback Policy
# =============================================================================


class FallbackPolicy(ResiliencePolicy[T], Generic[T]):
    """
    동기 Fallback Policy — 실패 시 대체 응답 제공.

    순수 fallback_chain + predicate 기반.
    Kill Switch, ErrorBudgetGate, Audit, DLQ 등 외부 관심사는
    PolicyComposer의 Guard/Hook/Sink가 처리한다.

    두 가지 실행 경로:
    - execute(func): 단독 사용 — func 실행 후 실패 시 Fallback
    - _apply_fallback(error): Composer 전용 — func 재실행 없이 Fallback만 시도

    예외 처리 컨트랙트:
    - execute()는 모든 예외를 흡수하여 PolicyResult로 반환한다.
    - FallbackPolicy는 Policy 체인의 마지막 보루이므로 예외를 재전파하지 않는다.
    - KeyboardInterrupt/SystemExit는 except Exception 패턴으로 자동 통과한다.
    """

    def __init__(
        self,
        fallback_fn: Callable[[], T] | None = None,
        default_value: T | None = None,
        fallback_chain: list[Callable[[], T]] | None = None,
        predicate: Callable[[PolicyResult[T]], bool] | None = None,
        strategy: FallbackStrategy | None = None,
    ):
        """
        Args:
            fallback_fn: Single fallback callable. Takes either zero arguments
                (legacy ``fb()``) or one positional (``fb(error)`` — the caught
                exception); arity is detected once at construction.
            default_value: Default value (used when every fallback fails).
            fallback_chain: Ordered list of fallback callables to try in turn.
                Each entry follows the same zero-arg / one-arg contract.
            predicate: Fallback activation condition (default: any non-SUCCESS
                outcome).
            strategy: Wraps an existing FallbackStrategy implementation
                (transitional shim). Can wrap SimpleFallback,
                PartitionAwareFallback, etc., but does not guarantee full
                backward compatibility due to structural issues (primary_fn
                double-execution, ABC contract violations). Prefer the native
                fallback_chain + predicate.

        Raises:
            ValueError: A fallback callable declares >= 2 required positional
                parameters (fail loud at construction, not mid-incident).
        """
        self._fallback_fn = fallback_fn
        self._fallback_fn_accepts_error = (
            _fallback_accepts_error(fallback_fn) if fallback_fn is not None else False
        )
        self._default_value = default_value
        self._fallback_chain = fallback_chain or []
        self._chain_accepts_error = [
            _fallback_accepts_error(fb) for fb in self._fallback_chain
        ]
        self._predicate = predicate or self._default_predicate
        self._strategy = strategy

    @property
    def name(self) -> str:
        """Policy 식별자."""
        return "fallback"

    @staticmethod
    def _default_predicate(result: PolicyResult) -> bool:
        """기본 조건: outcome이 SUCCESS가 아니면 Fallback 활성화."""
        return result.outcome != PolicyOutcome.SUCCESS

    def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Standalone use — run func, and on failure try the fallback chain.

        Implements the ResiliencePolicy Protocol with the same signature
        (execute(func, *args, context=, **kwargs)) as CircuitBreakerPolicy,
        BulkheadPolicy, and RetryPolicy.

        Execution order:
        1. Run func().
        2. Success → return PolicyResult(SUCCESS) immediately.
        3. Failure → consult the predicate against the classified outcome; if it
           declines, return a FAILURE result carrying the original error;
           otherwise delegate to _apply_fallback(error).
        """
        try:
            result = func(*args, **kwargs)
            return PolicyResult(
                value=result,
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=["fallback"],
                metadata={"fallback_used": False},
            )
        except Exception as primary_error:
            check_result: PolicyResult[T] = PolicyResult(
                value=None,
                outcome=_classify_exception_outcome(primary_error),
                error=primary_error,
            )
            if not self._predicate(check_result):
                return PolicyResult(
                    value=None,
                    outcome=PolicyOutcome.FAILURE,
                    error=primary_error,
                    executed_policies=["fallback"],
                    metadata={"fallback_used": False},
                )
            return self._apply_fallback(
                original_error=primary_error,
                context=context,
            )

    def _apply_fallback(
        self,
        original_error: Exception,
        context: PolicyContext | None = None,
    ) -> PolicyResult[T]:
        """
        Composer-only — try the fallback chain without re-running func.

        Called from PolicyComposer._execute_policy_chain()'s fallback_wrapper.
        The prior policy chain has already failed, so func is not re-run.

        CircuitBreakerPolicy, RetryPolicy, and BulkheadPolicy work both
        standalone and inside the composer via execute() alone; only
        FallbackPolicy needs "run func standalone, skip func inside the
        composer".

        Execution order:
        1. Try the strategy shim (if configured, transitional).
        2. Try the fallback_chain in turn.
        3. Try fallback_fn.
        4. Return default_value.
        5. All failed → PolicyResult(FAILURE).

        Each error-accepting fallback (arity detected at construction) receives
        ``original_error`` positionally so it can branch on the failure type.

        Args:
            original_error: The original exception from the prior policy chain.
            context: PolicyContext (propagated to Guard/Hook/Sink).

        Returns:
            PolicyResult[T]: The fallback result. Never raises.
        """
        # Step 1: strategy shim (transitional — wraps an existing
        # FallbackStrategy). If the shim returns a successful fallback, use it
        # immediately; a FAIL_FAST (FAILURE) result falls through to the native
        # path below.
        if self._strategy is not None:
            shim_result = self._execute_strategy_shim(original_error)
            if shim_result is not None and shim_result.success:
                return shim_result

        # Step 2: try the fallback_chain in turn.
        for i, fallback in enumerate(self._fallback_chain):
            try:
                result = (
                    fallback(original_error)
                    if self._chain_accepts_error[i]
                    else fallback()
                )
                return PolicyResult(
                    value=result,
                    outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                    executed_policies=["fallback"],
                    metadata={
                        "fallback_used": True,
                        "fallback_index": i,
                        "original_error": str(original_error),
                    },
                )
            except Exception as e:
                logger.warning(
                    "fallback.chain_failed",
                    fallback_attempt_index=i,
                    error=e,
                )
                continue

        # Step 3: try fallback_fn.
        if self._fallback_fn is not None:
            try:
                result = (
                    self._fallback_fn(original_error)
                    if self._fallback_fn_accepts_error
                    else self._fallback_fn()
                )
                return PolicyResult(
                    value=result,
                    outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                    executed_policies=["fallback"],
                    metadata={
                        "fallback_used": True,
                        "fallback_source": "fallback_fn",
                        "original_error": str(original_error),
                    },
                )
            except Exception as e:
                logger.warning(
                    "fallback.function_failed",
                    error=e,
                )

        # Step 4: return default_value.
        if self._default_value is not None:
            return PolicyResult(
                value=self._default_value,
                outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                executed_policies=["fallback"],
                metadata={
                    "fallback_used": True,
                    "fallback_source": "default_value",
                    "original_error": str(original_error),
                },
            )

        # Step 5: every fallback exhausted.
        return PolicyResult(
            value=None,
            outcome=PolicyOutcome.FAILURE,
            error=original_error,
            executed_policies=["fallback"],
            metadata={"fallback_used": True, "all_fallbacks_exhausted": True},
        )

    def _execute_strategy_shim(
        self,
        original_error: Exception,
    ) -> PolicyResult[T] | None:
        """
        기존 FallbackStrategy 구현체를 통한 과도기 Fallback 시도.

        strategy.execute()에 예외를 던지는 더미 함수를 primary_fn으로 주입하여
        fallback 경로를 유도한다.

        구조적 제약:
        - SimpleFallback: primary_fn 실패 → fallback_fn/default_value 경로 동작
        - PartitionAwareFallback: primary_fn 실패 → _handle_failure() 경로 동작
        - CacheFirstFallback: primary_fn을 무시하므로 cache_fn이 항상 먼저 실행됨

        Returns:
            PolicyResult[T] | None: 변환된 결과, 또는 실패 시 None (네이티브 경로로 진행).
        """
        try:
            # 원본 예외를 재발생시키는 더미 함수로 fallback 경로 유도
            def failing_primary() -> T:
                raise original_error

            fallback_result = self._strategy.execute(  # type: ignore[union-attr]
                primary_fn=failing_primary,
            )
            return self._convert_fallback_result(fallback_result, original_error)
        except Exception as e:
            logger.debug(
                "strategy.shim_failed_falling",
                error=e,
            )
            return None

    @staticmethod
    def _convert_fallback_result(
        fallback_result: FallbackResult,
        original_error: Exception,
    ) -> PolicyResult[T]:
        """
        FallbackResult → PolicyResult 변환.

        FallbackMode 값을 _FALLBACK_MODE_TO_OUTCOME 매핑으로 PolicyOutcome에 대응하고,
        FallbackMode 정보를 metadata["fallback_mode"]에 보존한다.
        """
        fallback_mode_value = (
            fallback_result.fallback_mode.value
            if fallback_result.fallback_mode is not None
            else None
        )
        outcome = _FALLBACK_MODE_TO_OUTCOME.get(
            fallback_mode_value or "",
            PolicyOutcome.SUCCESS_WITH_FALLBACK
            if fallback_result.used_fallback
            else PolicyOutcome.SUCCESS,
        )

        # FAIL_FAST인 경우 FAILURE로 매핑
        if fallback_result.used_fallback and fallback_mode_value == "fail_fast":
            outcome = PolicyOutcome.FAILURE

        metadata: dict[str, Any] = {
            "fallback_used": fallback_result.used_fallback,
            "strategy_shim": True,
        }
        if fallback_mode_value is not None:
            metadata["fallback_mode"] = fallback_mode_value
        if fallback_result.original_error is not None:
            metadata["original_error"] = fallback_result.original_error

        return PolicyResult(
            value=fallback_result.value,
            outcome=outcome,
            error=original_error if outcome == PolicyOutcome.FAILURE else None,
            executed_policies=["fallback"],
            metadata=metadata,
        )


# =============================================================================
# AsyncFallbackPolicy — 비동기 Fallback Policy
# =============================================================================


class AsyncFallbackPolicy(Generic[T]):
    """
    Async Fallback Policy — implements the AsyncResiliencePolicy Protocol.

    Provides the same fallback-chain logic as the sync FallbackPolicy,
    asynchronously. A separate class following the BulkheadPolicy /
    AsyncBulkheadPolicy split precedent.

    Consumer responsibility:
    Callables passed to fallback_chain / fallback_fn MUST be ``async def``. To
    mix in a sync fallback, the consumer wraps it with ``asyncio.to_thread()``
    before injecting. Same principle as AsyncHedgingStrategy's ``candidates``
    type (``list[Callable[[], Awaitable[T]]]``).

    Two execution paths:
    - execute(func): standalone — run the async func, fall back on failure.
    - _apply_fallback(error): AsyncPolicyComposer-only — try the fallback chain
      without re-running func.
    """

    def __init__(
        self,
        fallback_fn: Callable[..., Awaitable[T]] | None = None,
        default_value: T | None = None,
        fallback_chain: list[Callable[..., Awaitable[T]]] | None = None,
        predicate: Callable[[PolicyResult[T]], bool] | None = None,
    ):
        """
        Args:
            fallback_fn: Single async fallback callable. Zero-arg (``fb()``) or
                one positional (``fb(error)``); arity detected at construction.
            default_value: Default value (used when every fallback fails).
            fallback_chain: Ordered list of async fallback callables, same
                zero-arg / one-arg contract.
            predicate: Fallback activation condition (default: any non-SUCCESS).

        Raises:
            ValueError: A fallback callable declares >= 2 required positional
                parameters (fail loud at construction).
        """
        self._fallback_fn = fallback_fn
        self._fallback_fn_accepts_error = (
            _fallback_accepts_error(fallback_fn) if fallback_fn is not None else False
        )
        self._default_value = default_value
        self._fallback_chain = fallback_chain or []
        self._chain_accepts_error = [
            _fallback_accepts_error(fb) for fb in self._fallback_chain
        ]
        self._predicate = predicate or self._default_predicate

    @property
    def name(self) -> str:
        """Policy identifier."""
        return "fallback"

    @staticmethod
    def _default_predicate(result: PolicyResult) -> bool:
        """Default condition: activate the fallback on any non-SUCCESS outcome."""
        return result.outcome != PolicyOutcome.SUCCESS

    async def execute(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Standalone use — run the async func, fall back on failure.

        Same pattern as AsyncBulkheadPolicy.execute(): await func(*args,
        **kwargs) and return the outcome as a PolicyResult.

        Execution order:
        1. await func().
        2. Success → return PolicyResult(SUCCESS) immediately.
        3. Failure → consult the predicate against the classified outcome; if it
           declines, return a FAILURE result carrying the original error;
           otherwise await _apply_fallback(error).
        """
        try:
            result = await func(*args, **kwargs)
            return PolicyResult(
                value=result,
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=["fallback"],
                metadata={"fallback_used": False},
            )
        except Exception as primary_error:
            check_result: PolicyResult[T] = PolicyResult(
                value=None,
                outcome=_classify_exception_outcome(primary_error),
                error=primary_error,
            )
            if not self._predicate(check_result):
                return PolicyResult(
                    value=None,
                    outcome=PolicyOutcome.FAILURE,
                    error=primary_error,
                    executed_policies=["fallback"],
                    metadata={"fallback_used": False},
                )
            return await self._apply_fallback(
                original_error=primary_error,
                context=context,
            )

    async def _apply_fallback(
        self,
        original_error: Exception,
        context: PolicyContext | None = None,
    ) -> PolicyResult[T]:
        """
        AsyncPolicyComposer-only — try the async fallback chain.

        The async counterpart of sync FallbackPolicy._apply_fallback(). Does not
        re-run func; tries fallback_chain → fallback_fn → default_value. Each
        error-accepting fallback receives ``original_error`` positionally.

        Args:
            original_error: The original exception from the prior policy chain.
            context: PolicyContext (propagated to Guard/Hook/Sink).

        Returns:
            PolicyResult[T]: The fallback result. Never raises.
        """
        # Step 1: try the fallback_chain in turn.
        for i, fallback in enumerate(self._fallback_chain):
            try:
                result = await (
                    fallback(original_error)
                    if self._chain_accepts_error[i]
                    else fallback()
                )
                return PolicyResult(
                    value=result,
                    outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                    executed_policies=["fallback"],
                    metadata={
                        "fallback_used": True,
                        "fallback_index": i,
                        "original_error": str(original_error),
                    },
                )
            except Exception as e:
                logger.warning(
                    "async.fallback_chain_failed",
                    fallback_attempt_index=i,
                    error=e,
                )
                continue

        # Step 2: try fallback_fn.
        if self._fallback_fn is not None:
            try:
                result = await (
                    self._fallback_fn(original_error)
                    if self._fallback_fn_accepts_error
                    else self._fallback_fn()
                )
                return PolicyResult(
                    value=result,
                    outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                    executed_policies=["fallback"],
                    metadata={
                        "fallback_used": True,
                        "fallback_source": "fallback_fn",
                        "original_error": str(original_error),
                    },
                )
            except Exception as e:
                logger.warning(
                    "async.fallback_function_failed",
                    error=e,
                )

        # Step 3: return default_value.
        if self._default_value is not None:
            return PolicyResult(
                value=self._default_value,
                outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                executed_policies=["fallback"],
                metadata={
                    "fallback_used": True,
                    "fallback_source": "default_value",
                    "original_error": str(original_error),
                },
            )

        # Step 4: every fallback exhausted.
        return PolicyResult(
            value=None,
            outcome=PolicyOutcome.FAILURE,
            error=original_error,
            executed_policies=["fallback"],
            metadata={"fallback_used": True, "all_fallbacks_exhausted": True},
        )


# =============================================================================
# partition_aware_chain — PartitionState Provider 기반 동적 fallback chain
# =============================================================================


def partition_aware_chain(
    state_provider: Callable[[], PartitionState],
    cache_fn: Callable[[], T] | None = None,
    db_fn: Callable[[], T] | None = None,
) -> list[Callable[[], T]]:
    """
    PartitionState Provider 기반 동적 fallback chain 생성.

    각 fallback lambda가 실행되는 시점에 state_provider()를 호출하여
    최신 PartitionState를 조회한다. 이로써 생성 시점의 상태 고정(Stale) 문제를 방지한다.

    기존 PartitionAwareFallback의 PartitionState 직접 주입 + update_partition_state()
    수동 갱신 방식을 대체한다.

    Args:
        state_provider: 실행 시점마다 최신 PartitionState를 반환하는 공급자 함수.
                        예: lambda: connection_health_monitor.get_state()
        cache_fn: 캐시에서 데이터를 조회하는 함수.
        db_fn: DB에서 데이터를 조회하는 함수.

    Returns:
        FallbackPolicy.fallback_chain에 전달할 callable 리스트.
        각 callable은 실행 시점에 PartitionState 가용성을 실시간 체크한다.

    Usage::

        fallback = FallbackPolicy(
            fallback_chain=partition_aware_chain(
                state_provider=lambda: health_monitor.get_state(),
                cache_fn=lambda: redis.get("product:123"),
                db_fn=lambda: Product.objects.get(id=123),
            ),
            default_value={"status": "degraded"},
        )

    CB 독립 캐시 조회 패턴::

        FallbackPolicy는 CircuitBreaker 상태와 독립적으로 모든 예외에서 작동한다.
        (fallback.py execute()는 try/except Exception으로 모든 예외를 잡아
        _apply_fallback()에 위임하며, CB 상태를 확인하지 않는다.)

        반면 StaleCacheStore의 should_allow_with_fallback()은 cb_state를 필수로 받아
        CLOSED 상태에서는 캐시를 사용하지 않는다.

        partition_aware_chain()의 cache_fn으로 StaleCacheStore 조회를 넣으면
        CB CLOSED 상태에서의 일시적 실패에도 캐시 조회가 가능하다:

        from baldur.services.circuit_breaker.stale_cache_integration import (
            CanaryWithStaleCacheService,
            get_canary_stale_cache_service,
        )

        stale_service = get_canary_stale_cache_service()
        cache_key = CanaryWithStaleCacheService.build_stale_cache_key("product", "123")

        FallbackPolicy(
            fallback_chain=partition_aware_chain(
                state_provider=lambda: health_monitor.get_state(),
                cache_fn=lambda: stale_service._cache.get(cache_key).value,
                db_fn=lambda: Product.objects.get(id=123),
            ),
            default_value={"status": "degraded"},
        )
    """
    chain: list[Callable[[], T]] = []

    if cache_fn is not None:

        def _cache_fallback() -> T:
            ps = state_provider()
            if ps.cache_available:
                return cache_fn()  # type: ignore[return-value]
            raise RuntimeError("Cache unavailable at fallback execution time")

        chain.append(_cache_fallback)

    if db_fn is not None:

        def _db_fallback() -> T:
            ps = state_provider()
            if ps.db_available:
                return db_fn()  # type: ignore[return-value]
            raise RuntimeError("DB unavailable at fallback execution time")

        chain.append(_db_fallback)

    return chain
