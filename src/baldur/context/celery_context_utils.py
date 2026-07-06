"""
Celery 태스크 컨텍스트 통합 복원/정리.

Celery task_prerun/postrun 시그널에서 분산되어 있던 컨텍스트 복원 로직을
단일 유틸리티로 통합한다:
- trace_id / celery_context 복원/정리 (기존 signal_hooks.py on_task_prerun 내부)
- causation context 복원/정리 (기존 signal_hooks.py _setup_causation_context)
- cell_id 복원/정리 (기존 celery_cell_propagation.py extract_cell_id_on_prerun)
- domain 복원/정리 (OTel Baggage 또는 레거시 헤더)

토큰 저장:
  task.request에 저장하여 요청별 격리.
  (task는 워커 내 싱글톤이지만 request는 실행 요청별 Context 객체)

에러 정책:
  ContextCriticality에 따라 CRITICAL은 Fail-Fast, OPTIONAL은 Fail-Open.

의존 방향:
  signal_hooks.py → celery_context_utils.py (단방향)
  celery_context_utils.py → audit/trace.py, context/causation_context.py,
                             context/cell_context.py, decorators/domain_tag.py
"""

from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

from baldur.core.exceptions import BaldurError
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# 컨텍스트 중요도 분류 — Fail-Open / Fail-Fast 정책
# =============================================================================


class ContextCriticality(Enum):
    """컨텍스트 복원 실패 시 정책."""

    CRITICAL = "critical"  # 실패 → 태스크 중단 (Reject/Raise)
    IMPORTANT = "important"  # 실패 → WARNING 로그 + 메트릭 증가, 태스크 계속
    OPTIONAL = "optional"  # 실패 → DEBUG 로그, 태스크 계속


# 컨텍스트별 중요도 분류.
# cell_id → DB 라우터, 캐시 격벽, DLQ 파티셔닝에 사용 → 누락 시 크로스-테넌트 오염 가능
# tenant_id → 멀티테넌트 격리
# causation → 인과관계 추적, 누락 시 추적 불가하나 비즈니스 동작에 영향 없음
# trace_id → 관측용, 누락 시 비즈니스 영향 없음
# domain → 태깅용
CONTEXT_CRITICALITY: dict[str, ContextCriticality] = {
    "cell_id": ContextCriticality.CRITICAL,
    "tenant_id": ContextCriticality.CRITICAL,
    "causation": ContextCriticality.IMPORTANT,
    "actor": ContextCriticality.IMPORTANT,
    "trace_id": ContextCriticality.OPTIONAL,
    "domain": ContextCriticality.OPTIONAL,
}


class BaldurContextError(BaldurError):
    """
    Critical context restoration failure — task execution is aborted.

    This exception is classified as non-retryable. Running a task without
    isolation contexts like cell_id/tenant_id risks cross-tenant data
    contamination, so retrying would produce the same result.

    Celery integration:
    - Automatically registered in dont_autoretry_for via setup_baldur_signals()
      to prevent infinite retries.
    """

    def __init__(self, context_name: str, task_name: str, detail: str = ""):
        self.context_name = context_name
        self.task_name = task_name
        super().__init__(
            f"Critical context '{context_name}' restoration failed for task "
            f"'{task_name}'. Task rejected to prevent data contamination. {detail}"
        )

    def extra_context(self) -> dict:
        ctx = super().extra_context()
        ctx["context_name"] = self.context_name
        ctx["task_name"] = self.task_name
        return ctx


# =============================================================================
# 토큰 저장 구조
# =============================================================================


@dataclass
class TaskContextTokens:
    """
    태스크 수명 동안 보관할 ContextVar 토큰 모음.

    task_postrun에서 일괄 정리(reset)하기 위해 토큰을 추적한다.
    task.request에 저장하여 요청별 격리.
    """

    cell_id_token: contextvars.Token[str | None] | None = None
    causation_token: contextvars.Token | None = None
    domain_token: contextvars.Token[str | None] | None = None
    actor_token: contextvars.Token | None = None
    baggage_tokens: dict[str, contextvars.Token] = field(default_factory=dict)


# task.request에 토큰 저장용 속성명
_CONTEXT_TOKENS_ATTR = "_baldur_context_tokens"


def _get_task_request(task: Any) -> Any | None:
    """
    task.request를 안전하게 가져온다.

    단위 테스트에서 task()처럼 직접 호출 시 task.request가 빈 Context이거나
    존재하지 않을 수 있으므로 방어적으로 처리한다.
    """
    if task is None:
        return None
    request = getattr(task, "request", None)
    if request is None:
        return None
    return request


