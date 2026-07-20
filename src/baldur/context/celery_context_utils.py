"""
Unified Celery task context restoration/cleanup.

Consolidates the context restoration logic that was scattered across the
Celery task_prerun/postrun signals into a single utility:
- trace_id / celery_context restore/cleanup (previously in signal_hooks.py
  on_task_prerun)
- causation context restore/cleanup (previously signal_hooks.py
  _setup_causation_context)
- cell_id restore/cleanup (previously celery_cell_propagation.py
  extract_cell_id_on_prerun)
- domain restore/cleanup (OTel Baggage or legacy header)

Token storage:
  Stored on task.request for per-request isolation.
  (The task is a worker-level singleton, but request is a per-execution
  Context object.)

Error policy:
  Per ContextCriticality — CRITICAL is fail-fast, OPTIONAL is fail-open.

Dependency direction:
  signal_hooks.py → celery_context_utils.py (one-way)
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
# Context criticality classification — fail-open / fail-fast policy
# =============================================================================


class ContextCriticality(Enum):
    """Policy applied when context restoration fails."""

    CRITICAL = "critical"  # Failure → abort task (reject/raise)
    IMPORTANT = "important"  # Failure → WARNING log + metric, task continues
    OPTIONAL = "optional"  # Failure → DEBUG log, task continues


# Criticality classification per context.
# cell_id → used by the DB router, cache bulkhead and DLQ partitioning, so a
#   missing value risks cross-tenant contamination
# tenant_id → multi-tenant isolation
# causation → causality tracking; missing breaks tracing but not business
#   behaviour
# trace_id → observability only, no business impact when missing
# domain → tagging only
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
# Token storage structure
# =============================================================================


@dataclass
class TaskContextTokens:
    """
    ContextVar tokens held for the lifetime of a task.

    Tokens are tracked so task_postrun can reset them all in one pass.
    Stored on task.request for per-request isolation.
    """

    cell_id_token: contextvars.Token[str | None] | None = None
    causation_token: contextvars.Token | None = None
    domain_token: contextvars.Token[str | None] | None = None
    actor_token: contextvars.Token | None = None
    baggage_tokens: dict[str, contextvars.Token] = field(default_factory=dict)


# Attribute name used to store the tokens on task.request
_CONTEXT_TOKENS_ATTR = "_baldur_context_tokens"


def _get_task_request(task: Any) -> Any | None:
    """
    Fetch task.request safely.

    When a task is called directly in a unit test, task.request may be an empty
    Context or missing entirely, so handle it defensively.
    """
    if task is None:
        return None
    request = getattr(task, "request", None)
    if request is None:
        return None
    return request


# =============================================================================
# Unified resolvers — Baggage first, legacy fallback, set only once
# =============================================================================


def _resolve_cell_id(task: Any) -> tuple[str | None, str]:
    """
    Resolve cell_id from a single entry point. OTel Baggage first, then legacy.

    Priority:
    1. OTel Baggage (baldur.cell_id)
    2. Legacy custom header (task.request.get("cell_id"))
    3. None (cannot restore)

    Returns:
        (cell_id, source) — source is "baggage" | "legacy_header" | "none"
    """
    # ── Priority 1: OTel Baggage ──
    try:
        from opentelemetry import baggage as otel_baggage

        cell_id_obj = otel_baggage.get_baggage("baldur.cell_id")
        if isinstance(cell_id_obj, str) and cell_id_obj:
            return (cell_id_obj, "baggage")
    except ImportError:
        pass  # OTel not installed
    except Exception:
        pass  # Baggage parsing failed

    # ── Priority 2: legacy custom header ──
    request = _get_task_request(task)
    if request and hasattr(request, "get"):
        cell_id = request.get("cell_id")
        if cell_id:
            return (cell_id, "legacy_header")

    return (None, "none")


def _resolve_domain(task: Any) -> tuple[str | None, str]:
    """
    Resolve domain from a single entry point. OTel Baggage first, then legacy.

    Returns:
        (domain, source) — source is "baggage" | "legacy_header" | "none"
    """
    # ── Priority 1: OTel Baggage ──
    try:
        from opentelemetry import baggage as otel_baggage

        domain_obj = otel_baggage.get_baggage("baldur.domain")
        if isinstance(domain_obj, str) and domain_obj:
            return (domain_obj, "baggage")
    except ImportError:
        pass
    except Exception:
        pass

    # ── Priority 2: legacy custom header ──
    request = _get_task_request(task)
    if request and hasattr(request, "get"):
        domain = request.get("domain")
        if domain:
            return (domain, "legacy_header")

    return (None, "none")


# =============================================================================
# Causation restore/cleanup logic (moved out of signal_hooks.py)
# =============================================================================


# Attribute on task.request holding the causation context token
_CAUSATION_TOKEN_ATTR = "_baldur_causation_token"


def _detect_causation_source(task_name: str) -> str:
    """
    Infer the causation source type from the task name.

    Returns:
        Source string (celery_beat, management_cmd, scheduler, worker)
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
    Restore CausationContext at Celery task start, or create a system cascade.

    Extracts causation info from task.request.headers and sets a
    CausationContext. When the headers are absent (Celery Beat, standalone runs)
    a system cascade is generated automatically. Returns the token so
    TaskContextTokens can track it.
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
    Clean up CausationContext at Celery task end.

    Prevents the previous task's causation context from surviving into a reused
    worker.
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
# Strict mode configuration
# =============================================================================


