"""
Framework-agnostic Chaos Report handlers.

Extracted from api/django/views/chaos/report_views.py (Phase 2b).

Endpoints:
    GET  /chaos/reports              Report list
    GET  /chaos/reports/{report_id}  Report detail
    POST /chaos/reports/generate     Generate daily report
    GET  /chaos/grades/history       Grade history
    POST /chaos/dry-run/analysis     Dry-run analysis
"""

from __future__ import annotations

from datetime import datetime

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "chaos_report_list",
    "chaos_report_detail",
    "chaos_report_generate",
    "chaos_grade_history",
    "chaos_dry_run_analysis",
]


def _report_generator():
    from baldur.factory.registry import ProviderRegistry

    generator = ProviderRegistry.report_generator.safe_get()
    if generator is None:
        raise RuntimeError("Chaos report handlers require baldur_pro ReportGenerator")
    return generator


def _calculate_risk_level(risk_score: float, confidence_score: float) -> str:
    effective_risk = risk_score + (1 - confidence_score) * 0.2
    if effective_risk >= 0.75:
        return "critical"
    if effective_risk >= 0.5:
        return "high"
    if effective_risk >= 0.25:
        return "medium"
    return "low"


def chaos_report_list(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/reports — report list (viewer)."""
    try:
        days = int(ctx.get_query("days", 30))
    except (TypeError, ValueError):
        days = 30

    grade_filter = ctx.get_query("grade")
    generator = _report_generator()
    reports = generator.get_reports(days=days, grade_filter=grade_filter)
    return ResponseContext.json(
        {
            "status": "success",
            "data": [r.to_dict() for r in reports],
            "count": len(reports),
        }
    )


def chaos_report_detail(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/reports/{report_id} — report detail (viewer)."""
    report_id = ctx.get_path_param("report_id")
    generator = _report_generator()

    if len(report_id) == 10 and "-" in report_id:
        report = generator.get_report_by_date(report_id)
    else:
        report = generator.get_report(report_id)

    if not report:
        return ResponseContext.not_found("Report not found")

    return ResponseContext.json({"status": "success", "data": report.to_dict()})


def chaos_report_generate(ctx: RequestContext) -> ResponseContext:
    """POST /chaos/reports/generate — generate daily report (admin)."""
    body = ctx.json_body or {}
    report_date = None

    date_str = body.get("date")
    if date_str:
        try:
            report_date = datetime.fromisoformat(date_str)
        except ValueError:
            return ResponseContext.bad_request(f"Invalid date format: {date_str}")

    generator = _report_generator()
    report = generator.generate_daily_report(report_date=report_date)
    actor = resolve_actor(ctx)
    logger.info(
        "chaos_api.report_generated",
        report=report.report_id,
        request_user=actor,
    )
    return ResponseContext.json({"status": "success", "data": report.to_dict()})


def chaos_grade_history(ctx: RequestContext) -> ResponseContext:
    """GET /chaos/grades/history — grade history (viewer)."""
    try:
        days = int(ctx.get_query("days", 30))
    except (TypeError, ValueError):
        days = 30

    generator = _report_generator()
    history = generator.get_grade_history(days=days)
    return ResponseContext.json({"status": "success", "data": history})


def chaos_dry_run_analysis(ctx: RequestContext) -> ResponseContext:
    """POST /chaos/dry-run/analysis — dry-run analysis (viewer)."""
    body = ctx.json_body or {}
    target_service = body.get("target_service")
    experiment_type = body.get("experiment_type")

    if not target_service or not experiment_type:
        return ResponseContext.bad_request(
            "target_service and experiment_type are required"
        )

    config = body.get("config", {})
    include_blast_radius = body.get("include_blast_radius", True)

    from baldur_pro.services.chaos.impact_predictor import get_impact_predictor

    predictor = get_impact_predictor()
    predicted_outcome = predictor.predict_outcome(
        experiment_type=experiment_type,
        target_service=target_service,
        config=config,
    )
    service_impact = predictor.predict_service_impact(
        target_service=target_service,
        experiment_type=experiment_type,
        config=config,
    )

    blast_radius_analysis = None
    if include_blast_radius:
        from baldur_pro.services.chaos.blast_radius_analyzer import (
            get_blast_radius_analyzer,
        )

        analyzer = get_blast_radius_analyzer()
        blast_radius_analysis = analyzer.analyze(
            target_service=target_service,
            experiment_type=experiment_type,
        )

    requires_approval = predicted_outcome.requires_approval
    approval_level = predicted_outcome.approval_reason
    experiment_allowed = True

    if blast_radius_analysis:
        if blast_radius_analysis.requires_approval:
            requires_approval = True
            if not approval_level:
                approval_level = "blast_radius_threshold_exceeded"
        if not blast_radius_analysis.experiment_allowed:
            experiment_allowed = False

    # PredictedOutcome doesn't expose a direct risk_score; derive from the
    # predicted error-rate-increase fraction (0.0-1.0 scale used by
    # _calculate_risk_level).
    risk_proxy = getattr(
        predicted_outcome,
        "risk_score",
        predicted_outcome.predicted_error_rate_increase_percent / 100.0,
    )
    risk_level = _calculate_risk_level(risk_proxy, predicted_outcome.confidence_score)

    actor = resolve_actor(ctx)
    logger.info(
        "chaos_api.dry_run_analysis_completed",
        target_service=target_service,
        experiment_type=experiment_type,
        risk_level=risk_level,
        request_user=actor,
    )

    result = {
        "status": "success",
        "data": {
            "target_service": target_service,
            "experiment_type": experiment_type,
            "predicted_outcome": predicted_outcome.to_dict(),
            "service_impact": [s.to_dict() for s in service_impact],
            "blast_radius_analysis": (
                blast_radius_analysis.to_dict() if blast_radius_analysis else None
            ),
            "risk_level": risk_level,
            "requires_approval": requires_approval,
            "approval_level": approval_level,
            "experiment_allowed": experiment_allowed,
        },
    }
    return ResponseContext.json(result)
