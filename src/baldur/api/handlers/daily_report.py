"""
Framework-agnostic daily report handlers.

Extracted from api/django/views/daily_report.py — pure functions with
no Django/DRF imports. Covers the full Phase 2a surface for the
Daily Report feature (428 Phase 4, D8).

Data source:
    Per-date state backend keys ``baldur:daily_reports:{YYYY-MM-DD}`` — written by
    ``DailyReportService._persist_report()`` on each daily report generation.
    Each handler iterates the exact set of dates it needs and calls
    ``backend.get()`` per date. ``backend.get_all()`` is intentionally not used
    (see 430 D2) because FileStateBackend's pattern matching is
    broken for colon-containing prefixes.

Backend error handling:
    ``_get_state_backend_or_none`` catches singleton init failure (rare after
    the first successful boot). Mid-request ``backend.get()`` exceptions are
    raised as ``_BackendUnavailable`` by the loaders so handlers can surface
    them as 503 — distinguishing "backend down" from "key absent" (404).

Endpoints (paths mirror the Django URL conf):
    GET /reports/daily/                   List recent reports (summary)
    GET /reports/daily/trend/             Trend series + aggregates
    GET /reports/daily/{date}/            Single date (summary or detail)
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.services.daily_report.service import _STATE_BACKEND_KEY_PREFIX
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "daily_report_list",
    "daily_report_detail",
    "daily_report_trend",
]

_DEFAULT_TREND_METRICS = (
    "archived_count",
    "circuit_transitions",
    "dlq_new_entries_count",
    "dlq_pending_count",
    "task_failures",
)


class _BackendUnavailable(Exception):
    """Raised by loaders when the backend raises mid-request.

    Handlers catch this to return 503, distinguishing backend failure from
    legitimately-absent keys (which surface as None/empty and map to 404/empty).
    """


def _get_state_backend_or_none():
    """Return the configured state backend or None on singleton init failure."""
    try:
        from baldur.core.state_backend import get_state_backend

        return get_state_backend()
    except Exception as e:
        logger.warning("daily_report_api.load_failed", error=e)
        return None


def _window_start(today: date_type, days: int) -> date_type:
    """Start of the ``days``-wide window ending (inclusive) at ``today``."""
    return today - timedelta(days=days - 1)


def _load_report_for_date(backend: Any, date_str: str) -> dict[str, Any] | None:
    """Load a single per-date report. None when absent.

    Raises ``_BackendUnavailable`` on backend error so handlers can surface 503.
    """
    try:
        return backend.get(f"{_STATE_BACKEND_KEY_PREFIX}{date_str}")
    except Exception as e:
        logger.warning("daily_report_api.load_failed", error=e)
        raise _BackendUnavailable from e


def _load_reports_in_window(
    backend: Any, today: date_type, days: int
) -> dict[str, dict[str, Any]]:
    """Load reports for the exact ``days``-wide window ending at ``today``.

    Missing dates are skipped. Backend errors raise ``_BackendUnavailable``.
    """
    start_date = _window_start(today, days)
    result: dict[str, dict[str, Any]] = {}
    for i in range(days):
        date_str = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            report_dict = backend.get(f"{_STATE_BACKEND_KEY_PREFIX}{date_str}")
        except Exception as e:
            logger.warning("daily_report_api.load_failed", error=e)
            raise _BackendUnavailable from e
        if report_dict:
            result[date_str] = report_dict
    return result


def _load_recent_reports(
    backend: Any, today: date_type, days: int, keep_days: int
) -> dict[str, dict[str, Any]]:
    """Load up to ``days`` most recent reports scanning backward from ``today``.

    Iterates at most ``keep_days`` dates and stops early once ``days`` reports
    have been collected. Backend errors raise ``_BackendUnavailable``.
    """
    result: dict[str, dict[str, Any]] = {}
    for i in range(keep_days):
        if len(result) >= days:
            break
        date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            report_dict = backend.get(f"{_STATE_BACKEND_KEY_PREFIX}{date_str}")
        except Exception as e:
            logger.warning("daily_report_api.load_failed", error=e)
            raise _BackendUnavailable from e
        if report_dict:
            result[date_str] = report_dict
    return result


def _backend_unavailable_response() -> ResponseContext:
    return ResponseContext.json(
        {"status": "error", "message": "State backend unavailable"},
        status_code=503,
    )


def _get_keep_days() -> int:
    try:
        from baldur.settings.daily_report import get_daily_report_settings

        return get_daily_report_settings().keep_reports_days
    except Exception:
        return 90


def _strip_entries(report_dict: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in report_dict.items() if k != "entries"}


def _parse_days(raw: str | None, default: int) -> int:
    keep_days = _get_keep_days()
    if raw is None:
        return min(default, keep_days)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    if n < 1:
        n = 1
    return min(n, keep_days)


def _aggregate(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min": 0, "max": 0, "avg": 0.0, "p95": 0}
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    p95_idx = max(0, min(n - 1, int(round(0.95 * (n - 1)))))
    return {
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "avg": round(sum(sorted_vals) / n, 4),
        "p95": sorted_vals[p95_idx],
    }


def daily_report_list(ctx: RequestContext) -> ResponseContext:
    """List recent persisted daily reports (summary view)."""
    keep_days = _get_keep_days()
    days = _parse_days(ctx.get_query("days"), default=7)

    backend = _get_state_backend_or_none()
    if backend is None:
        return _backend_unavailable_response()

    today = utc_now().date()
    try:
        reports = _load_recent_reports(backend, today, days=days, keep_days=keep_days)
    except _BackendUnavailable:
        return _backend_unavailable_response()

    if not reports:
        return ResponseContext.json({"status": "success", "count": 0, "data": []})

    sorted_dates = sorted(reports.keys(), reverse=True)
    data = [_strip_entries(reports[d]) for d in sorted_dates]

    return ResponseContext.json({"status": "success", "count": len(data), "data": data})


def daily_report_detail(ctx: RequestContext) -> ResponseContext:
    """Return a single persisted report by date."""
    date = ctx.get_path_param("date", "")
    if not date:
        return ResponseContext.json(
            {"status": "error", "message": "Missing date path parameter"},
            status_code=400,
        )

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return ResponseContext.json(
            {
                "status": "error",
                "message": "Invalid date format, expected YYYY-MM-DD",
            },
            status_code=400,
        )

    backend = _get_state_backend_or_none()
    if backend is None:
        return _backend_unavailable_response()

    try:
        report_dict = _load_report_for_date(backend, date)
    except _BackendUnavailable:
        return _backend_unavailable_response()

    if report_dict is None:
        return ResponseContext.json(
            {"status": "error", "message": "Report not found for date"},
            status_code=404,
        )

    detail_requested = ctx.get_query("detail", "").lower() in ("1", "true", "yes")
    payload = report_dict if detail_requested else _strip_entries(report_dict)
    return ResponseContext.json({"status": "success", "data": payload})


def daily_report_trend(ctx: RequestContext) -> ResponseContext:
    """Return metric trend data over a date range."""
    days = _parse_days(ctx.get_query("days"), default=7)
    metrics_raw = ctx.get_query("metrics")
    if metrics_raw:
        metrics = [m.strip() for m in metrics_raw.split(",") if m.strip()]
    else:
        metrics = list(_DEFAULT_TREND_METRICS)

    backend = _get_state_backend_or_none()
    if backend is None:
        return _backend_unavailable_response()

    today = utc_now().date()
    start_date = _window_start(today, days)
    try:
        relevant = _load_reports_in_window(backend, today, days)
    except _BackendUnavailable:
        return _backend_unavailable_response()

    series: dict[str, list[dict[str, Any]]] = {m: [] for m in metrics}
    for date_key in sorted(relevant.keys()):
        report_dict = relevant[date_key]
        for metric in metrics:
            if metric in report_dict and isinstance(report_dict[metric], (int, float)):
                series[metric].append({"date": date_key, "value": report_dict[metric]})

    aggregates: dict[str, dict[str, float]] = {}
    for metric, points in series.items():
        aggregates[metric] = _aggregate([p["value"] for p in points])

    return ResponseContext.json(
        {
            "status": "success",
            "data": {
                "period": {
                    "from": start_date.strftime("%Y-%m-%d"),
                    "to": today.strftime("%Y-%m-%d"),
                    "days": days,
                },
                "series": series,
                "aggregates": aggregates,
            },
        }
    )