# =============================================================================
# 통합 리졸버 — Baggage 우선, Legacy Fallback, 1회만 Set
# =============================================================================


def _resolve_cell_id(task: Any) -> tuple[str | None, str]:
    """
    cell_id를 단일 진입점에서 결정. OTel Baggage 우선, Legacy Fallback.

    우선순위:
    1. OTel Baggage (baldur.cell_id)
    2. Legacy 커스텀 헤더 (task.request.get("cell_id"))
    3. None (복원 불가)

    Returns:
        (cell_id, source) — source는 "baggage" | "legacy_header" | "none"
    """
    # ── 1순위: OTel Baggage ──
    try:
        from opentelemetry import baggage as otel_baggage

        cell_id_obj = otel_baggage.get_baggage("baldur.cell_id")
        if isinstance(cell_id_obj, str) and cell_id_obj:
            return (cell_id_obj, "baggage")
    except ImportError:
        pass  # OTel 미설치 환경
    except Exception:
        pass  # Baggage 파싱 실패

    # ── 2순위: Legacy 커스텀 헤더 ──
    request = _get_task_request(task)
    if request and hasattr(request, "get"):
        cell_id = request.get("cell_id")
        if cell_id:
            return (cell_id, "legacy_header")

    return (None, "none")


def _resolve_domain(task: Any) -> tuple[str | None, str]:
    """
    domain을 단일 진입점에서 결정. OTel Baggage 우선, Legacy Fallback.

    Returns:
        (domain, source) — source는 "baggage" | "legacy_header" | "none"
    """
    # ── 1순위: OTel Baggage ──
    try:
        from opentelemetry import baggage as otel_baggage

        domain_obj = otel_baggage.get_baggage("baldur.domain")
        if isinstance(domain_obj, str) and domain_obj:
            return (domain_obj, "baggage")
    except ImportError:
        pass
    except Exception:
        pass

    # ── 2순위: Legacy 커스텀 헤더 ──
    request = _get_task_request(task)
    if request and hasattr(request, "get"):
        domain = request.get("domain")
        if domain:
            return (domain, "legacy_header")

    return (None, "none")


# =============================================================================
# Causation 복원/정리 로직 (signal_hooks.py에서 이관)
# =============================================================================


# Causation 컨텍스트 token 저장용 (task.request 속성)
_CAUSATION_TOKEN_ATTR = "_baldur_causation_token"


def _detect_causation_source(task_name: str) -> str:
    """
    Task 이름에서 causation source 유형 추론.

    Returns:
        source 문자열 (celery_beat, management_cmd, scheduler, worker)
    """
    task_name_lower = task_name.lower()

    if any(pattern in task_name_lower for pattern in ["beat", "schedule", "periodic"]):
        return "celery_beat"

    if any(pattern in task_name_lower for pattern in ["manage", "command", "admin"]):
        return "management_cmd"

    if any(pattern in task_name_lower for pattern in ["cron", "cleanup", "expire"]):
        return "scheduler"

    return "worker"


def _setup_causation_context(
    task: Any,
    task_id: str,
    task_name: str,
) -> contextvars.Token | None:
    """
    Celery Task 시작 시 Causation Context 자동 복원 또는 시스템 Cascade 생성.

    task.request.headers에서 causation 정보를 추출하여 CausationContext를 설정한다.
    헤더가 없는 경우 (Celery Beat, 독립 실행 등) 시스템 Cascade를 자동 생성한다.
    token을 반환하여 TaskContextTokens에서 추적 가능하게 한다.
    """
    try:
        from baldur.context.causation_context import (
            CELERY_HEADER_CASCADE_ID,
            CELERY_HEADER_CHAIN_DEPTH,
            CELERY_HEADER_NAMESPACE,
            CELERY_HEADER_PARENT_EVENT,
            CausationInfo,
            _current_causation,
        )

        request = _get_task_request(task)
        if not request:
            return None

        headers = getattr(request, "headers", None) or {}
        cascade_id = headers.get(CELERY_HEADER_CASCADE_ID)

        if cascade_id:
            info = CausationInfo(
                cascade_id=cascade_id,
                parent_event_id=headers.get(CELERY_HEADER_PARENT_EVENT, ""),
                chain_depth=int(headers.get(CELERY_HEADER_CHAIN_DEPTH, "0")) + 1,
                namespace=headers.get(CELERY_HEADER_NAMESPACE, "global"),
                metadata={
                    "restored_from": "celery_signal",
                    "restored_at": utc_now().isoformat(),
                    "task_id": task_id,
                    "task_name": task_name,
                },
            )
        else:
            source = _detect_causation_source(task_name)
            info = CausationInfo(
                cascade_id=f"cascade-{uuid.uuid4().hex[:12]}",
                parent_event_id=f"SYSTEM_ROOT_{source}_{uuid.uuid4().hex[:8]}",
                chain_depth=0,
                namespace="global",
                metadata={
                    "system_source": source,
                    "auto_generated": True,
                    "task_id": task_id,
                    "task_name": task_name,
                    "created_at": utc_now().isoformat(),
                },
            )

        token = _current_causation.set(info)
        setattr(request, _CAUSATION_TOKEN_ATTR, token)
        return token

    except ImportError:
        return None
    except Exception as e:
        logger.debug(
            "context_utils.causation_setup_failed",
            error=e,
        )
        return None


def _cleanup_causation_context(task: Any) -> None:
    """
    Celery Task 종료 시 Causation Context 정리.

    Worker 재사용 시 이전 Task의 causation 컨텍스트 잔존 방지.
    """
    try:
        from baldur.context.causation_context import _current_causation

        request = _get_task_request(task)
        if not request:
            return

        token = getattr(request, _CAUSATION_TOKEN_ATTR, None)
        if token:
            _current_causation.reset(token)
            try:
                delattr(request, _CAUSATION_TOKEN_ATTR)
            except AttributeError:
                pass

    except ImportError:
        pass
    except Exception as e:
        logger.debug(
            "context_utils.causation_cleanup_failed",
            error=e,
        )


# =============================================================================
# Strict Mode 설정
# =============================================================================


_strict_cell_context: bool | None = None


def _is_strict_context_enabled() -> bool:
    """
    BALDUR_STRICT_CELL_CONTEXT 환경변수 확인. 캐싱 포함.

    Returns:
        True이면 CRITICAL 컨텍스트 누락 시 BaldurContextError 발생.
    """
    global _strict_cell_context
    if _strict_cell_context is None:
        import os

        _strict_cell_context = os.environ.get(
            "BALDUR_STRICT_CELL_CONTEXT", "false"
        ).lower() in ("true", "1", "yes", "on")
    return _strict_cell_context


def _reset_strict_cell_context_cache() -> None:
    """strict_cell_context 캐시를 초기화한다. 테스트 용도."""
    global _strict_cell_context
    _strict_cell_context = None


# =============================================================================
# Actor Context 복원/정리
# =============================================================================


def _restore_actor_context(
    task: Any,
    kwargs: dict | None = None,
) -> contextvars.Token | None:
    """
    Celery Task 시작 시 ActorContext 복원.

    우선순위:
    1. kwargs["actor_info"] — 명시적 override (테스트, 특수 케이스)
    2. task.request.headers — 자동 전파된 ActorContext
    3. SYSTEM_ACTOR — fallback (Beat 태스크 등)

    Args:
        task: Celery task 인스턴스
        kwargs: 태스크 kwargs

    Returns:
        ContextVar token for cleanup, or None
    """
    try:
        import json

        from baldur.context.actor_context import (
            CELERY_HEADER_ACTOR_ID,
            CELERY_HEADER_ACTOR_IP,
            CELERY_HEADER_ACTOR_ROLES,
            CELERY_HEADER_ACTOR_SESSION,
            CELERY_HEADER_ACTOR_SOURCE,
            CELERY_HEADER_ACTOR_TYPE,
            _current_actor,
        )

        # Priority 1: kwargs["actor_info"] explicit override
        actor_info = kwargs.get("actor_info") if kwargs else None
        if actor_info:
            return _restore_actor_from_dict(actor_info)

        # Priority 2: headers (auto-propagated)
        request = _get_task_request(task)
        if request:
            headers = getattr(request, "headers", None) or {}
            actor_id = headers.get(CELERY_HEADER_ACTOR_ID)
            if actor_id:
                roles_json = headers.get(CELERY_HEADER_ACTOR_ROLES, "[]")
                try:
                    roles = json.loads(roles_json)
                except (json.JSONDecodeError, TypeError):
                    roles = []

                from baldur.context.actor_context import Actor

                actor = Actor(
                    actor_id=actor_id,
                    actor_type=headers.get(CELERY_HEADER_ACTOR_TYPE, "celery"),
                    source=headers.get(CELERY_HEADER_ACTOR_SOURCE, "celery"),
                    ip_address=headers.get(CELERY_HEADER_ACTOR_IP),
                    session_id=headers.get(CELERY_HEADER_ACTOR_SESSION),
                    roles=roles,
                )
                token = _current_actor.set(actor)
                logger.debug(
                    "context_utils.actor_restored_from_headers",
                    actor_id=actor_id,
                    actor_type=actor.actor_type,
                )
                return token

        # Priority 3: SYSTEM_ACTOR fallback (no token needed - default)
        return None

    except ImportError:
        return None
    except Exception as e:
        logger.debug(
            "context_utils.actor_restore_failed",
            error=e,
        )
        return None


