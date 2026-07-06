"""
Framework-agnostic Deployment Policy handlers.

Extracted from api/django/views/error_budget/deployment.py — deployment
verdict, freeze acknowledge, override approval, freeze lift, and active
override check.

Endpoints:
    GET  /deployment-policy/verdict/          Get deployment verdict
    POST /deployment-policy/acknowledge/      Acknowledge freeze
    POST /deployment-policy/override/         Approve override
    POST /deployment-policy/lift/             Lift freeze
    GET  /deployment-policy/active-override/  Check active override

Core Principle: "The system advises, humans decide."
No actual CI/CD blocking — only status queries and decision records.

FAIL-SAFE DESIGN:
    Error Budget system failure -> default PROCEED (fail-open).
    System availability is more important than blocking deployments.
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "deployment_verdict",
    "deployment_freeze_acknowledge",
    "deployment_override",
    "deployment_freeze_lift",
    "deployment_active_override",
]


def _service():
    from baldur.factory.registry import ProviderRegistry

    service = ProviderRegistry.error_budget_service.safe_get()
    if service is None:
        raise RuntimeError(
            "Error budget deployment requires baldur_pro ErrorBudgetService"
        )
    return service


def _override_type_enum():
    from baldur.models.recovery import OverrideType

    return OverrideType


def _failsafe_verdict(error_str: str):
    from baldur_pro.services.error_budget import get_failsafe_verdict_response

    return get_failsafe_verdict_response(error_str)


def deployment_verdict(ctx: RequestContext) -> ResponseContext:
    """GET /deployment-policy/verdict/ — deployment readiness verdict.

    Query Parameters:
        slo_name: SLO name to evaluate (default: "availability")

    Returns deployment verdict including status, can_deploy,
    requires_override, message, and allowed_deployment_types.

    Note: This is an ADVISORY endpoint. It does not block deployments.
    """
    try:
        slo_name = ctx.get_query("slo_name", "availability")

        service = _service()
        verdict = service.get_deployment_verdict(slo_name)

        # Check for active override
        active_override = service.check_active_override()

        response_data = verdict.to_dict()

        if active_override:
            response_data["active_override"] = active_override.to_dict()
            response_data["verdict"]["has_active_override"] = True
        else:
            response_data["verdict"]["has_active_override"] = False

        return ResponseContext.json(
            {
                "status": "success",
                "data": response_data,
                "timestamp": utc_now().isoformat(),
            }
        )

    except Exception as e:
        logger.exception(
            "deployment_policy_api.verdict_failed",
            error=e,
        )
        # FAIL-SAFE: provide default PROCEED response on system failure (200 OK)
        return ResponseContext.json(_failsafe_verdict(str(e)))


def deployment_freeze_acknowledge(ctx: RequestContext) -> ResponseContext:
    """POST /deployment-policy/acknowledge/ — acknowledge deployment freeze.

    Request Body:
        justification: Reason for acknowledging the freeze (required)
    """
    body = ctx.json_body or {}
    justification = body.get("justification", "")

    if not justification:
        return ResponseContext.bad_request(
            "justification is required: Please provide a reason "
            "for acknowledging the freeze."
        )

    service = _service()
    decided_by = resolve_actor(ctx)

    record = service.acknowledge_freeze(
        decided_by=decided_by,
        justification=justification,
    )

    logger.info(
        "deployment_policy.freeze_acknowledged",
        decided_by=decided_by,
        justification=justification,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Deployment freeze has been acknowledged.",
            "data": record.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def deployment_override(ctx: RequestContext) -> ResponseContext:
    """POST /deployment-policy/override/ — approve deployment freeze override.

    Request Body:
        justification:   Reason for the override (required)
        override_type:   hotfix / security_patch / executive_approval / rollback (required)
        deployment_id:   Deployment identifier (optional)
        deployment_name: Deployment name (optional)
        expires_hours:   Override expiry in hours (default: 4)

    IMPORTANT: This is a governance record. It does NOT automatically
    enable deployments. CI/CD systems should check for active overrides.
    """
    body = ctx.json_body or {}
    justification = body.get("justification", "")
    override_type_str = body.get("override_type", "")
    deployment_id = body.get("deployment_id")
    deployment_name = body.get("deployment_name")
    expires_hours = int(body.get("expires_hours", 4))

    # Validation
    OverrideType = _override_type_enum()

    if not justification:
        return ResponseContext.bad_request(
            "justification is required: Please provide a reason for the override."
        )

    if not override_type_str:
        return ResponseContext.bad_request(
            f"override_type is required: Please select an override type. "
            f"valid_types={[t.value for t in OverrideType]}"
        )

    try:
        override_type = OverrideType(override_type_str)
    except ValueError:
        return ResponseContext.bad_request(
            f"Invalid override_type: {override_type_str}, "
            f"valid_types={[t.value for t in OverrideType]}"
        )

    service = _service()
    decided_by = resolve_actor(ctx)

    record = service.approve_override(
        decided_by=decided_by,
        justification=justification,
        override_type=override_type,
        deployment_id=deployment_id,
        deployment_name=deployment_name,
        expires_hours=expires_hours,
    )

    logger.warning(
        "deployment_policy.override_approved",
        decided_by=decided_by,
        override_type=override_type.value,
        deployment_name=deployment_name,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Deployment freeze override has been approved. "
            "This decision is recorded in the audit log.",
            "warning": "Deploying with low error budget carries "
            "additional risk of outages.",
            "data": record.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def deployment_freeze_lift(ctx: RequestContext) -> ResponseContext:
    """POST /deployment-policy/lift/ — lift deployment freeze.

    Request Body:
        justification: Reason for lifting the freeze (required)
    """
    body = ctx.json_body or {}
    justification = body.get("justification", "")

    if not justification:
        return ResponseContext.bad_request(
            "justification is required: Please provide a reason for lifting the freeze."
        )

    service = _service()
    decided_by = resolve_actor(ctx)

    record = service.lift_freeze(
        decided_by=decided_by,
        justification=justification,
    )

    logger.info(
        "deployment_policy.freeze_lifted",
        decided_by=decided_by,
        justification=justification,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": "Deployment freeze has been lifted. "
            "Normal deployments are now allowed.",
            "data": record.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def deployment_active_override(ctx: RequestContext) -> ResponseContext:
    """GET /deployment-policy/active-override/ — check for active override.

    Returns the currently active override, if any.
    CI/CD systems can use this to check if a deployment is allowed
    despite the freeze recommendation.
    """
    service = _service()
    active_override = service.check_active_override()

    if active_override:
        return ResponseContext.json(
            {
                "status": "success",
                "has_active_override": True,
                "data": active_override.to_dict(),
                "timestamp": utc_now().isoformat(),
            }
        )
    return ResponseContext.json(
        {
            "status": "success",
            "has_active_override": False,
            "data": None,
            "timestamp": utc_now().isoformat(),
        }
    )
