"""
Container/VM Resource Monitor.

Resource-monitoring utility with cgroup v1/v2 support.

Detects the container's memory/CPU limits via cgroups so that a Chaos
Experiment's Resource Exhaustion stays within safe bounds.

Settings are overridable via environment variables through
ResourceMonitorSettings:
- BALDUR_RESOURCE_MONITOR_SAFETY_MARGIN
"""

from __future__ import annotations

from pathlib import Path

import structlog

from baldur.settings.resource_monitor import get_resource_monitor_settings

logger = structlog.get_logger()


class CgroupResourceMonitor:
    """
    Cgroup-based resource monitor.

    Detects memory/CPU limits and reads current usage.
    Supports both cgroup v1 and v2.

    Usage:
        max_bytes = CgroupResourceMonitor.get_memory_max_bytes()
        current_bytes = CgroupResourceMonitor.get_memory_current_bytes()
        available = CgroupResourceMonitor.get_available_memory_bytes()
    """

    # cgroup v2 paths (Kubernetes 1.25+, Docker 20.10+)
    CGROUP_V2_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")
    CGROUP_V2_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")

    # cgroup v1 paths (legacy compatibility)
    CGROUP_V1_MEMORY_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    CGROUP_V1_MEMORY_USAGE = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")

    @classmethod
    def _get_default_safety_margin(cls) -> float:
        """Default safety margin (15%). Loaded from ResourceMonitorSettings."""
        return get_resource_monitor_settings().safety_margin

    @classmethod
    def get_memory_max_bytes(cls) -> int | None:
        """
        Container memory limit (bytes).

        Returns:
            Memory limit in bytes. None = no limit or undetectable.
        """
        try:
            # Try cgroup v2 first
            if cls.CGROUP_V2_MEMORY_MAX.exists():
                content = cls.CGROUP_V2_MEMORY_MAX.read_text().strip()
                if content != "max":  # "max" = unlimited
                    return int(content)
                return None

            # cgroup v1 fallback
            if cls.CGROUP_V1_MEMORY_LIMIT.exists():
                value = int(cls.CGROUP_V1_MEMORY_LIMIT.read_text().strip())
                # A very large value is effectively unlimited (~9EB)
                if value < 2**62:
                    return value
                return None

            return None
        except Exception as e:
            logger.debug(
                "cgroup_resource_monitor.read_memory_max_failed",
                error=e,
            )
            return None

    @classmethod
    def get_memory_current_bytes(cls) -> int | None:
        """
        Current memory usage (bytes).

        Returns:
            Current usage in bytes. None = undetectable.
        """
        try:
            if cls.CGROUP_V2_MEMORY_CURRENT.exists():
                return int(cls.CGROUP_V2_MEMORY_CURRENT.read_text().strip())

            if cls.CGROUP_V1_MEMORY_USAGE.exists():
                return int(cls.CGROUP_V1_MEMORY_USAGE.read_text().strip())

            return None
        except Exception as e:
            logger.debug(
                "cgroup_resource_monitor.read_memory_current_failed",
                error=e,
            )
            return None

    @classmethod
    def get_available_memory_bytes(
        cls,
        safety_margin: float | None = None,
    ) -> int | None:
        """
        Safely usable memory (bytes).

        Applies a safety margin to avoid triggering the OOM killer.

        Args:
            safety_margin: OOM-prevention headroom ratio (default 15%,
                configurable via environment variable)

        Returns:
            (max - current) * (1 - safety_margin). None = not computable.

        Example:
            # 1GB limit, 700MB in use, 15% margin
            # available = (1024MB - 700MB) * 0.85 = 275MB
        """
        if safety_margin is None:
            safety_margin = cls._get_default_safety_margin()

        max_bytes = cls.get_memory_max_bytes()
        current_bytes = cls.get_memory_current_bytes()

        if max_bytes is None or current_bytes is None:
            return None

        available = max_bytes - current_bytes
        safe_available = int(available * (1.0 - safety_margin))

        logger.debug(
            "cgroup_resource_monitor.mb_mb_mb_safe",
            max_mb=max_bytes / 1024 / 1024,
            current_mb=current_bytes / 1024 / 1024,
            available_mb=available / 1024 / 1024,
            safety_margin_pct=safety_margin * 100,
            safe_available_mb=safe_available / 1024 / 1024,
        )

        return max(0, safe_available)

    @classmethod
    def get_memory_usage_percent(cls) -> float | None:
        """
        Current memory usage ratio (%).

        Returns:
            Usage 0.0~100.0. None = not computable.
        """
        max_bytes = cls.get_memory_max_bytes()
        current_bytes = cls.get_memory_current_bytes()

        if max_bytes is None or current_bytes is None or max_bytes == 0:
            return None

        return (current_bytes / max_bytes) * 100.0

    @classmethod
    def is_memory_constrained(cls) -> bool:
        """
        Check whether the container has a memory limit configured.

        Returns:
            True if a cgroup memory limit is set.
        """
        return cls.get_memory_max_bytes() is not None

    @classmethod
    def check_safe_for_exhaustion(
        cls,
        requested_bytes: int,
        safety_margin: float | None = None,
    ) -> tuple[bool, int]:
        """
        Check whether the memory requested by a ResourceExhaustion experiment is safe.

        Args:
            requested_bytes: requested memory (bytes)
            safety_margin: safety margin (default 15%, configurable via
                environment variable)

        Returns:
            (is_safe, actual_bytes_to_use)
            - is_safe: whether the request is within the safe limit
            - actual_bytes_to_use: bytes to actually use (cap applied)
        """
        if safety_margin is None:
            safety_margin = cls._get_default_safety_margin()

        available = cls.get_available_memory_bytes(safety_margin)

        if available is None:
            # cgroup undetectable - allow without a cap
            logger.warning("cgroup_resource_monitor.cannot_detect_cgroup_limits")
            return True, requested_bytes

        if requested_bytes <= available:
            return True, requested_bytes

        # Safe limit exceeded - apply the cap
        logger.warning(
            "cgroup_resource_monitor.requested_mb_exceeds_safe",
            requested_mb=requested_bytes / 1024 / 1024,
            available_mb=available / 1024 / 1024,
        )
        return False, available

    # =========================================================================
    # Phase 3 (238_PREDICTIVE_ANOMALY_FORECASTER): OOM prediction
    # =========================================================================

    @classmethod
    def predict_oom_minutes(
        cls,
        memory_samples: list[int],
        max_memory_bytes: int | None = None,
        safety_margin: float | None = None,
    ) -> float | None:
        """
        HoltLinear-based prediction of minutes until OOM.

        Analyzes the memory-usage time series and, based on the current
        growth trend, predicts the time until the memory limit
        (max_memory_bytes) is reached.

        Args:
            memory_samples: memory-usage time series (bytes), assumed at
                60-second intervals.
            max_memory_bytes: memory limit. None = auto-detect from cgroup.
            safety_margin: safety margin. None = use the default.

        Returns:
            Estimated minutes until OOM. None = insufficient data, or
            usage is flat/decreasing.

        Code rationale:
            The existing get_available_memory_bytes() only inspects the
            current snapshot. HoltLinear trend analysis detects memory-leak
            patterns ahead of time, enabling proactive action before the
            OOM killer fires.
        """
        if len(memory_samples) < 5:
            return None

        if max_memory_bytes is None:
            max_memory_bytes = cls.get_memory_max_bytes()
        if max_memory_bytes is None:
            return None

        if safety_margin is None:
            safety_margin = cls._get_default_safety_margin()

        # Safe limit = max * (1 - margin)
        safe_limit = int(max_memory_bytes * (1.0 - safety_margin))

        try:
            from baldur.core.time_series import (
                HoltLinearForecaster,
            )

            forecaster = HoltLinearForecaster(
                alpha=0.3, beta=0.1, warmup_samples=min(5, len(memory_samples))
            )
            for sample in memory_samples:
                forecaster.update(float(sample))

            if not forecaster.is_warmed_up:
                return None

            trend_slope = forecaster.get_trend_slope()

            # Trend <= 0 means no memory growth -> no OOM risk
            if trend_slope <= 0:
                return None

            current_level = forecaster._level
            if current_level is None or current_level >= safe_limit:
                return 0.0  # already over the limit

            # Remaining memory / growth per minute = minutes until OOM
            remaining = safe_limit - current_level
            minutes_to_oom = remaining / trend_slope

            logger.debug(
                "cgroup_resource_monitor.oom_prediction_mb_mb",
                current_mb=current_level / 1024 / 1024,
                safe_limit_mb=safe_limit / 1024 / 1024,
                trend_slope_mb=trend_slope / 1024 / 1024,
                minutes_to_oom=minutes_to_oom,
            )

            return max(0.0, minutes_to_oom)
        except Exception as e:
            logger.debug(
                "cgroup_resource_monitor.oom_prediction_failed",
                error=e,
            )
            return None


# Backward compatibility alias
CgroupMemoryMonitor = CgroupResourceMonitor
