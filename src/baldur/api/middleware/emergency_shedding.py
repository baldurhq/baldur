"""
Emergency-mode HTTP shedding helper — framework-free.

Emergency Mode's per-tier HTTP load shedding — while an emergency level is
active, classify the request into a tier and drop it with 503 according to the
level's traffic multiplier, so ``non_essential`` is shed before ``standard``
and ``critical`` is protected longest — exposed as a framework-free decision
function adapters compose, mirroring ``check_rate_limit`` /
``check_backpressure`` / ``check_cb_open``.

Capability ladder
-----------------
The emergency *level* is owned by the PRO emergency manager: the
``ProviderRegistry.emergency_manager`` slot is empty in OSS and populated only
by ``baldur_pro``'s provider registration, so this helper gates on **slot
presence** — deliberately slot-direct, never a resolution chain:

- **baldur_pro absent (OSS) or unentitled** — the slot is empty, so the check
  is a clean no-op: nothing is classified, no PRO module is imported, and no
  log record is emitted.
- **baldur_pro present (PRO)** — the level is read and merged with the
  backpressure level (most-restrictive-wins) into one per-tier multiplier that
  decides the request.

Resource contract
-----------------
Bare ``ResponseContext | None`` like the other reject helpers: the decision
acquires no resource and sets no request-scoped state, so there is nothing for
an adapter to release. ``None`` allows, a 503 ``ResponseContext`` rejects.

Merge
-----
Emergency and backpressure each publish a per-tier traffic multiplier
(``EMERGENCY_LEVEL_RULES`` and ``BACKPRESSURE_TIER_RULES``); the lower of the
two decides — Most Restrictive Wins. Both halves sit behind the PRO gate above,
so the backpressure half never runs on an OSS install.

Classification posture
----------------------
Tier resolution never raises: an unmapped application route resolves to
``non_essential`` (protect-critical by design), and the shipped IP overrides
classify private source ranges as ``critical``. Operators map their own routes
through the tiering configuration surface.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.web_framework import HttpMethod, ResponseContext
from baldur.models.emergency import EmergencyLevel
from baldur.settings.backpressure import BackpressureLevel

if TYPE_CHECKING:
    from baldur.interfaces.web_framework import RequestContext

logger = structlog.get_logger()


__all__ = ["check_emergency_shedding"]


def _get_shedding_settings():
    """Return ``EmergencyModeSettings`` or ``None`` when unavailable."""
    try:
        from baldur.settings.emergency_mode import get_emergency_mode_settings

        return get_emergency_mode_settings()
    except Exception as exc:  # settings layer unavailable -> treat as disabled
        logger.warning("emergency_shedding.settings_load_failed", error=str(exc))
        return None


def _emergency_manager():
    """Return the PRO emergency manager, or ``None`` in OSS.

    Presence of the manager is the capability gate: the slot is populated only
    by ``baldur_pro``'s provider registration. ``None`` (empty slot or factory
    unavailable) means the shedding decision does not run at all. Resolved
    lazily so unit tests can patch ``ProviderRegistry`` and so a slim install
    does not break import of this module.
    """
    try:
        from baldur.factory.registry import ProviderRegistry
    except ImportError:
        return None
    return ProviderRegistry.emergency_manager.safe_get()


def _level_rules():
    """Return the per-emergency-level tier multiplier table, or ``None``.

    The table is PRO-owned and computed once at import from
    ``EmergencyModeSettings.level_rules_json``. Unreachable with a populated
    manager slot (registration imports the owning package first) — kept as the
    guard the Django middleware has always carried.
    """
    try:
        from baldur_pro.services.emergency_mode.enums import EMERGENCY_LEVEL_RULES
    except ImportError:
        return None
    return EMERGENCY_LEVEL_RULES


def _backpressure_tier_rules():
    """Return the per-backpressure-level tier multiplier table."""
    from baldur.scaling.tiering.defaults import BACKPRESSURE_TIER_RULES

    return BACKPRESSURE_TIER_RULES


def _backpressure_level():
    """Return the current backpressure level (``NONE`` until the controller runs)."""
    from baldur.scaling.rate_controller import get_rate_controller

    return get_rate_controller().get_state().level


def _tier_registry():
    """Return the tier registry used to classify the request."""
    from baldur.scaling.tiering import get_tier_registry

    return get_tier_registry()


def _client_user_id(request: RequestContext) -> str | None:
    """Resolve the authenticated user id as a string, or ``None``."""
    if not request.is_authenticated or request.user is None:
        return None
    uid = getattr(request.user, "id", None)
    if uid is None:
        uid = getattr(request.user, "pk", None)
    return str(uid) if uid is not None else None


def _should_allow(multiplier: float) -> bool:
    """Decide one request against a traffic multiplier.

    ``1.0`` allows every request, ``0.0`` rejects every request, and a value in
    between allows that fraction. Uses the ``random`` module's default instance
    rather than a private ``Random``: CPython reseeds the default instance in
    every forked child, so preloading workers do not inherit one shed sequence.
    """
    if multiplier >= 1.0:
        return True
    if multiplier <= 0.0:
        return False
    return random.random() < multiplier


def _shedding_response(
    tier_id: str,
    emergency_level: Any,
    retry_after: int,
) -> ResponseContext:
    """Build the 503 load-shedding response."""
    return ResponseContext(
        status_code=503,
        body={
            "error": "Service Temporarily Unavailable",
            "code": "LOAD_SHEDDING",
            "message": (
                "The request was temporarily throttled for system load management. "
                "Please try again shortly."
            ),
            "tier": tier_id,
            "emergency_level": getattr(emergency_level, "value", emergency_level),
            "retry_after": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


def check_emergency_shedding(request: RequestContext) -> ResponseContext | None:
    """Decide whether to shed ``request`` under the active emergency level.

    Pipeline:

    1. ``OPTIONS`` passthrough (CORS preflight is never shed).
    2. ``enabled`` gate (``EmergencyModeSettings.shedding_enabled``).
    3. PRO gate — no emergency manager (OSS / unentitled) -> clean no-op: no
       classification, no PRO import, no log record.
    4. Read the emergency level (only when the manager reports active) and the
       backpressure level; both normal -> allow before classification.
    5. Classify the request into a tier.
    6. Most Restrictive Wins merge of the two per-tier multipliers.
    7. Probabilistic decision -> ``None`` (allow) or a 503 ``ResponseContext``.

    Fail-open: any unexpected error after the OPTIONS check allows the request
    and records one WARNING — a protection feature must not take the web tier
    down when its own inputs are unavailable.
    """
    # 1. CORS preflight is always allowed (no body, negligible load; rejecting
    #    it would break the subsequent real request).
    if request.method == HttpMethod.OPTIONS:
        return None

    try:
        # 2. Enable gate.
        settings = _get_shedding_settings()
        if settings is None or not settings.shedding_enabled:
            return None

        # 3. PRO gate — an empty emergency-manager slot means no level exists.
        manager = _emergency_manager()
        if manager is None:
            return None

        level_rules = _level_rules()
        if level_rules is None:
            return None

        # 4. Level reads. is_active() first so an inactive process initiates no
        #    state-backend read on the request path.
        emergency_level = (
            manager.get_current_level()
            if manager.is_active()
            else EmergencyLevel.NORMAL
        )
        bp_level = _backpressure_level()

        if (
            emergency_level == EmergencyLevel.NORMAL
            and bp_level == BackpressureLevel.NONE
        ):
            return None

        # 5. Classify (Defense-in-Depth fallback chain; never raises).
        tier_result = _tier_registry().resolve_tier_with_fallback(
            path=request.path,
            client_ip=request.client_ip,
            user_id=_client_user_id(request),
            method=request.method.value,
        )
        tier_id = tier_result.tier_id

        # 6. Most Restrictive Wins: the lower of the two per-tier multipliers.
        emergency_multiplier = level_rules.get(emergency_level, {}).get(tier_id, 1.0)
        backpressure_multiplier = (
            _backpressure_tier_rules().get(bp_level, {}).get(tier_id, 1.0)
        )
        final_multiplier = min(emergency_multiplier, backpressure_multiplier)

        # 7. Decision.
        if _should_allow(final_multiplier):
            return None

        rejection = _shedding_response(
            tier_id, emergency_level, settings.shed_retry_after_seconds
        )
    except Exception as exc:
        logger.warning("emergency_shedding.check_failed", error=str(exc))
        return None

    # Recorded after the response exists so a shed never depends on either the
    # log record or the counter (the recorder swallows its own failures).
    logger.info(
        "emergency_shedding.request_rejected",
        path=request.path,
        tier=tier_id,
        emergency_level=getattr(emergency_level, "value", emergency_level),
        backpressure_level=getattr(bp_level, "value", bp_level),
        multiplier=final_multiplier,
    )
    from baldur.metrics.recorders.emergency_mode import record_em_shed

    record_em_shed(tier_id, emergency_level, bp_level)
    return rejection
