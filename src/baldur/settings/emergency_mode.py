"""
EmergencyMode Settings - Pydantic v2.

Emergency Mode 서비스의 RecoveryGate 파라미터, 레벨 결정 임계값, 스로틀 배율,
건강도 감점 상수, 트래픽 배율 규칙을 환경변수로 외부화.

Source:
- services/emergency_mode/models.py (RecoveryGateConfig defaults)
- services/emergency_mode/manager.py (cache TTL, snapshot/history limits)
- services/correlation_engine/emergency_decision.py (level thresholds)
- services/throttle/adaptive/__init__.py (throttle multipliers)
- services/regional_emergency/health_penalty.py (penalty constants)
- services/emergency_mode/enums.py (level rules)

Environment Variables:
    BALDUR_EMERGENCY_MODE_STABILIZATION_PERIOD_SECONDS=300
    BALDUR_EMERGENCY_MODE_CPU_THRESHOLD_PERCENT=80.0
    BALDUR_EMERGENCY_MODE_ERROR_RATE_THRESHOLD=0.05
    BALDUR_EMERGENCY_MODE_LEVEL_STEP_DELAY_SECONDS=60
    BALDUR_EMERGENCY_MODE_HEALTH_CHECK_INTERVAL_SECONDS=30
    BALDUR_EMERGENCY_MODE_AUTO_ACTIVATE_DURATION_MINUTES=30
    BALDUR_EMERGENCY_MODE_CACHE_TTL_SECONDS=30
    BALDUR_EMERGENCY_MODE_MAX_SNAPSHOTS=10
    BALDUR_EMERGENCY_MODE_MAX_HISTORY=100
    BALDUR_EMERGENCY_MODE_L1_SCORE_THRESHOLD=0.4
    BALDUR_EMERGENCY_MODE_L1_CONFIDENCE_THRESHOLD=0.5
    BALDUR_EMERGENCY_MODE_L2_SCORE_THRESHOLD=0.6
    BALDUR_EMERGENCY_MODE_L2_MIN_SERVICES=2
    BALDUR_EMERGENCY_MODE_L3_SCORE_THRESHOLD=0.8
    BALDUR_EMERGENCY_MODE_L3_MIN_SERVICES=3
    BALDUR_EMERGENCY_MODE_L3_MIN_CASCADE_DEPTH=3
    BALDUR_EMERGENCY_MODE_THROTTLE_L1_MULTIPLIER=0.8
    BALDUR_EMERGENCY_MODE_THROTTLE_L2_MULTIPLIER=0.5
    BALDUR_EMERGENCY_MODE_PENALTY_REGIONAL_STRICT=20.0
    BALDUR_EMERGENCY_MODE_PENALTY_GLOBAL_STRICT=30.0
    BALDUR_EMERGENCY_MODE_PENALTY_LEVEL_1=5.0
    BALDUR_EMERGENCY_MODE_PENALTY_LEVEL_2=10.0
    BALDUR_EMERGENCY_MODE_LEVEL_RULES_JSON=null
"""

from __future__ import annotations

import json

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    MediumCount,
    Percentage,
    Probability,
    SmallCount,
)


