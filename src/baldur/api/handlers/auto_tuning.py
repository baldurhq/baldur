"""
Framework-agnostic Auto-Tuning handlers.

Extracted from api/django/views/auto_tuning.py (Phase 2b).
Covers auto-tuning status, enable/disable, module control, bounds,
history, override, and metrics.

Endpoints:
    GET    /auto-tuning/status              System status
    POST   /auto-tuning/enable              Enable auto-tuning
    POST   /auto-tuning/disable             Disable auto-tuning
    POST   /auto-tuning/{module}/enable     Enable specific module
    POST   /auto-tuning/{module}/disable    Disable specific module
    GET    /auto-tuning/bounds              Get safety bounds
    PUT    /auto-tuning/bounds              Update safety bounds
    GET    /auto-tuning/history             Adjustment history list
    GET    /auto-tuning/history/{id}        Adjustment history detail
    POST   /auto-tuning/override            Set manual override
    DELETE /auto-tuning/override/{parameter} Clear manual override
    GET    /auto-tuning/metrics             Current metrics
"""

from __future__ import annotations

import threading
from datetime import datetime

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.audit.helpers import log_system_control_audit
from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "auto_tuning_status",
    "auto_tuning_enable",
    "auto_tuning_disable",
    "auto_tuning_module_enable",
    "auto_tuning_module_disable",
    "auto_tuning_bounds_get",
    "auto_tuning_bounds_update",
    "auto_tuning_history",
    "auto_tuning_override_set",
    "auto_tuning_override_clear",
    "auto_tuning_metrics",
]

_service_instance = None
_service_lock = threading.Lock()


def _get_auto_tuning_service():
    try:
        from baldur.factory import get_auto_tuning_service

        return get_auto_tuning_service()
    except ImportError:
        return _create_default_service()


def _create_default_service():  # noqa: C901
    from baldur.factory import ProviderRegistry

    try:
        from baldur_pro.services.auto_tuning import AutoTuningService
    except ImportError:
        AutoTuningService = None  # type: ignore[assignment,misc]

    class DummyMetricsAdapter:
        def fetch_current_metrics(self):
            return {
                "p99_latency_ms": 0,
                "error_rate": 0,
                "retry_exhausted_rate": 0,
                "throughput_rps": 0,
            }

    class DummyConfigProvider:
        def get(self, key, default=None):
            return default

    class DummyConfigApplier:
        def __init__(self):
            self._values = {}

        def get_current(self, parameter):
            return self._values.get(parameter, 0)

        def apply(self, parameter, value):
            self._values[parameter] = value
            return True

        def rollback(self, parameter, value):
            self._values[parameter] = value
            return True

    try:
        audit_adapter = ProviderRegistry.get_audit_adapter()
    except (ValueError, ImportError):

        class AuditAdapterWrapper:
            def log(self, entry):
                log_system_control_audit(
                    action=entry.get("action", "auto_tuning"),
                    actor=entry.get("actor", "system"),
                    old_state=entry.get("old_state"),
                    new_state=entry.get("new_state"),
                    reason=entry.get("reason", str(entry)),
                )

        audit_adapter = AuditAdapterWrapper()

    try:
        from baldur.adapters.metrics.auto_tuning_adapter import InternalMetricsAdapter

        metrics_adapter = InternalMetricsAdapter()
    except ImportError:
        logger.warning("auto_tuning.internalmetricsadapter_import_failed_falling")
        metrics_adapter = DummyMetricsAdapter()

    try:
        from baldur.adapters.config_applier.composite import CompositeConfigApplier
        from baldur.adapters.config_applier.throttle import ThrottleConfigApplier

        config_applier = CompositeConfigApplier(
            [ThrottleConfigApplier(), DummyConfigApplier()]
        )
    except ImportError:
        logger.warning("auto_tuning.throttleconfigapplier_import_failed_falling")
        config_applier = DummyConfigApplier()

    service = AutoTuningService(
        metrics_adapter=metrics_adapter,
        config_provider=DummyConfigProvider(),
        config_applier=config_applier,
        audit_adapter=audit_adapter,
    )

    try:
        from baldur_pro.services.auto_tuning.throttle_sla_rules import (
            THROTTLE_SLA_RULES,
        )

        service.decision_engine.rules.extend(THROTTLE_SLA_RULES)
    except ImportError:
        logger.warning("auto_tuning.import_failed_sla_auto")

    return service


def _service():
    global _service_instance
    if _service_instance is None:
        with _service_lock:
            if _service_instance is None:
                _service_instance = _get_auto_tuning_service()
    return _service_instance


def _export_limit() -> int:
    try:
        from baldur.settings.api_view import get_api_view_settings

        return get_api_view_settings().auto_tuning_export_limit
    except Exception:
        return 1000


def auto_tuning_status(ctx: RequestContext) -> ResponseContext:
    """GET /auto-tuning/status — system status (viewer)."""
    return ResponseContext.json(_service().get_status())


def auto_tuning_enable(ctx: RequestContext) -> ResponseContext:
    """POST /auto-tuning/enable — enable auto-tuning (admin)."""
    body = ctx.json_body or {}
    actor = resolve_actor(ctx)
    result = _service().enable(
        reason=body.get("reason", ""),
        mode=body.get("mode", "automatic"),
        enabled_by=actor,
    )
    return ResponseContext.json(result)


def auto_tuning_disable(ctx: RequestContext) -> ResponseContext:
    """POST /auto-tuning/disable — disable auto-tuning (admin)."""
    body = ctx.json_body or {}
    actor = resolve_actor(ctx)
    result = _service().disable(
        reason=body.get("reason", ""),
        duration_minutes=body.get("duration_minutes"),
        disabled_by=actor,
        notify=body.get("notify", True),
    )
    return ResponseContext.json(result)


def auto_tuning_module_enable(ctx: RequestContext) -> ResponseContext:
    """POST /auto-tuning/{module}/enable — enable specific module (admin)."""
    module = ctx.get_path_param("module")
    body = ctx.json_body or {}
    actor = resolve_actor(ctx)
    result = _service().enable_module(
        module=module,
        reason=body.get("reason", ""),
        enabled_by=actor,
    )
    if "error" in result:
        return ResponseContext.bad_request(result["error"])
    return ResponseContext.json(result)


def auto_tuning_module_disable(ctx: RequestContext) -> ResponseContext:
    """POST /auto-tuning/{module}/disable — disable specific module (admin)."""
    module = ctx.get_path_param("module")
    body = ctx.json_body or {}
    actor = resolve_actor(ctx)
    result = _service().disable_module(
        module=module,
        reason=body.get("reason", ""),
        duration_minutes=body.get("duration_minutes"),
        disabled_by=actor,
    )
    if "error" in result:
        return ResponseContext.bad_request(result["error"])
    return ResponseContext.json(result)


def auto_tuning_bounds_get(ctx: RequestContext) -> ResponseContext:
    """GET /auto-tuning/bounds — get safety bounds (viewer)."""
    return ResponseContext.json(_service().get_bounds())


def auto_tuning_bounds_update(ctx: RequestContext) -> ResponseContext:
    """PUT /auto-tuning/bounds — update safety bounds (admin)."""
    body = ctx.json_body or {}
    parameter = body.get("parameter")
    if not parameter:
        return ResponseContext.bad_request("parameter is required")

    actor = resolve_actor(ctx)
    result = _service().update_bounds(
        parameter=parameter,
        bounds=body.get("bounds", {}),
        reason=body.get("reason", ""),
        updated_by=actor,
    )
    if "error" in result:
        return ResponseContext.bad_request(result["error"])
    return ResponseContext.json(result)


def auto_tuning_history(ctx: RequestContext) -> ResponseContext:
    """GET /auto-tuning/history — adjustment history list/detail (viewer)."""
    history_id = ctx.get_path_param("history_id")
    service = _service()

    if history_id:
        limit = _export_limit()
        records = service.adjustment_recorder.get_records(limit=limit)
        for record in records:
            if record.record_id == history_id:
                return ResponseContext.json(record.to_dict())
        return ResponseContext.not_found("History record not found")

    start_date = ctx.get_query("start_date")
    end_date = ctx.get_query("end_date")
    parameter = ctx.get_query("parameter")

    try:
        page = int(ctx.get_query("page", 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(ctx.get_query("page_size", 20))
    except (TypeError, ValueError):
        page_size = 20

    start_dt = None
    end_dt = None
    if start_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except ValueError:
            pass

    result = service.get_history(
        start_date=start_dt,
        end_date=end_dt,
        parameter=parameter,
        page=page,
        page_size=page_size,
    )
    return ResponseContext.json(result)


def auto_tuning_override_set(ctx: RequestContext) -> ResponseContext:
    """POST /auto-tuning/override — set manual override (admin)."""
    body = ctx.json_body or {}
    parameter = body.get("parameter")
    if not parameter:
        return ResponseContext.bad_request("parameter is required")

    value = body.get("value")
    if value is None:
        return ResponseContext.bad_request("value is required")

    actor = resolve_actor(ctx)
    result = _service().override(
        parameter=parameter,
        value=float(value),
        reason=body.get("reason", ""),
        duration_minutes=body.get("duration_minutes"),
        disable_auto_tuning=body.get("disable_auto_tuning", True),
        overridden_by=actor,
    )
    if "error" in result:
        return ResponseContext.bad_request(result["error"])
    return ResponseContext.json(result)


def auto_tuning_override_clear(ctx: RequestContext) -> ResponseContext:
    """DELETE /auto-tuning/override/{parameter} — clear override (admin)."""
    parameter = ctx.get_path_param("parameter")
    actor = resolve_actor(ctx)
    result = _service().clear_override(
        parameter=parameter,
        cleared_by=actor,
    )
    if "error" in result:
        return ResponseContext.not_found(result["error"])
    return ResponseContext.json(result)


def auto_tuning_metrics(ctx: RequestContext) -> ResponseContext:
    """GET /auto-tuning/metrics — current metrics (viewer)."""
    return ResponseContext.json(_service().get_current_metrics())
