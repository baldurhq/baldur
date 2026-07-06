"""
Audit Hook — 파이프라인 실행 결과를 감사 로그에 기록.

PolicyComposer의 Hook으로 등록하여 파이프라인 전체 결과를
감사 로깅한다. 개별 Policy 내부 이벤트는 관찰하지 않는다
(2계층 Hook 구조).

Fail-Open 원칙: audit 서비스 import/호출 실패 시 로깅만 하고
비즈니스 로직에 영향을 주지 않는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.resilience_policy import PolicyResult

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyContext

logger = structlog.get_logger()


class AuditHook:
    """감사 로깅 훅.

    파이프라인 전체(End-to-End) 결과만 관찰한다.
    개별 Policy 내부 이벤트(Retry 각 시도 등)는 Policy가 처리하며,
    이 Hook에는 전파하지 않는다.
    """

    def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        """실행 시작 시 호출."""
        logger.debug(
            "policy_pipeline.execution_started",
            policy_name=policy_name,
            attempt=attempt,
        )

    def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        """파이프라인 성공 시 호출."""
        logger.info(
            "policy_pipeline.execution_succeeded",
            executed_policies=result.executed_policies,
            total_attempts=result.total_attempts,
            duration_ms=result.total_duration_ms,
        )

    def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        """파이프라인 실패 시 호출."""
        logger.warning(
            "policy_pipeline.execution_failed",
            policy_name=policy_name,
            error=str(error),
            total_attempts=attempt,
        )

    def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        """재시도 예정 시 호출."""
        logger.info(
            "policy_pipeline.retry_scheduled",
            policy_name=policy_name,
            attempt=attempt,
            delay_seconds=delay,
        )

    def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """파이프라인 거부 시 호출."""
        logger.warning(
            "policy_pipeline.execution_rejected",
            guard_name=guard_name,
            reason=reason,
        )
