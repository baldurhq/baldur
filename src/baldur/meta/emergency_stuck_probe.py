"""EmergencyStuckProbe — semantic-stuck detection for emergency mode.

The ``daemon_workers`` catch-all probe already covers the gradual-recovery
worker's *liveness*; this probe answers the orthogonal "frozen, not just
crashed" question — is the emergency *level* failing to release? It runs a
time-clock on the emergency manager's recovery/activation timestamps and
reports UNHEALTHY when:

- **Recovery wedged** (primary): ``is_recovering`` and ``recovery_started_at``
  is older than the stuck threshold — the gradual-recovery worker retries a
  perpetually-failing gate forever (live thread, frozen level).
- **Auto-triggered overstay** (secondary backstop): an auto-triggered,
  non-recovering level older than the threshold whose expiry has lapsed —
  reachable only in the narrow window where auto-deactivation itself failed.

Operator-held levels (``is_auto_triggered=False``, not recovering) are
intentional incident response, not a wedged healer, and are excluded —
escalating one would be a false page. Detect + escalate only.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from baldur.meta.config import get_meta_watchdog_settings
from baldur.meta.health_probe import HealthProbe, HealthStatus, ProbeResult
from baldur.models.emergency import EmergencyLevel
from baldur.utils.time import from_iso_string, utc_now

logger = structlog.get_logger()

__all__ = ["EmergencyStuckProbe"]


class EmergencyStuckProbe(HealthProbe):
    """Semantic-stuck probe for emergency-mode level/recovery state."""

    @property
    def component_name(self) -> str:
        return "emergency_mode"

    def is_applicable(self) -> bool:
        """Probe only when the PRO emergency manager is registered.

        Registration is enablement (no chaos-style top-level enable flag);
        OSS-only / unregistered → ``safe_get()`` is ``None`` → skipped. A
        registered-but-NORMAL manager never escalates, so an idle subsystem
        stays a benign always-green component. Fail-safe: resolution error →
        not applicable.
        """
        try:
            from baldur.factory.registry import ProviderRegistry

            return ProviderRegistry.emergency_manager.safe_get() is not None
        except Exception:
            return False

    def probe(self) -> ProbeResult:
        start = time.time()
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.emergency_manager.safe_get()
            if manager is None:
                raise RuntimeError("baldur_pro EmergencyManager not registered")

            state = manager.get_state()
            threshold = get_meta_watchdog_settings().emergency_stuck_threshold_seconds
            now = utc_now()

            wedged, reason, details = self._evaluate(state, threshold, now)
            return ProbeResult(
                component=self.component_name,
                status=(HealthStatus.UNHEALTHY if wedged else HealthStatus.HEALTHY),
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                reason=reason,
                details=details,
            )
        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )

    @staticmethod
    def _age_seconds(iso_str: Any, now: Any) -> float | None:
        """Age of an ISO timestamp in seconds, or ``None`` on parse failure.

        Parse failures are swallowed (fail-safe) so a malformed timestamp
        skips the clause rather than raising — mirroring
        ``CircuitBreakerProbe._count_stuck_open_breakers``.
        """
        if not iso_str:
            return None
        try:
            return (now - from_iso_string(iso_str)).total_seconds()
        except (TypeError, ValueError):
            return None

    def _evaluate(
        self,
        state: Any,
        threshold: float,
        now: Any,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Return ``(wedged, reason, details)`` for the current state.

        Level is compared only with ``!= EmergencyLevel.NORMAL`` (equality,
        never the rich ``</>`` comparators), so no ``EmergencyLevel``-vs-int
        ``TypeError`` is possible.
        """
        level = getattr(state, "level", EmergencyLevel.NORMAL)
        level_str = level.value if isinstance(level, EmergencyLevel) else str(level)
        is_recovering = bool(getattr(state, "is_recovering", False))

        details: dict[str, Any] = {
            "level": level_str,
            "is_recovering": is_recovering,
            "is_auto_triggered": bool(getattr(state, "is_auto_triggered", False)),
            "threshold_sec": threshold,
        }

        # Primary: recovery wedged.
        if is_recovering:
            recovery_started_at = getattr(state, "recovery_started_at", None)
            age = self._age_seconds(recovery_started_at, now)
            if age is not None and age > threshold:
                return (
                    True,
                    (
                        f"Emergency recovery wedged for {age:.0f}s at level "
                        f"{level_str} (threshold {threshold:.0f}s)"
                    ),
                    {
                        **details,
                        "wedged_since": recovery_started_at,
                        "actual_age_sec": age,
                        "clause": "recovery_wedged",
                    },
                )
            # Recovering but not yet past the threshold (or unparseable
            # timestamp) → healthy; an operator-held level cannot be recovering.
            return (False, "", details)

        # Secondary backstop: auto-triggered overstay. Operator-held levels
        # (is_auto_triggered=False) are deliberately excluded.
        if level != EmergencyLevel.NORMAL and getattr(
            state, "is_auto_triggered", False
        ):
            expires_at = getattr(state, "expires_at", None)
            expires_age = self._age_seconds(expires_at, now)
            # "expires_at is None or already past": a positive age means past;
            # an unparseable non-None expiry is treated as NOT past (fail-safe).
            expired_or_none = expires_at is None or (
                expires_age is not None and expires_age > 0
            )
            activated_at = getattr(state, "activated_at", None)
            age = self._age_seconds(activated_at, now)
            if age is not None and age > threshold and expired_or_none:
                return (
                    True,
                    (
                        f"Auto-triggered emergency level {level_str} held for "
                        f"{age:.0f}s without recovery (threshold {threshold:.0f}s)"
                    ),
                    {
                        **details,
                        "wedged_since": activated_at,
                        "actual_age_sec": age,
                        "clause": "auto_triggered_overstay",
                    },
                )

        return (False, "", details)