_strict_cell_context: bool | None = None


def _is_strict_context_enabled() -> bool:
    """
    Read the BALDUR_STRICT_CELL_CONTEXT env var. Result is cached.

    Returns:
        True if a missing CRITICAL context must raise BaldurContextError.
    """
    global _strict_cell_context
    if _strict_cell_context is None:
        import os

        _strict_cell_context = os.environ.get(
            "BALDUR_STRICT_CELL_CONTEXT", "false"
        ).lower() in ("true", "1", "yes", "on")
    return _strict_cell_context


def _reset_strict_cell_context_cache() -> None:
    """Reset the strict_cell_context cache. For test use."""
    global _strict_cell_context
    _strict_cell_context = None


# =============================================================================
# Actor context restore/cleanup
# =============================================================================


def _restore_actor_context(
    task: Any,
    kwargs: dict | None = None,
) -> contextvars.Token | None:
    """
    Restore ActorContext at Celery task start.

    Priority:
    1. kwargs["actor_info"] — explicit override (tests, special cases)
    2. task.request.headers — auto-propagated ActorContext
    3. SYSTEM_ACTOR — fallback (Beat tasks, etc.)

    Args:
        task: Celery task instance
        kwargs: Task kwargs

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
# Main restore function
# =============================================================================


def restore_all_task_context(  # noqa: C901, PLR0912, PLR0915
    task: Any,
    task_id: str,
    task_name: str,
    kwargs: dict | None = None,
) -> TaskContextTokens:
    """
    Restore every ContextVar of a Celery task in one pass.

    Call site: inside on_task_prerun() (signal_hooks.py).
    Replaces the previous per-context handlers.

    Restoration order:
    1. trace_id (kwargs → task_id fallback)           [OPTIONAL]
    2. celery_context                                 [OPTIONAL]
    3. causation (within this module)                 [IMPORTANT]
    4. cell_id (unified resolver — Baggage first)     [CRITICAL]
    5. domain (unified resolver — Baggage first)      [OPTIONAL]
    6. actor (kwargs["actor_info"] → headers first)   [IMPORTANT]

    Args:
        task: Celery task instance (sender)
        task_id: Task ID
        task_name: Task name
        kwargs: Task kwargs

    Returns:
        TaskContextTokens — pass this to cleanup_all_task_context()

    Raises:
        BaldurContextError: when a CRITICAL context fails to restore
            (only when BALDUR_STRICT_CELL_CONTEXT=true)
    """
    tokens = TaskContextTokens()
    request = _get_task_request(task)

    # ── 1. Restore trace_id [OPTIONAL] ──
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

        # Set celery_context
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

    # ── 2. Restore causation [IMPORTANT] ──
    try:
        tokens.causation_token = _setup_causation_context(task, task_id, task_name)
    except Exception as e:
        logger.warning(
            "context_utils.causation_restore_failed",
            error=e,
        )

    # ── 3. Restore cell_id [CRITICAL] — unified resolver ──
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

    # ── 4. Restore domain [OPTIONAL] — unified resolver ──
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
        pass  # Ignore when the domain_tag module is absent
    except Exception as e:
        logger.debug(
            "context_utils.domain_restore_failed",
            error=e,
        )

    # ── 5. Restore actor [IMPORTANT] ──
    try:
        tokens.actor_token = _restore_actor_context(task, kwargs)
    except Exception as e:
        logger.warning(
            "context_utils.actor_restore_failed",
            error=e,
        )

    # ── Store the tokens on task.request (for postrun cleanup) ──
    if request is not None:
        try:
            setattr(request, _CONTEXT_TOKENS_ATTR, tokens)
        except AttributeError:
            logger.debug("context_utils.cannot_store_tokens_task")

    return tokens


# =============================================================================
# Main cleanup function
# =============================================================================


def cleanup_all_task_context(task: Any) -> None:  # noqa: C901, PLR0912
    """
    Clean up every ContextVar at Celery task end in one pass.

    Call site: inside on_task_postrun() (signal_hooks.py).
    Replaces the previous per-context cleanup handlers.
    """
    request = _get_task_request(task)
    tokens: TaskContextTokens | None = (
        getattr(request, _CONTEXT_TOKENS_ATTR, None) if request else None
    )

    # ── 1. Clean up cell_id ──
    if tokens and tokens.cell_id_token:
        try:
            from baldur.context.cell_context import _current_cell_id

            _current_cell_id.reset(tokens.cell_id_token)
        except Exception as e:
            logger.debug(
                "context_utils.cleanup_failed",
                error=e,
            )

    # ── 2. Clean up domain ──
    if tokens and tokens.domain_token:
        try:
            from baldur.decorators.domain_tag import _current_domain

            _current_domain.reset(tokens.domain_token)
        except Exception as e:
            logger.debug(
                "context_utils.domain_cleanup_failed",
                error=e,
            )

    # ── 3. Clean up actor ──
    if tokens and tokens.actor_token:
        try:
            from baldur.context.actor_context import _current_actor

            _current_actor.reset(tokens.actor_token)
        except Exception as e:
            logger.debug(
                "context_utils.actor_cleanup_failed",
                error=e,
            )

    # ── 4. Clean up baggage_tokens in one pass ──
    if tokens and tokens.baggage_tokens:
        for key, _token in tokens.baggage_tokens.items():
            try:
                pass  # Activated once 266 is implemented
            except Exception as e:
                logger.debug(
                    "context_utils.baggage_token_cleanup_failed",
                    context_key=key,
                    error=e,
                )

    # ── 5. Clean up causation ──
    try:
        _cleanup_causation_context(task)
    except Exception as e:
        logger.debug(
            "context_utils.causation_cleanup_failed",
            error=e,
        )

    # ── 6. Clean up trace_id / celery_context ──
    try:
        from baldur.audit.trace import clear_celery_context, clear_trace_id

        clear_trace_id()
        clear_celery_context()
    except Exception as e:
        logger.debug(
            "context_utils.cleanup_failed",
            error=e,
        )

    # ── 7. Clean up the request attribute ──
    if request is not None:
        try:
            delattr(request, _CONTEXT_TOKENS_ATTR)
        except AttributeError:
            pass
