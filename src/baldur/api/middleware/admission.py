"""
Admission-control middleware helper — framework-free.

Tier-based priority load shedding — the TrafficGate pipeline (per-tier
Bulkhead -> CascadeLoadShedding -> RateController) that sheds ``non_essential``
traffic first and protects ``critical`` endpoints under a flood — exposed as a
framework-free decision function adapters can compose, mirroring
``check_rate_limit`` / ``check_backpressure`` / ``check_cb_open``.

Capability ladder
-----------------
Per-tier Bulkhead concurrency isolation — the priority-*differentiating* part —
lives in ``baldur_pro``. The ``ProviderRegistry.bulkhead_registry`` slot is
empty in OSS and populated only by ``baldur_pro._register_singleton_providers``,
so admission gates on **bulkhead-registry presence**:

- **baldur_pro absent (OSS)** — the registry slot is empty, so
  ``check_admission`` is a clean no-op (``active=False``): it classifies
  nothing, consumes no token, and never imports the tier registry. The OSS
  baseline ``check_backpressure`` stays the active rate gate; the adapter runs
  it when ``active`` is ``False``.
- **baldur_pro present (PRO)** — the request is classified into a tier and run
  through the per-tier TrafficGate decision. ``active=True`` tells the adapter
  to skip ``check_backpressure`` (admission's internal RateController step is the
  single rate gate — they share the same token bucket).

Resource contract
------------------
Unlike the other three reject helpers (bare ``ResponseContext | None``),
admission is the one resource-holding gate: when it allows under PRO,
``TrafficGate.should_allow`` *acquires* a per-tier Bulkhead slot that MUST be
released after the request completes, or the tier fills permanently. The
:class:`AdmissionDecision` therefore carries a ``release`` closure the adapter
invokes in teardown.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.web_framework import HttpMethod, ResponseContext

if TYPE_CHECKING:
    from baldur.interfaces.web_framework import RequestContext
    from baldur.scaling.traffic_gate import TrafficGate

logger = structlog.get_logger()


__all__ = [
    "AdmissionDecision",
    "check_admission",
]


# Tier ID -> TrafficGate priority int. TrafficGate convention: lower = higher
# priority. Mirrors the Django middleware's TIER_PRIORITY_MAP.
TIER_PRIORITY_MAP: dict[str, int] = {
    "critical": 0,
    "standard": 50,
    "non_essential": 100,
}

# Degraded non_essential forced deadline (ms): under HIGH/CRITICAL backpressure,
# cap a non_essential request's deadline so a heavy query cannot hog the
# critical/standard tiers' resources.
_DEGRADED_TIER_DEADLINE_MS = 1000


@dataclass
class AdmissionDecision:
    """Result of an admission-control decision.

    Richer than the bare ``ResponseContext | None`` the other reject helpers
    return because admission is the one resource-holding gate.

    Attributes:
        rejection: A 503 ``ResponseContext`` to reject with, or ``None`` to
            allow the request through.
        active: ``True`` when admission handled shedding (PRO present + enabled).
            The adapter then skips ``check_backpressure`` so the shared token
            bucket is not consumed twice. ``False`` = OSS no-op / disabled, in
            which case the adapter runs the OSS baseline ``check_backpressure``.
        release: Idempotent closure to call in teardown when a per-tier Bulkhead
            slot was acquired (allow path under PRO only); ``None`` otherwise.
        tier_id: The classified tier (``critical`` / ``standard`` /
            ``non_essential``) when ``active``; ``None`` for the OSS no-op. Used
            by the post-response RTT-sampling helper to name the per-tier
            gradient calculator.
    """

    rejection: ResponseContext | None = None
    active: bool = False
    release: Callable[[], None] | None = None
    tier_id: str | None = None


def _bulkhead_registry():
    """Return the PRO per-tier Bulkhead registry, or ``None`` in OSS.

    Presence of this registry is the capability gate: it is populated only by
    ``baldur_pro._register_singleton_providers``. ``None`` (empty slot or
    factory unavailable) means OSS -> ``check_admission`` is a clean no-op.
    Resolved lazily so unit tests can patch ``ProviderRegistry`` and so a slim
    install does not break import of this module.
    """
    try:
        from baldur.factory.registry import ProviderRegistry
    except ImportError:
        return None
    return ProviderRegistry.bulkhead_registry.safe_get()


def _get_admission_settings():
    """Return ``AdmissionControlSettings`` or ``None`` when unavailable."""
    try:
        from baldur.settings.admission_control import get_admission_control_settings

        return get_admission_control_settings()
    except Exception as exc:  # settings layer unavailable -> treat as disabled
        logger.warning("admission.settings_load_failed", error=str(exc))
        return None


def _client_user_id(request: RequestContext) -> str | None:
    """Resolve the authenticated user id as a string, or ``None``."""
    if not request.is_authenticated or request.user is None:
        return None
    uid = getattr(request.user, "id", None)
    if uid is None:
        uid = getattr(request.user, "pk", None)
    return str(uid) if uid is not None else None


def _maybe_force_degraded_deadline(gate: TrafficGate, tier_id: str) -> None:
    """Cap a degraded ``non_essential`` request's deadline under heavy load.

    Framework-free, ``DEADLINE_ENABLED``-gated, ImportError-guarded. When the
    tier is ``non_essential`` and backpressure is HIGH/CRITICAL, set a 1s
    deadline (unless an inbound deadline is already tighter) so heavy queries
    cannot starve the ``critical`` / ``standard`` tiers.
    """
    if tier_id != "non_essential":
        return
    try:
        from baldur.scaling.deadline_context import (
            DEADLINE_ENABLED,
            get_remaining_ms,
            set_deadline,
        )
        from baldur.settings.backpressure import BackpressureLevel
    except ImportError:
        return

    if not DEADLINE_ENABLED:
        return

    bp_level = gate.get_level()
    if bp_level in (BackpressureLevel.HIGH, BackpressureLevel.CRITICAL):
        remaining = get_remaining_ms()
        if remaining is None or remaining > _DEGRADED_TIER_DEADLINE_MS:
            set_deadline(_DEGRADED_TIER_DEADLINE_MS)
            logger.info(
                "admission.forced_deadline",
                tier_id=tier_id,
                bp_level=bp_level.name,
                deadline_ms=_DEGRADED_TIER_DEADLINE_MS,
            )


def _make_release(gate: TrafficGate, bulkhead_name: str) -> Callable[[], None]:
    """Build an idempotent release closure for an acquired bulkhead slot.

    A ``released`` flag short-circuits a second call so a double-invoke
    (e.g. both the CB-reject teardown and a later finally) cannot trigger a
    spurious ``traffic_gate.release_bulkhead_failed`` warning.
    """
    released = False

    def _release() -> None:
        nonlocal released
        if released:
            return
        released = True
        gate.release_bulkhead(bulkhead_name)

    return _release


def _rejection_response(
    tier_id: str,
    gate_name: str,
    reason: str,
    level,
) -> ResponseContext:
    """Build the 503 admission-reject response (dynamic Retry-After)."""
    from baldur.settings.backpressure import get_backpressure_settings

    bp_settings = get_backpressure_settings()
    try:
        retry_after = bp_settings.get_retry_after_for_level(level)
    except Exception:
        retry_after = bp_settings.reject_retry_after_seconds

    return ResponseContext(
        status_code=503,
        body={
            "error": "Service Temporarily Unavailable",
            "code": "ADMISSION_CONTROL_REJECTED",
            "message": (
                "Request temporarily limited for load management. Please retry later."
            ),
            "tier": tier_id,
            "gate": gate_name,
            "retry_after": retry_after,
        },
        headers={
            "Retry-After": str(retry_after),
            "X-Baldur-Backpressure-Level": level.value,
        },
    )


def check_admission(request: RequestContext) -> AdmissionDecision:
    """Decide whether to admit ``request`` under tier-based load shedding.

    Pipeline:

    1. ``OPTIONS`` passthrough (CORS preflight is never shed).
    2. ``enabled`` gate (``AdmissionControlSettings.enabled``).
    3. PRO gate — no per-tier Bulkhead registry (OSS) -> clean no-op
       (``active=False``), no token consumed, tier registry never imported.
    4. Classify the request into a tier.
    5. Degraded-tier forced deadline (``non_essential`` + heavy load).
    6. Cell-aware bulkhead naming + lazy ``get_or_create`` so the per-tier
       (or per-cell-per-tier) Bulkhead exists for ``should_allow`` to acquire.
    7. ``TrafficGate.should_allow`` decision.
    8. Build the :class:`AdmissionDecision` (reject 503 / allow + release).

    Fail-open: any unexpected error after the PRO gate degrades to
    ``active=False`` so the adapter falls back to the OSS baseline rather than
    500-ing the request.
    """
    # 1. CORS preflight is always allowed (no body, negligible load; rejecting
    #    it would break the subsequent real request).
    if request.method == HttpMethod.OPTIONS:
        return AdmissionDecision()

    # 2. Enable gate.
    settings = _get_admission_settings()
    if settings is None or not settings.enabled:
        return AdmissionDecision()

    # 3. PRO gate — empty Bulkhead registry slot == OSS == clean no-op.
    registry = _bulkhead_registry()
    if registry is None:
        return AdmissionDecision(active=False)

    try:
        from baldur.scaling.tiering import get_tier_registry
        from baldur.scaling.traffic_gate import get_traffic_gate
    except ImportError:
        # Tiering / traffic-gate core unavailable -> fall back to OSS baseline.
        return AdmissionDecision(active=False)

    try:
        gate = get_traffic_gate()

        # 4. Classify into a tier (Defense-in-Depth fallback chain).
        tier_result = get_tier_registry().resolve_tier_with_fallback(
            path=request.path,
            client_ip=request.client_ip,
            user_id=_client_user_id(request),
            method=request.method.value,
        )
        tier_id = tier_result.tier_id

        # 5. Degraded-tier forced deadline.
        _maybe_force_degraded_deadline(gate, tier_id)

        # 6. Cell-aware bulkhead naming + lazy creation so should_allow can
        #    acquire it (registry.get() raises KeyError on an unknown name,
        #    which TrafficGate treats as "skip bulkhead").
        from baldur.context.cell_context import get_current_cell_id

        cell_id = get_current_cell_id()
        bulkhead_name = (
            f"cell:{cell_id}:tier:{tier_id}" if cell_id else f"tier:{tier_id}"
        )
        try:
            registry.get_or_create(
                name=bulkhead_name,
                max_concurrent=settings.get_tier_max_concurrent(tier_id),
            )
        except Exception as exc:
            # Bulkhead creation failure is non-fatal — should_allow degrades to
            # the rate-controller path (fail-open).
            logger.debug("admission.bulkhead_create_failed", error=str(exc))

        # 7. TrafficGate decision.
        decision = gate.should_allow(
            priority=TIER_PRIORITY_MAP.get(tier_id, 50),
            bulkhead_name=bulkhead_name,
            bulkhead_timeout=settings.get_tier_bulkhead_timeout(tier_id),
            metadata={"tier_id": tier_id},
        )
    except Exception as exc:
        logger.warning("admission.check_failed", error=str(exc))
        return AdmissionDecision(active=False)

    # 8. Build the decision.
    if not decision.allowed:
        logger.warning(
            "admission.request_rejected",
            path=request.path,
            tier_id=tier_id,
            gate=decision.gate,
            reason=decision.reason,
        )
        return AdmissionDecision(
            rejection=_rejection_response(
                tier_id, decision.gate, decision.reason, decision.level
            ),
            active=True,
            tier_id=tier_id,
        )

    release = None
    if decision.bulkhead_acquired and decision.bulkhead_name:
        release = _make_release(gate, decision.bulkhead_name)

    return AdmissionDecision(
        rejection=None,
        active=True,
        release=release,
        tier_id=tier_id,
    )