def _restore_actor_from_dict(actor_info: dict) -> contextvars.Token | None:
    """Restore ActorContext from actor_info dict (kwargs override)."""
    try:
        from baldur.context.actor_context import Actor, _current_actor

        if not actor_info:
            return None

        actor = Actor(
            actor_id=actor_info.get("actor_id", "unknown"),
            actor_type=actor_info.get("actor_type", "celery"),
            source=actor_info.get("source", "celery"),
            ip_address=actor_info.get("ip_address"),
            session_id=actor_info.get("session_id"),
            roles=actor_info.get("roles", []),
        )
        token = _current_actor.set(actor)
        logger.debug(
            "context_utils.actor_restored_from_kwargs",
            actor_id=actor.actor_id,
            actor_type=actor.actor_type,
        )
        return token

    except Exception as e:
        logger.debug(
            "context_utils.actor_restore_from_dict_failed",
            error=e,
        )
        return None


# =============================================================================
# 메인 복원 함수
# =============================================================================


def restore_all_task_context(  # noqa: C901, PLR0912, PLR0915
    task: Any,
    task_id: str,
    task_name: str,
    kwargs: dict | None = None,
) -> TaskContextTokens:
    """
    Celery 태스크의 모든 ContextVar를 일괄 복원.

    호출 위치: on_task_prerun() 내부 (signal_hooks.py)
    기존 개별 핸들러를 대체한다.

    복원 순서:
    1. trace_id (kwargs → task_id fallback)           [OPTIONAL]
    2. celery_context                                 [OPTIONAL]
    3. causation (본 모듈 내부)                        [IMPORTANT]
    4. cell_id (통합 리졸버 — Baggage 우선)            [CRITICAL]
    5. domain (통합 리졸버 — Baggage 우선)             [OPTIONAL]
    6. actor (kwargs["actor_info"] → headers 우선)   [IMPORTANT]

    Args:
        task: Celery task 인스턴스 (sender)
        task_id: 태스크 ID
        task_name: 태스크 이름
        kwargs: 태스크 kwargs

    Returns:
        TaskContextTokens — cleanup_all_task_context()에 전달

    Raises:
        BaldurContextError: CRITICAL 컨텍스트 복원 실패 시
            (BALDUR_STRICT_CELL_CONTEXT=true인 경우에만)
    """
    tokens = TaskContextTokens()
    request = _get_task_request(task)

    # ── 1. trace_id 복원 [OPTIONAL] ──
    try:
        from baldur.audit.trace import (
            _celery_context_var,
            generate_celery_trace_id,
            set_trace_id,
        )

        trace_info = kwargs.get("trace_info") if kwargs else None
        if trace_info and trace_info.get("trace_id"):
            set_trace_id(trace_info["trace_id"])
        else:
            set_trace_id(generate_celery_trace_id(task_id))

        # celery_context 설정
        retries = getattr(request, "retries", 0) if request else 0
        _celery_context_var.set(
            {
                "task_id": task_id,
                "task_name": task_name,
                "retries": retries,
            }
        )
    except Exception as e:
        logger.debug(
            "context_utils.restore_failed",
            error=e,
        )

    # ── 2. causation 복원 [IMPORTANT] ──
    try:
        tokens.causation_token = _setup_causation_context(task, task_id, task_name)
    except Exception as e:
        logger.warning(
            "context_utils.causation_restore_failed",
            error=e,
        )

    # ── 3. cell_id 복원 [CRITICAL] — 통합 리졸버 ──
    try:
        cell_id, source = _resolve_cell_id(task)
        if cell_id:
            from baldur.context.cell_context import _current_cell_id

            tokens.cell_id_token = _current_cell_id.set(cell_id)
            logger.debug(
                "context_utils.restored",
                cell_id=cell_id,
                source=source,
            )
        elif _is_strict_context_enabled():
            raise BaldurContextError(
                context_name="cell_id",
                task_name=task_name,
                detail="Neither OTel Baggage nor legacy header provided cell_id. "
                "Set BALDUR_STRICT_CELL_CONTEXT=false to disable.",
            )
    except BaldurContextError:
        raise
    except Exception as e:
        if _is_strict_context_enabled():
            raise BaldurContextError(
                context_name="cell_id",
                task_name=task_name,
                detail=str(e),
            ) from e
        logger.debug(
            "context_utils.restore_failed",
            error=e,
        )

    # ── 4. domain 복원 [OPTIONAL] — 통합 리졸버 ──
    # 545 chokepoint 5: route through set_domain_context() so OTel baggage /
    # legacy-header injected values inherit validation + fallback.
    try:
        domain, domain_source = _resolve_domain(task)
        if domain:
            from baldur.decorators.domain_tag import set_domain_context

            tokens.domain_token = set_domain_context(domain)
            logger.debug(
                "context_utils.domain_restored",
                healing_domain=domain,
                domain_source=domain_source,
            )
    except ImportError:
        pass  # domain_tag 모듈 미존재 시 무시
    except Exception as e:
        logger.debug(
            "context_utils.domain_restore_failed",
            error=e,
        )

    # ── 5. actor 복원 [IMPORTANT] ──
    try:
        tokens.actor_token = _restore_actor_context(task, kwargs)
    except Exception as e:
        logger.warning(
            "context_utils.actor_restore_failed",
            error=e,
        )

    # ── 토큰을 task.request에 저장 (postrun 정리용) ──
    if request is not None:
        try:
            setattr(request, _CONTEXT_TOKENS_ATTR, tokens)
        except AttributeError:
            logger.debug("context_utils.cannot_store_tokens_task")

    return tokens


# =============================================================================
# 메인 정리 함수
# =============================================================================


def cleanup_all_task_context(task: Any) -> None:  # noqa: C901, PLR0912
    """
    Celery 태스크 종료 시 모든 ContextVar 일괄 정리.

    호출 위치: on_task_postrun() 내부 (signal_hooks.py)
    기존 개별 정리 핸들러를 대체한다.
    """
    request = _get_task_request(task)
    tokens: TaskContextTokens | None = (
        getattr(request, _CONTEXT_TOKENS_ATTR, None) if request else None
    )

    # ── 1. cell_id 정리 ──
    if tokens and tokens.cell_id_token:
        try:
            from baldur.context.cell_context import _current_cell_id

            _current_cell_id.reset(tokens.cell_id_token)
        except Exception as e:
            logger.debug(
                "context_utils.cleanup_failed",
                error=e,
            )

    # ── 2. domain 정리 ──
    if tokens and tokens.domain_token:
        try:
            from baldur.decorators.domain_tag import _current_domain

            _current_domain.reset(tokens.domain_token)
        except Exception as e:
            logger.debug(
                "context_utils.domain_cleanup_failed",
                error=e,
            )

    # ── 3. actor 정리 ──
    if tokens and tokens.actor_token:
        try:
            from baldur.context.actor_context import _current_actor

            _current_actor.reset(tokens.actor_token)
        except Exception as e:
            logger.debug(
                "context_utils.actor_cleanup_failed",
                error=e,
            )

    # ── 4. baggage_tokens 일괄 정리 ──
    if tokens and tokens.baggage_tokens:
        for key, _token in tokens.baggage_tokens.items():
            try:
                pass  # 266 구현 시 활성화
            except Exception as e:
                logger.debug(
                    "context_utils.baggage_token_cleanup_failed",
                    context_key=key,
                    error=e,
                )

    # ── 5. causation 정리 ──
    try:
        _cleanup_causation_context(task)
    except Exception as e:
        logger.debug(
            "context_utils.causation_cleanup_failed",
            error=e,
        )

    # ── 6. trace_id / celery_context 정리 ──
    try:
        from baldur.audit.trace import clear_celery_context, clear_trace_id

        clear_trace_id()
        clear_celery_context()
    except Exception as e:
        logger.debug(
            "context_utils.cleanup_failed",
            error=e,
        )

    # ── 7. request 속성 정리 ──
    if request is not None:
        try:
            delattr(request, _CONTEXT_TOKENS_ATTR)
        except AttributeError:
            pass
