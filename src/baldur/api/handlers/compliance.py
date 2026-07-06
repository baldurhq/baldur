"""
Framework-agnostic Compliance handlers.

Extracted from api/django/views/compliance.py (Phase 2b).

Endpoints:
    GET   /compliance/standards                         List standards
    GET   /compliance/checks                            List all checks
    GET   /compliance/checks/{standard}                 Checks by standard
    POST  /compliance/run                               Run all checks
    POST  /compliance/run/{standard}                    Run standard checks
    GET   /compliance/reports                            List reports
    GET   /compliance/reports/{report_id}               Report detail
    GET   /compliance/reports/{report_id}/evidence/pending   Pending evidence
    PATCH /compliance/reports/{report_id}/checks/{check_id}/review  Review evidence
"""

from __future__ import annotations

from datetime import datetime

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "compliance_standards",
    "compliance_checks",
    "compliance_run",
    "compliance_reports",
    "compliance_report_detail",
    "compliance_pending_evidence",
    "compliance_evidence_review",
]


def _engine():
    # 599 D7 — canary/chaos pattern: the implementation lives in the
    # private distribution; resolve via the registry slot populated by
    # register_dormant_services(). None -> handlers return 503.
    from baldur.factory.registry import ProviderRegistry

    return ProviderRegistry.compliance_engine.safe_get()


def compliance_standards(ctx: RequestContext) -> ResponseContext:
    """GET /compliance/standards — list standards (viewer)."""
    engine = _engine()
    if engine is None:
        return ResponseContext.service_unavailable("Compliance service not available")

    standards = engine.list_standards()
    return ResponseContext.json(
        {"standards": [{"value": s.value, "name": s.name} for s in standards]}
    )


def compliance_checks(ctx: RequestContext) -> ResponseContext:
    """GET /compliance/checks — list checks (viewer)."""
    standard = ctx.get_path_param("standard")
    engine = _engine()
    if engine is None:
        return ResponseContext.service_unavailable("Compliance service not available")

    if standard:
        from baldur.models.compliance import ComplianceStandard

        try:
            std_enum = ComplianceStandard(standard)
        except ValueError:
            return ResponseContext.bad_request(f"Unknown standard: {standard}")
        checks = engine.list_checks(std_enum)
    else:
        checks = engine.list_checks()

    return ResponseContext.json(
        {"checks": [c.to_dict() for c in checks], "total_count": len(checks)}
    )


def compliance_run(ctx: RequestContext) -> ResponseContext:
    """POST /compliance/run — run compliance checks (operator)."""
    standard = ctx.get_path_param("standard")
    engine = _engine()
    if engine is None:
        return ResponseContext.service_unavailable("Compliance service not available")

    from baldur.models.compliance import ComplianceContext, ComplianceStandard

    body = ctx.json_body or {}
    compliance_ctx = ComplianceContext(
        triggered_by="api_manual",
        domain=body.get("domain"),
    )

    if standard:
        try:
            std_enum = ComplianceStandard(standard)
        except ValueError:
            return ResponseContext.bad_request(f"Unknown standard: {standard}")
        try:
            report = engine.run_standard(std_enum, compliance_ctx)
        except ValueError as e:
            return ResponseContext.not_found(str(e))
    else:
        report = engine.run_configured(compliance_ctx)

    return ResponseContext.json(report.to_dict())


def compliance_reports(ctx: RequestContext) -> ResponseContext:
    """GET /compliance/reports — list reports (viewer)."""
    try:
        page = int(ctx.get_query("page", 1))
        page_size = min(int(ctx.get_query("page_size", 20)), 100)
    except (ValueError, TypeError):
        return ResponseContext.bad_request("Invalid page or page_size parameter")

    if page < 1:
        page = 1

    standard_filter = ctx.get_query("standard")
    date_from = ctx.get_query("date_from")
    date_to = ctx.get_query("date_to")

    try:
        from baldur.audit.continuous_audit import get_continuous_audit_recorder
        from baldur.interfaces.audit_adapter import AuditAction

        recorder = get_continuous_audit_recorder()
        records = recorder.query(
            action=AuditAction.COMPLIANCE_CHECK,
            start_time=_parse_iso_datetime(date_from),
            end_time=_parse_iso_datetime(date_to),
            limit=1000,
        )

        if standard_filter:
            records = [
                r
                for r in records
                if standard_filter in r.get("details", {}).get("standards_checked", [])
            ]

        total_count = len(records)
        start = (page - 1) * page_size
        end = start + page_size
        paginated = records[start:end]

        return ResponseContext.json(
            {
                "reports": paginated,
                "total_count": total_count,
                "page": page,
                "page_size": page_size,
            }
        )
    except Exception as e:
        logger.warning("compliance.reports_query_failed", error=str(e))
        return ResponseContext.json(
            {
                "reports": [],
                "total_count": 0,
                "page": page,
                "page_size": page_size,
            }
        )


def compliance_report_detail(ctx: RequestContext) -> ResponseContext:
    """GET /compliance/reports/{report_id} — report detail (viewer)."""
    report_id = ctx.get_path_param("report_id")
    try:
        from baldur.audit.continuous_audit import get_continuous_audit_recorder
        from baldur.interfaces.audit_adapter import AuditAction

        recorder = get_continuous_audit_recorder()
        records = recorder.query(
            action=AuditAction.COMPLIANCE_CHECK,
            limit=1000,
        )
        matched = [
            r for r in records if r.get("details", {}).get("report_id") == report_id
        ]

        if not matched:
            return ResponseContext.not_found("Report not found")

        return ResponseContext.json(
            {
                "report_id": report_id,
                "records": matched,
                "total_count": len(matched),
            }
        )
    except Exception as e:
        logger.warning(
            "compliance.report_detail_failed",
            report_id=report_id,
            error=str(e),
        )
        return ResponseContext.not_found("Report not found")


def compliance_pending_evidence(ctx: RequestContext) -> ResponseContext:
    """GET /compliance/reports/{report_id}/evidence/pending — pending evidence (viewer)."""
    report_id = ctx.get_path_param("report_id")
    engine = _engine()
    if engine is None:
        return ResponseContext.service_unavailable("Compliance service not available")

    pending = engine.get_pending_evidence(report_id)
    return ResponseContext.json(
        {
            "report_id": report_id,
            "pending_evidence": pending,
            "total_count": len(pending),
        }
    )


def compliance_evidence_review(ctx: RequestContext) -> ResponseContext:
    """PATCH /compliance/reports/{report_id}/checks/{check_id}/review — review evidence (operator)."""
    report_id = ctx.get_path_param("report_id")
    check_id = ctx.get_path_param("check_id")
    engine = _engine()
    if engine is None:
        return ResponseContext.service_unavailable("Compliance service not available")

    body = ctx.json_body or {}
    approved = body.get("approved")
    reviewer = body.get("reviewer")
    comment = body.get("comment")

    if approved is None or reviewer is None:
        return ResponseContext.bad_request(
            "Fields 'approved' and 'reviewer' are required"
        )

    try:
        result = engine.review_evidence(
            check_id=check_id,
            report_id=report_id,
            approved=bool(approved),
            reviewer=str(reviewer),
            comment=comment,
        )
        return ResponseContext.json(result.to_dict())
    except ValueError as e:
        return ResponseContext.bad_request(str(e))
