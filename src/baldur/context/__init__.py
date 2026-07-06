"""
Context module for tracking who/what is performing actions.

Provides thread-safe, async-safe context tracking for:
- Actor (who is performing the action)
- Request context (web request info)
- Audit context (automatic audit logging)
- Causation context (cascade event causality tracking)

Failure Protection:
- warn_if_untracked(): 추적 안 된 민감 작업 경고
- require_actor_for_action(): 추적 필수 작업에서 강제
- get_actor_for_celery() / restore_actor_from_celery(): Celery task 지원
- set_management_command_actor(): Management command 지원
- get_causation_for_celery() / restore_causation_from_celery(): Cascade 인과관계 전파

Status: Internal
"""

from baldur.context.actor_context import (
    ANONYMOUS_ACTOR,
    SYSTEM_ACTOR,
    Actor,
    ActorContext,
    ActorTrackingWarning,
    get_actor_for_celery,
    get_audit_actor_info,
    require_actor_for_action,
    restore_actor_from_celery,
    set_management_command_actor,
    warn_if_untracked,
)
from baldur.context.causation_context import (  # X-Test Causation ID 프리픽스 관련
    CELERY_HEADER_CASCADE_ID,
    CELERY_HEADER_CHAIN_DEPTH,
    CELERY_HEADER_NAMESPACE,
    CELERY_HEADER_PARENT_EVENT,
    XTEST_CAUSATION_PREFIX,
    CausationContext,
    CausationInfo,
    get_causation_for_celery,
    get_causation_for_kafka,
    is_xtest_id,
    normalize_causation_id,
    restore_causation_from_celery,
    restore_causation_from_kafka,
)
from baldur.context.cell_context import (
    cell_scope,
    get_current_cell_id,
    set_cell_id,
)

__all__ = [
    # Actor context
    "Actor",
    "ActorContext",
    "ActorTrackingWarning",
    "SYSTEM_ACTOR",
    "ANONYMOUS_ACTOR",
    "get_audit_actor_info",
    "warn_if_untracked",
    "require_actor_for_action",
    "get_actor_for_celery",
    "restore_actor_from_celery",
    "set_management_command_actor",
    # Causation context
    "CausationInfo",
    "CausationContext",
    "get_causation_for_celery",
    "restore_causation_from_celery",
    "get_causation_for_kafka",
    "restore_causation_from_kafka",
    "CELERY_HEADER_CASCADE_ID",
    "CELERY_HEADER_PARENT_EVENT",
    "CELERY_HEADER_CHAIN_DEPTH",
    "CELERY_HEADER_NAMESPACE",
    # X-Test Causation ID 프리픽스 관련
    "XTEST_CAUSATION_PREFIX",
    "is_xtest_id",
    "normalize_causation_id",
    # Cell context
    "get_current_cell_id",
    "set_cell_id",
    "cell_scope",
]
