"""
ErrorBudgetGateProbe — monitor Error Budget Gate health for MetaWatchdog.

Two-source judgment:
  Source A: Gate service availability — enabled + effective status
  Source B: Internal component health — fault detector state

Probing strategy: Passive only (no active check, no I/O, no side effects).

Reference: 411 XP7
"""

from __future__ import annotations

import time
from datetime import datetime

import structlog

from baldur.meta.health_probe import HealthProbe, HealthStatus, ProbeResult
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = ["ErrorBudgetGateProbe"]


class ErrorBudgetGateProbe(HealthProbe):
    """Health probe for Error Budget Gate."""

    @property
    def component_name(self) -> str:
        return "error_budget_gate"

    def is_applicable(self) -> bool:
        """Gate is a default-disabled feature; probe only when enabled."""
        from baldur.settings.error_budget_gate import (
            get_error_budget_gate_settings,
        )

        return get_error_budget_gate_settings().enabled

    def probe(self) -> ProbeResult:  # noqa: C901, PLR0912, PLR0915
        start = time.time()
        try:
            from baldur_pro.services.error_budget_gate.gate import (
                get_error_budget_gate,
            )

            gate = get_error_budget_gate()
            passive = gate.get_passive_health()

            enabled: bool = passive["enabled"]
            effective_status: str = passive["effective_status"]
            fd_state: str = passive["fault_detector_state"]
            fd_failures: int = passive["fault_detector_failures"]
            last_checked_at_str: str | None = passive["last_checked_at"]

            # Staleness check
            from baldur.meta.config import get_meta_watchdog_settings
            from baldur.settings.error_budget_gate import (
                get_error_budget_gate_settings,
            )

            meta_settings = get_meta_watchdog_settings()
            gate_settings = get_error_budget_gate_settings()
            multiplier = meta_settings.probe_cache_staleness_multiplier
            staleness_threshold = gate_settings.cache_ttl_seconds * multiplier

            now = utc_now()
            elapsed: float = 0.0
            is_stale = False
            if last_checked_at_str is not None:
                last_checked_at = datetime.fromisoformat(last_checked_at_str)
                elapsed = (now - last_checked_at).total_seconds()
                is_stale = elapsed > staleness_threshold
            else:
                is_stale = True  # Never checked

            # Judgment
            status: HealthStatus
            reason: str = ""

            if not enabled:
                # Disabled by config — intentional, report as HEALTHY
                status = HealthStatus.HEALTHY
                reason = "Gate disabled by configuration"
            elif is_stale:
                # No traffic — status may be stale
                status = HealthStatus.UNKNOWN
                if last_checked_at_str is None:
                    reason = "Gate idle — never evaluated yet, status may be stale"
                else:
                    elapsed_min = f"{elapsed / 60:.0f}"
                    reason = (
                        f"Gate idle — no traffic for {elapsed_min}m, "
                        "status may be stale"
                    )
            elif effective_status in ("open", "warning"):
                fd_degraded = fd_state in ("degraded", "recovering")
                if fd_degraded:
                    status = HealthStatus.DEGRADED
                    reason = f"Gate fault detector in {fd_state} state ({fd_failures} failures)"
                else:
                    status = HealthStatus.HEALTHY
            elif effective_status == "blocked":
                fd_degraded = fd_state in ("degraded", "recovering")
                if fd_degraded:
                    status = HealthStatus.DEGRADED
                    reason = f"Gate blocked and fault detector {fd_state}"
                else:
                    status = HealthStatus.DEGRADED
                    reason = (
                        "Gate operational; automation blocked due to low error budget"
                    )
            elif effective_status == "fail_open":
                status = HealthStatus.DEGRADED
                reason = "Gate in fail-open mode — error budget service unreachable"
            elif effective_status == "fail_open_rate_limited":
                status = HealthStatus.UNHEALTHY
                reason = "Fail-open rate limit exceeded — automation throttled"
            else:
                status = HealthStatus.UNKNOWN
                reason = f"Unexpected gate status: {effective_status}"

            return ProbeResult(
                component=self.component_name,
                status=status,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                reason=reason,
                details=passive,
            )

        except ImportError:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error="error_budget_gate module not available",
            )
        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )
