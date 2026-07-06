"""Framework-agnostic /features/ handler — 530 Wave 6F.

Composes the manifest loader × resolver × entitlement validator into the
admin-only feature inventory response documented in 530 D9. Pure Python:
no Django/DRF imports, so the same handler can be mounted by future
FastAPI/Flask adapters without modification.
"""

from __future__ import annotations

import os
from typing import Any

from baldur.interfaces.web_framework import RequestContext, ResponseContext

__all__ = ["features_summary"]


def _entitlement_block() -> dict[str, Any]:
    """Compose the top-level ``entitlement`` block (530 D9).

    Returns the EntitlementStatus value + claims-derived fields when
    available. When ``baldur_pro`` is absent (OSS install), the
    entitlement validator returns ``MISSING`` with no claims — the
    block shrinks to a single ``status`` key per the spec.
    """
    from baldur.core.entitlement import get_entitlement_status

    result = get_entitlement_status()
    block: dict[str, Any] = {"status": result.status.value}
    claims = result.claims
    if claims is not None:
        block["customer_id"] = claims.customer_id
        block["org"] = claims.org
        block["expires"] = claims.expires
        block["days_until_expiry"] = claims.days_until_expiry
    return block


def features_summary(ctx: RequestContext) -> ResponseContext:
    """GET /api/baldur/features/ — full inventory + entitlement overlay.

    Response shape (530 D9): top-level ``entitlement`` block + flat
    ``features`` array, one entry per manifest row. ``license_status``
    is computed against the current entitlement (Core entries are
    always ``active``; v1.0 entries flip to ``requires_pro`` when no
    active license is present).
    """
    from baldur.services.feature_manifest import (
        load_feature_manifest,
        resolve_feature_status,
    )

    entitlement = _entitlement_block()
    entitlement_status = entitlement["status"]

    entries = load_feature_manifest()
    features = [
        {
            "module": status.module,
            "class": status.class_name,
            "field": status.field,
            "tier": status.tier,
            "default": status.default,
            "enabled": status.enabled,
            "env_var": status.env_var,
            "license_status": status.license_status.value,
        }
        for status in (
            resolve_feature_status(
                entry,
                env=os.environ,
                entitlement_status=entitlement_status,
            )
            for entry in entries
        )
    ]

    return ResponseContext.json(
        {"entitlement": entitlement, "features": features},
    )
