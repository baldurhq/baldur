"""
SLO/SLI Definitions

Service Level Indicators (SLI) and Service Level Objectives (SLO)
definitions for monitoring system reliability.

Design Philosophy:
- SLIs are measurements (what we measure)
- SLOs are targets (what we want to achieve)
- Error Budget = how much failure is acceptable
- All decisions are informed by data, but made by humans

Usage:
    from baldur.slo import SLOConfig, SLI, SLO, ErrorBudget

    slo_config = SLOConfig(
        slos=[
            SLO(
                name="api_availability",
                sli=SLI.AVAILABILITY,
                target=0.999,
                window_days=30,
            ),
        ]
    )
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class SLI(str, Enum):
    """
    Standard Service Level Indicators.

    These are the metrics we measure.
    """

    # Availability SLIs
    AVAILABILITY = "availability"  # Proportion of successful requests

    # Latency SLIs
    LATENCY_P50 = "latency_p50"  # 50th percentile latency
    LATENCY_P90 = "latency_p90"  # 90th percentile latency
    LATENCY_P99 = "latency_p99"  # 99th percentile latency

    # Error SLIs
    ERROR_RATE = "error_rate"  # Proportion of failed requests

    # Throughput SLIs
    THROUGHPUT = "throughput"  # Requests per second

    # Custom
    CUSTOM = "custom"


@dataclass
class SLO:
    """
    Service Level Objective definition.

    An SLO is a target for an SLI over a time window.

    Example:
        SLO(
            name="api_availability",
            description="API should be 99.9% available",
            sli=SLI.AVAILABILITY,
            target=0.999,
            window_days=30,
        )
    """

    name: str
    sli: SLI
    target: float  # Target value (e.g., 0.999 for 99.9%)
    window_days: int = 30  # Rolling window in days

    description: str | None = None
    service_name: str | None = None
    domain: str | None = None
    region: str | None = None

    # Thresholds for alerting
    warning_threshold: float | None = None  # Alert when dropping below this
    critical_threshold: float | None = None  # Critical alert threshold

    # Error budget burn rate thresholds - defaults come from settings
    fast_burn_rate: float = field(default_factory=lambda: _get_default_fast_burn_rate())
    slow_burn_rate: float = field(default_factory=lambda: _get_default_slow_burn_rate())

    def __post_init__(self) -> None:
        if self.warning_threshold is None:
            # Default: warn when 50% of error budget consumed
            self.warning_threshold = (
                1 + self.target
            ) / 2  # midpoint between target and 1.0

        if self.critical_threshold is None:
            # Default: critical when approaching SLO target
            self.critical_threshold = self.target + 0.001

    @property
    def error_budget(self) -> float:
        """
        Error budget as a decimal.

        For 99.9% availability, error budget is 0.1% = 0.001
        """
        return 1.0 - self.target

    @property
    def error_budget_minutes_per_window(self) -> float:
        """Error budget in minutes per window."""
        total_minutes = self.window_days * 24 * 60
        return total_minutes * self.error_budget

    @property
    def error_budget_minutes_per_day(self) -> float:
        """Error budget in minutes per day."""
        return self.error_budget_minutes_per_window / self.window_days

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "sli": self.sli.value,
            "target": self.target,
            "target_percentage": f"{self.target * 100:.3f}%",
            "window_days": self.window_days,
            "description": self.description,
            "service_name": self.service_name,
            "domain": self.domain,
            "error_budget": self.error_budget,
            "error_budget_minutes_per_window": self.error_budget_minutes_per_window,
            "error_budget_minutes_per_day": self.error_budget_minutes_per_day,
            "warning_threshold": self.warning_threshold,
            "critical_threshold": self.critical_threshold,
        }


def _get_default_fast_burn_rate() -> float:
    """Read default_fast_burn_rate from settings."""
    try:
        from baldur.settings.slo import get_slo_settings

        return get_slo_settings().default_fast_burn_rate
    except Exception:
        return 14.4  # Google SRE default


def _get_default_slow_burn_rate() -> float:
    """Read default_slow_burn_rate from settings."""
    try:
        from baldur.settings.slo import get_slo_settings

        return get_slo_settings().default_slow_burn_rate
    except Exception:
        return 3.0  # Google SRE default


@dataclass
class SLOStatus:
    """
    Current status of an SLO.

    Tracks current performance against target.
    """

    slo: SLO
    current_value: float
    measured_at: datetime = field(default_factory=lambda: utc_now())

    # Error budget status
    budget_remaining: float | None = None  # Remaining as decimal (1.0 = 100%)
    burn_rate: float | None = None  # Current burn rate

    # Historical
    sample_count: int = 0  # Number of data points

    @property
    def is_meeting_target(self) -> bool:
        """Check if currently meeting SLO target."""
        if self.slo.sli in [SLI.ERROR_RATE]:
            # Lower is better
            return self.current_value <= (1 - self.slo.target)
        # Higher is better
        return self.current_value >= self.slo.target

    @property
    def is_warning(self) -> bool:
        """Check if in warning state."""
        if not self.is_meeting_target:
            return True
        if (
            self.slo.warning_threshold
            and self.current_value < self.slo.warning_threshold
        ):
            return True
        return bool(self.budget_remaining is not None and self.budget_remaining < 0.5)

    @property
    def is_critical(self) -> bool:
        """Check if in critical state."""
        if (
            self.slo.critical_threshold
            and self.current_value < self.slo.critical_threshold
        ):
            return True
        if self.budget_remaining is not None and self.budget_remaining < 0.1:
            return True
        return bool(
            self.burn_rate is not None and self.burn_rate > self.slo.fast_burn_rate
        )

    @property
    def budget_consumed_percent(self) -> float:
        """Error budget consumed as percentage."""
        if self.budget_remaining is None:
            return 0.0
        return (1.0 - self.budget_remaining) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "slo_name": self.slo.name,
            "target": self.slo.target,
            "current_value": self.current_value,
            "current_percentage": f"{self.current_value * 100:.3f}%",
            "is_meeting_target": self.is_meeting_target,
            "is_warning": self.is_warning,
            "is_critical": self.is_critical,
            "budget_remaining": self.budget_remaining,
            "budget_consumed_percent": self.budget_consumed_percent,
            "burn_rate": self.burn_rate,
            "measured_at": self.measured_at.isoformat(),
            "sample_count": self.sample_count,
        }


@dataclass
class SLOConfig:
    """
    Configuration for multiple SLOs.

    Provides default SLO definitions that can be customized.
    """

    slos: list[SLO] = field(default_factory=list)
    default_window_days: int = 30

    @classmethod
    def default_config(cls, service_name: str | None = None) -> SLOConfig:
        """
        Create default SLO configuration.

        Default SLOs:
        - Availability: 99.9% (43.2 min downtime/month)
        - Latency P99: < 500ms
        - Error Rate: < 0.1%
        """
        return cls(
            slos=[
                SLO(
                    name="availability",
                    description="API availability - proportion of successful requests",
                    sli=SLI.AVAILABILITY,
                    target=0.999,
                    window_days=30,
                    service_name=service_name,
                ),
                SLO(
                    name="latency_p99",
                    description="99th percentile response time should be under 500ms",
                    sli=SLI.LATENCY_P99,
                    target=0.500,  # 500ms in seconds
                    window_days=7,
                    service_name=service_name,
                ),
                SLO(
                    name="error_rate",
                    description="Error rate should be under 0.1%",
                    sli=SLI.ERROR_RATE,
                    target=0.999,  # Success rate (inverse of error rate)
                    window_days=30,
                    service_name=service_name,
                ),
            ],
        )

    def get_slo(self, name: str) -> SLO | None:
        """Get SLO by name."""
        for slo in self.slos:
            if slo.name == name:
                return slo
        return None

    def add_slo(self, slo: SLO) -> None:
        """Add an SLO."""
        # Replace if exists
        self.slos = [s for s in self.slos if s.name != slo.name]
        self.slos.append(slo)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "slos": [slo.to_dict() for slo in self.slos],
            "default_window_days": self.default_window_days,
        }

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2)
