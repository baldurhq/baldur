"""
Context module for tracking who/what is performing actions.

Provides thread-safe, async-safe context tracking for:
- Actor (who is performing the action)
- Request context (web request info)
- Audit context (automatic audit logging)
- Causation context (cascade event causality tracking)

Failure Protection:
- warn_if_untracked(): warns on untracked sensitive operations
- require_actor_for_action(): enforces tracking where it is mandatory
- get_actor_for_celery() / restore_actor_from_celery(): Celery task support
- set_management_command_actor(): management command support
- get_causation_for_celery() / restore_causation_from_celery(): cascade
  causality propagation

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
from baldur.context.causation_context import (  # X-Test causation ID prefix helpers
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
    # X-Test causation ID prefix helpers
    "XTEST_CAUSATION_PREFIX",
    "is_xtest_id",
    "normalize_causation_id",
    # Cell context
    "get_current_cell_id",
    "set_cell_id",
    "cell_scope",
]
