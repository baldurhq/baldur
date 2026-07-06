"""
NotificationChannelProbe — monitor UNM channel health for MetaWatchdog.

Two-source judgment:
  Source A: Each adapter's is_available() — configuration validity (instant, no I/O)
  Source B: UNM delivery result tracking — _channel_last_success dict

Judgment:
  HEALTHY   — config OK + (recent success or no traffic)
  DEGRADED  — some channels failing or stale
  UNHEALTHY — all channels down or all configs invalid

Reference: 409 UU-E8
"""

from __future__ import annotations

import time
from datetime import timedelta

import structlog

from baldur.meta.health_probe import HealthProbe, HealthStatus, ProbeResult
from baldur.utils.time import utc_now

logger = structlog.get_logger()

# Channel considered stale if no successful delivery for this duration
_STALE_THRESHOLD = timedelta(minutes=10)

__all__ = ["NotificationChannelProbe"]


class NotificationChannelProbe(HealthProbe):
    """Health probe for unified notification channels."""

    @property
    def component_name(self) -> str:
        return "notification_channels"

    def probe(self) -> ProbeResult:  # noqa: C901
        start = time.time()
        try:
            from baldur_pro.services.unified_notification.service import (
                get_unified_notification_manager,
            )

            manager = get_unified_notification_manager()

            # Source A: adapter availability via ProviderRegistry
            available_count = 0
            unavailable_count = 0
            adapter_details: dict[str, str] = {}

            try:
                from baldur.factory import ProviderRegistry

                names = ProviderRegistry.notification.list_providers()
                for name in names:
                    try:
                        adapter = ProviderRegistry.get_notification(name)
                        is_avail = getattr(adapter, "is_available", lambda: True)()
                        adapter_details[name] = (
                            "available" if is_avail else "unavailable"
                        )
                        if is_avail:
                            available_count += 1
                        else:
                            unavailable_count += 1
                    except Exception:
                        adapter_details[name] = "error"
                        unavailable_count += 1
            except Exception:
                # No adapters registered — use fallback defaults
                adapter_details["fallback"] = "available"
                available_count = 1

            # Source B: delivery result tracking
            now = utc_now()
            last_success = manager._channel_last_success
            stale_channels: list[str] = []
            for ch, ts in last_success.items():
                if (now - ts) > _STALE_THRESHOLD:
                    stale_channels.append(ch)

            # Judgment
            total_adapters = available_count + unavailable_count
            reason = ""
            if total_adapters == 0:
                status = HealthStatus.UNKNOWN
                reason = "No notification adapters registered"
            elif unavailable_count == total_adapters:
                status = HealthStatus.UNHEALTHY
                reason = f"All {total_adapters} notification channels unavailable"
            elif unavailable_count > 0 or stale_channels:
                status = HealthStatus.DEGRADED
                reason = f"{unavailable_count} channels unavailable, {len(stale_channels)} stale"
            else:
                status = HealthStatus.HEALTHY

            return ProbeResult(
                component=self.component_name,
                status=status,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                reason=reason,
                details={
                    "available_count": available_count,
                    "unavailable_count": unavailable_count,
                    "stale_channels": stale_channels,
                    "adapters": adapter_details,
                },
            )
        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )
