"""
Framework-agnostic Meta-Watchdog handlers.

Extracted from api/django/views/meta_watchdog.py (Phase 2b).

Endpoints:
    GET  /health/meta-watchdog        K8s liveness probe
    GET  /meta/status                 System status and component health
    POST /meta/force-check            Trigger immediate health check
    POST /meta/escalation-test        Operator self-test of escalation channels
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "meta_watchdog_liveness",
    "meta_watchdog_status",
    "meta_watchdog_force_check",
    "meta_watchdog_send_test",
]


def _settings():
    from baldur.meta.config import get_meta_watchdog_settings

    return get_meta_watchdog_settings()


def _watchdog():
    from baldur.factory.registry import ProviderRegistry

    watchdog = ProviderRegistry.selfhealer_watchdog.safe_get()
    if watchdog is None:
        raise ImportError("baldur_pro SelfhealerWatchdog not installed")
    return watchdog


def _state_store():
    from baldur.meta.state_store import get_watchdog_state_store

    return get_watchdog_state_store()


def _format_state(state) -> dict:
    return {
        "overall_status": state.overall_status.value,
        "components": {
            name: component_status.value
            for name, component_status in state.component_statuses.items()
        },
        # Per-component reason/error/details for the console drill-down — in
        # particular the daemon_workers catch-all's per-worker map, which the
        # single component status row otherwise hides. getattr-guarded so an
        # older watchdog state object without the field degrades to {}.
        "component_details": getattr(state, "component_details", {}) or {},
        "last_check": state.last_check.isoformat(),
        "escalation_count": state.escalation_count,
        "escalation_pending": state.escalation_pending,
    }


def meta_watchdog_liveness(ctx: RequestContext) -> ResponseContext:
    """GET /health/meta-watchdog — K8s liveness probe (public)."""
    try:
        settings = _settings()

        if not settings.enabled:
            return ResponseContext.json(
                {"status": "disabled", "message": "Meta-Watchdog is disabled"}
            )

        store = _state_store()
        age_seconds = store.get_last_loop_age_seconds()
        max_age = settings.probe_interval_seconds * 3

        if age_seconds > max_age:
            logger.warning(
                "meta_watchdog.liveness_check_failed",
                age_seconds=age_seconds,
                max_age=max_age,
            )
            return ResponseContext.service_unavailable(
                "Watchdog loop appears to be stuck"
            )

        return ResponseContext.json(
            {
                "status": "alive",
                "last_loop_age_seconds": age_seconds,
                "max_age_seconds": max_age,
            }
        )
    except ImportError:
        return ResponseContext.service_unavailable("Meta-Watchdog not installed")
    except Exception as e:
        logger.exception("meta_watchdog.liveness_check_error", error=e)
        return ResponseContext.server_error(str(e))


def meta_watchdog_status(ctx: RequestContext) -> ResponseContext:
    """GET /meta/status — system status and component health (public)."""
    try:
        settings = _settings()

        if not settings.enabled:
            return ResponseContext.json(
                {"overall_status": "disabled", "message": "Meta-Watchdog is disabled"}
            )

        watchdog = _watchdog()
        state = watchdog.get_state()
        result = _format_state(state)
        result["self_cb_open"] = state.self_cb_open
        result["consecutive_failures"] = state.consecutive_failures
        return ResponseContext.json(result)

    except ImportError:
        return ResponseContext.service_unavailable("Meta-Watchdog not installed")
    except Exception as e:
        logger.exception("meta_watchdog.status_check_error", error=e)
        return ResponseContext.server_error(str(e))


def meta_watchdog_force_check(ctx: RequestContext) -> ResponseContext:
    """POST /meta/force-check — trigger immediate health check (operator)."""
    try:
        settings = _settings()

        if not settings.enabled:
            return ResponseContext.json(
                {"overall_status": "disabled", "message": "Meta-Watchdog is disabled"}
            )

        watchdog = _watchdog()
        state = watchdog.force_check()
        result = _format_state(state)
        result["message"] = "Force check completed"
        return ResponseContext.json(result)

    except ImportError:
        return ResponseContext.service_unavailable("Meta-Watchdog not installed")
    except Exception as e:
        logger.exception("meta_watchdog.force_check_error", error=e)
        return ResponseContext.server_error(str(e))


def meta_watchdog_send_test(ctx: RequestContext) -> ResponseContext:
    """POST /meta/escalation-test — send an escalation self-test (operator).

    Delivery routes through the notification seam: on an OSS install
    the configured channel validates config and logs the intended delivery
    (live external push is a PRO capability); on PRO it confirms the channel
    actually delivers.
    """
    # Deliberately NOT gated on settings.enabled (unlike the sibling
    # liveness/status/force_check handlers): validating a webhook *before*
    # enabling the watchdog loop is a primary use case. A fresh
    # EscalationManager is constructed on purpose — NOT get_escalation_manager()
    # — because the cached singleton binds its settings once at first
    # construction, which would freeze a stale webhook URL; the self-test must
    # validate the *currently configured* channel. send_test() also bypasses
    # cooldown and holds no shared state, so no singleton is needed. The three
    # designed outcomes use json() directly (not bad_request/server_error) so
    # the channel lists survive for machine parsing.
    try:
        from baldur.meta.escalation import EscalationManager

        result = EscalationManager().send_test()

        body = {
            "success": result.success,
            "channels_sent": result.channels_sent,
            "channels_failed": result.channels_failed,
            "error_message": result.error_message,
        }

        if result.success:
            status_code = 200
        elif not result.channels_sent and not result.channels_failed:
            status_code = 400  # no channel configured
        else:
            status_code = 502  # ≥1 configured channel failed to deliver

        return ResponseContext.json(body, status_code=status_code)

    except Exception as e:
        logger.exception("meta_watchdog.escalation_test_error", error=e)
        return ResponseContext.server_error(str(e))
