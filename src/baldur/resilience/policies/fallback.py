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

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import structlog

from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)

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
            fallback_fn: 단일 fallback 함수.
            default_value: 기본값 (모든 fallback 실패 시).
            fallback_chain: 순차 시도할 fallback 함수 리스트.
            predicate: Fallback 활성화 조건 (기본: outcome이 SUCCESS가 아닌 모든 경우).
            strategy: 기존 FallbackStrategy 구현체 래핑 (과도기 Shim).
                      SimpleFallback, PartitionAwareFallback 등을 래핑할 수 있으나,
                      primary_fn 중복 실행, ABC 계약 위반 등 구조적 문제로
                      완벽한 하위 호환을 보장하지 않는다.
                      네이티브 fallback_chain + predicate 사용을 권장한다.
        """
        self._fallback_fn = fallback_fn
        self._default_value = default_value
        self._fallback_chain = fallback_chain or []
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
        단독 사용 — func 실행 후 실패 시 Fallback 체인 순차 시도.

        ResiliencePolicy Protocol 구현.
        CircuitBreakerPolicy, BulkheadPolicy, RetryPolicy와
        동일한 시그니처(execute(func, *args, context=, **kwargs))를 따른다.

        실행 순서:
        1. func() 실행
        2. 성공 → PolicyResult(SUCCESS) 즉시 반환
        3. 실패 → _apply_fallback(error) 위임
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
        Composer 전용 — func 재실행 없이 Fallback 체인만 시도.

        PolicyComposer._execute_policy_chain()의 fallback_wrapper에서 호출된다.
        이전 Policy 체인에서 이미 실패한 상황이므로 func를 다시 실행하지 않는다.

        CircuitBreakerPolicy, RetryPolicy, BulkheadPolicy는
        execute()만으로 단독/Composer 양쪽 모두 동작하지만,
        FallbackPolicy만 "단독 시 func 실행 + Composer 시 func 미실행"이 필요하다.

        실행 순서:
        1. strategy Shim 시도 (설정된 경우, 과도기)
        2. fallback_chain 순차 시도
        3. fallback_fn 시도
        4. default_value 반환
        5. 모든 실패 → PolicyResult(FAILURE)

        Args:
            original_error: 이전 Policy 체인에서 발생한 원본 예외.
            context: PolicyContext (Guard/Hook/Sink 전파용).

        Returns:
            PolicyResult[T]: Fallback 결과. 예외를 던지지 않는다.
        """
        # Step 1: strategy Shim 시도 (과도기 — 기존 FallbackStrategy 래핑)
        # strategy가 성공적 fallback을 반환하면 즉시 사용한다.
        # strategy가 FAIL_FAST(FAILURE)를 반환하면 네이티브 경로로 진행한다.
        if self._strategy is not None:
            shim_result = self._execute_strategy_shim(original_error)
            if shim_result is not None and shim_result.success:
                return shim_result

        # Step 2: fallback_chain 순차 시도
        for i, fallback in enumerate(self._fallback_chain):
            try:
                result = fallback()
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

        # Step 3: fallback_fn 시도
        if self._fallback_fn is not None:
            try:
                result = self._fallback_fn()
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

        # Step 4: default_value 반환
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

        # Step 5: 모든 fallback 소진
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
    비동기 Fallback Policy — AsyncResiliencePolicy Protocol 구현.

    동기 FallbackPolicy와 동일한 Fallback 체인 로직을 비동기로 제공한다.
    BulkheadPolicy/AsyncBulkheadPolicy 분리 선례와 동일한 패턴으로 별도 클래스.

    소비자 책임(Consumer Responsibility):
    fallback_chain, fallback_fn에 전달하는 함수는 반드시 async def여야 한다.
    동기 Fallback 함수를 혼용하려면 소비자가 asyncio.to_thread()로 래핑하여 주입한다.
    AsyncHedgingStrategy의 candidates 타입(list[Callable[[], Awaitable[T]]])과 동일 원칙.

    두 가지 실행 경로:
    - execute(func): 단독 사용 — async func 실행 후 실패 시 Fallback
    - _apply_fallback(error): AsyncPolicyComposer 전용 — func 재실행 없이 Fallback만 시도
    """

    def __init__(
        self,
        fallback_fn: Callable[[], Awaitable[T]] | None = None,
        default_value: T | None = None,
        fallback_chain: list[Callable[[], Awaitable[T]]] | None = None,
        predicate: Callable[[PolicyResult[T]], bool] | None = None,
    ):
        """
        Args:
            fallback_fn: 단일 비동기 fallback 함수.
            default_value: 기본값 (모든 fallback 실패 시).
            fallback_chain: 순차 시도할 비동기 fallback 함수 리스트.
            predicate: Fallback 활성화 조건 (기본: outcome이 SUCCESS가 아닌 모든 경우).
        """
        self._fallback_fn = fallback_fn
        self._default_value = default_value
        self._fallback_chain = fallback_chain or []
        self._predicate = predicate or self._default_predicate

    @property
    def name(self) -> str:
        """Policy 식별자."""
        return "fallback"

    @staticmethod
    def _default_predicate(result: PolicyResult) -> bool:
        """기본 조건: outcome이 SUCCESS가 아니면 Fallback 활성화."""
        return result.outcome != PolicyOutcome.SUCCESS

    async def execute(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        단독 사용 — 비동기 func 실행 후 실패 시 Fallback.

        AsyncBulkheadPolicy.execute()와 동일한 패턴:
        await func(*args, **kwargs) 호출 후 결과를 PolicyResult로 반환.

        실행 순서:
        1. await func() 실행
        2. 성공 → PolicyResult(SUCCESS) 즉시 반환
        3. 실패 → await _apply_fallback(error) 위임
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
        AsyncPolicyComposer 전용 — 비동기 Fallback 체인만 시도.

        동기 FallbackPolicy._apply_fallback()의 비동기 대응.
        func를 재실행하지 않고 fallback_chain → fallback_fn → default_value만 시도한다.

        Args:
            original_error: 이전 Policy 체인에서 발생한 원본 예외.
            context: PolicyContext (Guard/Hook/Sink 전파용).

        Returns:
            PolicyResult[T]: Fallback 결과. 예외를 던지지 않는다.
        """
        # Step 1: fallback_chain 순차 시도
        for i, fallback in enumerate(self._fallback_chain):
            try:
                result = await fallback()
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

        # Step 2: fallback_fn 시도
        if self._fallback_fn is not None:
            try:
                result = await self._fallback_fn()
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

        # Step 3: default_value 반환
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

        # Step 4: 모든 fallback 소진
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
