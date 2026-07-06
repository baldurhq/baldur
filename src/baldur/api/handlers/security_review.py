"""
Framework-agnostic security review handler.

Runs the same check functions as the Django management command
(adapters/django/management/commands/security_review.py) but returns
results via ResponseContext instead of stdout.

Endpoints:
    GET /security-review    Run all security checks and return results
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = ["security_review_run"]

REVIEW_VERSION = "1.0.0"


def security_review_run(ctx: RequestContext) -> ResponseContext:
    """GET /security-review — run all security checks (admin)."""
    from baldur.adapters.django.management.commands.security_review import (
        CHECK_FUNCTIONS,
    )

    results = []
    for section_name, check_func in CHECK_FUNCTIONS:
        try:
            section_results = check_func()
            for r in section_results:
                results.append(
                    {
                        "section": section_name,
                        "category": r.category,
                        "name": r.name,
                        "passed": r.passed,
                        "details": r.details,
                    }
                )
        except Exception as e:
            results.append(
                {
                    "section": section_name,
                    "category": "error",
                    "name": f"{section_name} check failed",
                    "passed": False,
                    "details": str(e),
                }
            )

    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])
    total = passed + failed
    pass_rate = (passed / total * 100) if total > 0 else 0

    output_path = ctx.get_query("output")
    if output_path:
        _export_json(output_path, results, pass_rate)

    status_code = 200 if pass_rate >= 70 else 422

    return ResponseContext.json(
        {
            "version": REVIEW_VERSION,
            "timestamp": utc_now().isoformat(),
            "passed": passed,
            "failed": failed,
            "total": total,
            "pass_rate": round(pass_rate, 1),
            "status": (
                "PASSED"
                if pass_rate >= 90
                else "NEEDS_ATTENTION"
                if pass_rate >= 70
                else "FAILED"
            ),
            "results": results,
        },
        status_code=status_code,
    )


def _export_json(path: str, results: list[dict], pass_rate: float) -> None:
    import json

    data = {
        "version": REVIEW_VERSION,
        "date": utc_now().isoformat(),
        "pass_rate": round(pass_rate, 1),
        "results": results,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    logger.info("security_review.exported", path=path)
