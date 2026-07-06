"""
Framework-agnostic dashboard handler.

Extracted from api/django/views/dashboard.py — pure function with
no Django/DRF imports.
"""

from __future__ import annotations

from baldur.interfaces.web_framework import RequestContext, ResponseContext

__all__ = ["dashboard_summary"]


def dashboard_summary(ctx: RequestContext) -> ResponseContext:
    """Get comprehensive dashboard summary statistics."""
    from baldur.services.dashboard_service import get_dashboard_service

    service = get_dashboard_service()
    summary = service.get_summary()
    return ResponseContext.json(summary.to_dict())