class EmergencyModeSettings(BaseSettings):
    """
    Emergency Mode 설정.

    RecoveryGate 파라미터, 레벨 결정 임계값, 스로틀 배율,
    건강도 감점 상수, 트래픽 배율 규칙을 정의합니다.
    """

    model_config = make_settings_config("BALDUR_EMERGENCY_MODE_")

    # =========================================================================
    # RecoveryGate parameters (services/emergency_mode/models.py)
    # =========================================================================
    stabilization_period_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Recovery stabilization wait period (seconds).",
    )
    cpu_threshold_percent: float = Field(
        default=80.0,
        ge=10.0,
        le=100.0,
        description="CPU usage threshold for recovery gate (percent).",
    )
    error_rate_threshold: float = Field(
        default=0.05,
        ge=0.001,
        le=0.5,
        description="Error rate threshold for recovery gate.",
    )
    level_step_delay_seconds: int = Field(
        default=60,
        ge=5,
        le=600,
        description="Delay between recovery level steps (seconds).",
    )
    health_check_interval_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Health check interval during recovery (seconds).",
    )

    # =========================================================================
    # Manager parameters (services/emergency_mode/manager.py)
    # =========================================================================
    auto_activate_duration_minutes: int = Field(
        default=30,
        ge=5,
        le=1440,
        description="Default auto-activation expiry duration (minutes).",
    )
    cache_ttl_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="State cache TTL (seconds).",
    )
    max_snapshots: MediumCount = Field(
        default=10,
        description="Maximum rollback snapshots to keep.",
    )
    max_history: int = Field(
        default=100,
        ge=10,
        le=10000,
        description="Maximum history entries to keep.",
    )

    # =========================================================================
    # Level decision thresholds (correlation_engine/emergency_decision.py)
    # =========================================================================
    l1_score_threshold: float = Field(
        default=0.4,
        ge=0.1,
        le=0.9,
        description="Minimum score for LEVEL_1 decision.",
    )
    l1_confidence_threshold: float = Field(
        default=0.5,
        ge=0.1,
        le=1.0,
        description="Minimum confidence for LEVEL_1 decision.",
    )
    l2_score_threshold: float = Field(
        default=0.6,
        ge=0.2,
        le=0.95,
        description="Minimum score for LEVEL_2 decision.",
    )
    l2_min_services: SmallCount = Field(
        default=2,
        description="Minimum affected services for LEVEL_2 decision.",
    )
    l3_score_threshold: float = Field(
        default=0.8,
        ge=0.3,
        le=1.0,
        description="Minimum score for LEVEL_3 decision.",
    )
    l3_min_services: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Minimum affected services for LEVEL_3 decision.",
    )
    l3_min_cascade_depth: SmallCount = Field(
        default=3,
        description="Minimum cascade depth for LEVEL_3 decision.",
    )

    # =========================================================================
    # Throttle multipliers (throttle/adaptive/__init__.py)
    # =========================================================================
    throttle_l1_multiplier: Probability = Field(
        default=0.8,
        description="Throttle limit multiplier for LEVEL_1.",
    )
    throttle_l2_multiplier: Probability = Field(
        default=0.5,
        description="Throttle limit multiplier for LEVEL_2.",
    )

    # =========================================================================
    # Health penalty constants (regional_emergency/health_penalty.py)
    # =========================================================================
    penalty_regional_strict: Percentage = Field(
        default=20.0,
        description="Health score penalty for Regional STRICT mode.",
    )
    penalty_global_strict: Percentage = Field(
        default=30.0,
        description="Health score penalty for Global STRICT mode.",
    )
    penalty_level_1: float = Field(
        default=5.0,
        ge=0.0,
        le=50.0,
        description="Health score penalty for LEVEL_1.",
    )
    penalty_level_2: float = Field(
        default=10.0,
        ge=0.0,
        le=50.0,
        description="Health score penalty for LEVEL_2.",
    )

    # =========================================================================
    # Health Penalty cache (regional_emergency/health_penalty.py) — 339
    # =========================================================================
    penalty_cache_ttl_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description="EmergencyHealthPenalty cache TTL (seconds).",
    )

    # =========================================================================
    # Level rules override (services/emergency_mode/enums.py)
    # =========================================================================
    level_rules_json: str | None = Field(
        default=None,
        description=(
            "JSON override for emergency level traffic rules. "
            "Format: {level_int: {tier: multiplier}}. "
            "None uses built-in defaults from enums.py."
        ),
    )

    # =========================================================================
    # Recovery dampening override (throttle/adaptive/_recovery.py)
    # =========================================================================
    recovery_dampening_multipliers_json: str | None = Field(
        default=None,
        description=(
            "JSON array override for recovery dampening step multipliers. "
            "Default [0.8, 0.9, 1.0]. Values must be ascending, "
            "each 0.0-1.0, last must be 1.0."
        ),
    )

    @model_validator(mode="after")
    def validate_level_ordering(self) -> EmergencyModeSettings:
        """Validate cross-field constraints."""
        # Score thresholds: L1 < L2 < L3
        if not (
            self.l1_score_threshold < self.l2_score_threshold < self.l3_score_threshold
        ):
            raise ValueError(
                f"Score thresholds must be ordered: L1 ({self.l1_score_threshold}) "
                f"< L2 ({self.l2_score_threshold}) < L3 ({self.l3_score_threshold})"
            )
        # Throttle multipliers: L1 >= L2 (higher level = more restriction = lower multiplier)
        if self.throttle_l1_multiplier < self.throttle_l2_multiplier:
            raise ValueError(
                f"throttle_l1_multiplier ({self.throttle_l1_multiplier}) must be >= "
                f"throttle_l2_multiplier ({self.throttle_l2_multiplier})"
            )
        # Penalties: L1 <= L2 (higher level = larger penalty)
        if self.penalty_level_1 > self.penalty_level_2:
            raise ValueError(
                f"penalty_level_1 ({self.penalty_level_1}) must be <= "
                f"penalty_level_2 ({self.penalty_level_2})"
            )
        # Validate level_rules_json structure if provided
        if self.level_rules_json is not None:
            self._validate_level_rules_json(self.level_rules_json)
        # Validate recovery_dampening_multipliers_json if provided
        if self.recovery_dampening_multipliers_json is not None:
            self._validate_recovery_dampening_json(
                self.recovery_dampening_multipliers_json
            )
        return self

    @staticmethod
    def _validate_level_rules_json(json_str: str) -> None:
        """Validate level rules JSON structure."""
        try:
            rules = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"level_rules_json is not valid JSON: {e}") from e

        # ValueError (not TypeError): Pydantic wraps a ValueError into a
        # ValidationError; a TypeError would propagate unwrapped.
        if not isinstance(rules, dict):
            raise ValueError("level_rules_json must be a JSON object")  # noqa: TRY004

        required_tiers = {"critical", "standard", "non_essential"}
        for level_key, tier_rules in rules.items():
            if not isinstance(tier_rules, dict):
                raise ValueError(  # noqa: TRY004
                    f"Level {level_key} rules must be a dict"
                )
            missing = required_tiers - set(tier_rules.keys())
            if missing:
                raise ValueError(f"Level {level_key} missing required tiers: {missing}")
            for tier, value in tier_rules.items():
                if not isinstance(value, (int, float)) or not (0.0 <= value <= 1.0):
                    raise ValueError(
                        f"Level {level_key} tier {tier} value must be 0.0-1.0, got {value}"
                    )

    @staticmethod
    def _validate_recovery_dampening_json(json_str: str) -> None:
        """Validate recovery dampening multipliers JSON array."""
        try:
            values = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"recovery_dampening_multipliers_json is not valid JSON: {e}"
            ) from e

        if not isinstance(values, list) or len(values) < 1:
            raise ValueError(
                "recovery_dampening_multipliers_json must be a non-empty JSON array"
            )
        for i, v in enumerate(values):
            if not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
                raise ValueError(
                    f"recovery_dampening_multipliers_json[{i}] must be 0.0-1.0, got {v}"
                )
        if values != sorted(values):
            raise ValueError(
                "recovery_dampening_multipliers_json values must be ascending"
            )
        if values[-1] != 1.0:
            raise ValueError(
                "recovery_dampening_multipliers_json last value must be 1.0"
            )

    def get_parsed_level_rules(self) -> dict[int, dict[str, float]] | None:
        """Parse and return level rules from JSON, or None for defaults."""
        if self.level_rules_json is None:
            return None
        return {int(k): v for k, v in json.loads(self.level_rules_json).items()}

    def get_parsed_recovery_dampening_multipliers(self) -> tuple[float, ...] | None:
        """Parse and return recovery dampening multipliers, or None for defaults."""
        if self.recovery_dampening_multipliers_json is None:
            return None
        return tuple(json.loads(self.recovery_dampening_multipliers_json))


def get_emergency_mode_settings() -> EmergencyModeSettings:
    """Return cached EmergencyModeSettings via RootConfig."""
    from baldur.settings.root import get_config

    return get_config().services_group.emergency_mode


def reset_emergency_mode_settings() -> None:
    """Reset cached EmergencyModeSettings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["emergency_mode"]
    except KeyError:
        pass
