"""Web console handler — serves the built-in operator UI at ``GET /`` (536).

Design (impl doc 536):
    * D4 — returns 404 when ``console_enabled`` is false; the JSON API keeps
      serving.
    * D3 — loads ``console.html`` fresh per request (no cache; admin <1 RPS) so
      an editable/source install picks up frontend edits with no restart.
    * D8/D9 — injects a v1.0 PRO panel-visibility map computed from
      ``ProviderRegistry.<slot>.has_provider("pro")`` (registry presence reflects
      what is *running*, not license text).
    * D12 — generates a per-request CSP nonce and emits a nonce-based
      Content-Security-Policy header; the same nonce is injected into the inline
      ``<script>`` tags.
"""

from __future__ import annotations

import json
import secrets
from importlib.resources import files

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.settings.admin import get_admin_server_settings

logger = structlog.get_logger()

__all__ = ["console_page"]


# Hardcoded PRO panel -> ProviderRegistry slot map (D9).
#
# MUST stay an explicit list — never derive it by iterating "all registered PRO
# slots", or v1.1 panels (chaos_scheduler / saga / error_budget / postmortem)
# would leak under any active license (PRO registration registers every slot,
# v1.1 ones included; the v1.0/v1.1 split lives only in V1_LAUNCH_MANIFEST.yaml).
# Source of truth for the v1.0 panel set: memory/pro-launch-strategy.md Rev 5,
# plus the Meta-Watchdog detect+escalate slice, which graduated to v1.0 in Rev 7
# (after this map was first frozen) — hence meta_watchdog is appended last.
#
# Post-v1.0 additions append below the v1.0 set with their adding doc noted
# (the map is no longer frozen — incremental-accumulation window): runtime_config
# is the Runtime Config editor panel (662), the first post-v1.0 panel addition,
# gated on the PRO-only RuntimeConfigManager slot.
_V1_PRO_PANEL_SLOTS: dict[str, str] = {
    "emergency": "emergency_manager",
    "dlq": "dlq_service",
    "bulkhead": "bulkhead_registry",
    "canary": "canary_rollout_service",
    "throttle": "adaptive_throttle",
    "governance": "governance",
    "meta_watchdog": "selfhealer_watchdog",
    "runtime_config": "runtime_config_manager",  # 662 — post-v1.0
}

# Placeholders substituted server-side at render. The nonce value from
# secrets.token_urlsafe is URL-safe base64 ([A-Za-z0-9_-]) and never collides
# with these tokens, so replacement order is irrelevant.
_PANELS_PLACEHOLDER = "__BALDUR_PANELS_JSON__"
_ENTITLEMENT_PLACEHOLDER = "__BALDUR_ENTITLEMENT__"
_NONCE_PLACEHOLDER = "__BALDUR_CSP_NONCE__"


def _panel_visibility() -> dict[str, bool]:
    """Compute the v1.0 PRO panel-visibility map from registry presence (D8/D9).

    A panel is visible iff its backing slot has a provider registered under
    ``"pro"`` — which, because PRO registration *is* the boot-time entitlement
    gate, reflects what is actually running rather than license text.
    ``has_provider()`` is a side-effect-free dict-membership check; it never
    instantiates the singleton on the render path.
    """
    from baldur.factory import ProviderRegistry

    visibility: dict[str, bool] = {}
    for panel, slot in _V1_PRO_PANEL_SLOTS.items():
        registry = getattr(ProviderRegistry, slot, None)
        visibility[panel] = bool(registry is not None and registry.has_provider("pro"))
    return visibility


def _safe_json_for_script(data: object) -> str:
    """``json.dumps`` escaped for safe embedding inside an inline ``<script>``.

    Escapes ``<``, ``>``, ``&`` so a ``</script>`` or comment sequence in the
    payload cannot break out of the script context. The payload here is fully
    server-controlled (hardcoded keys, bool values), but the escape keeps the
    pattern correct if the payload ever grows.
    """
    return (
        json.dumps(data)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _entitlement_status() -> str:
    """Best-effort entitlement status string for the informational banner (D8).

    Non-gating: explains the rare mid-run license-expiry window (PRO services
    stay live until restart). Never affects panel visibility; any failure
    degrades to ``"unknown"``.
    """
    try:
        from baldur.core.entitlement import get_entitlement_status

        return get_entitlement_status().status.value
    except Exception as exc:  # noqa: BLE001
        logger.debug("admin.console_entitlement_unavailable", error=exc)
        return "unknown"


def console_page(ctx: RequestContext) -> ResponseContext:
    """``GET /`` — render the web console (D3/D5/D8/D12)."""
    settings = get_admin_server_settings()
    if not settings.console_enabled:
        return ResponseContext.not_found("Admin web console is disabled")

    try:
        template = (files("baldur.api.admin.console") / "console.html").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, OSError) as exc:
        logger.exception("admin.console_asset_error", error=exc)
        return ResponseContext.server_error("Console asset unavailable")

    nonce = secrets.token_urlsafe(16)
    html = (
        template.replace(
            _PANELS_PLACEHOLDER, _safe_json_for_script(_panel_visibility())
        )
        .replace(_ENTITLEMENT_PLACEHOLDER, _entitlement_status())
        .replace(_NONCE_PLACEHOLDER, nonce)
    )

    # Nonce-based CSP (D12): a bare default-src 'self' would block the page's
    # own inline scripts. The nonce keeps script protection meaningful (an
    # injected inline <script> lacks the nonce -> blocked); style-src
    # 'unsafe-inline' is the lower-risk relaxation for the inline <style>.
    csp = f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'"
    return ResponseContext.raw(
        html,
        "text/html; charset=utf-8",
        headers={"Content-Security-Policy": csp},
    )
