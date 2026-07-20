"""
DecisionEngine Settings - Pydantic v2.

Tuning decision engine configuration.
Minimum change ratio, sample-count-based confidence, and variability-based
stability factor settings.

Environment Variables:
    BALDUR_DECISION_ENGINE_MAX_HISTORY=5000
    BALDUR_DECISION_ENGINE_MIN_CHANGE_RATIO=0.05
    BALDUR_DECISION_ENGINE_CONFIDENCE_SAMPLES_VERY_LOW=5
    BALDUR_DECISION_ENGINE_CONFIDENCE_SAMPLES_LOW=20
    BALDUR_DECISION_ENGINE_CONFIDENCE_SAMPLES_MEDIUM=50
    BALDUR_DECISION_ENGINE_CONFIDENCE_SAMPLES_HIGH=100
    BALDUR_DECISION_ENGINE_CONFIDENCE_VALUE_VERY_LOW=0.3
    BALDUR_DECISION_ENGINE_CONFIDENCE_VALUE_LOW=0.5
    BALDUR_DECISION_ENGINE_CONFIDENCE_VALUE_MEDIUM=0.65
    BALDUR_DECISION_ENGINE_CONFIDENCE_VALUE_HIGH=0.75
    BALDUR_DECISION_ENGINE_CONFIDENCE_VALUE_VERY_HIGH=0.9
    BALDUR_DECISION_ENGINE_STABILITY_CV_HIGH=0.5
    BALDUR_DECISION_ENGINE_STABILITY_CV_MEDIUM=0.2
    BALDUR_DECISION_ENGINE_STABILITY_FACTOR_UNSTABLE=0.7
    BALDUR_DECISION_ENGINE_STABILITY_FACTOR_MODERATE=0.85
    BALDUR_DECISION_ENGINE_STABILITY_FACTOR_STABLE=1.0
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class DecisionEngineSettings(BaseSettings):
    """
    DecisionEngine configuration.

    Thresholds and confidence mappings used by metric-analysis-driven tuning
    decisions.
    """

    model_config = make_settings_config("BALDUR_DECISION_ENGINE_")

    # ==========================================================================
    # History size
    # ==========================================================================
    max_history: int = Field(
        default=5000,
        ge=100,
        le=10000,
        description="Maximum analysis history entries. Default 5,000 to cover 72 hours.",
    )

    # ==========================================================================
    # Minimum change ratio
    # ==========================================================================
    min_change_ratio: float = Field(
        default=0.05,
        ge=0.01,
        le=0.5,
        description="Minimum change ratio for a meaningful adjustment (0.05 = 5%)",
    )

    # ==========================================================================
    # Sample-count-based confidence mapping - sample count thresholds
    # ==========================================================================
    confidence_samples_very_low: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Sample count threshold for very low confidence",
    )
    confidence_samples_low: int = Field(
        default=20,
        ge=5,
        le=100,
        description="Sample count threshold for low confidence",
    )
    confidence_samples_medium: int = Field(
        default=50,
        ge=10,
        le=200,
        description="Sample count threshold for medium confidence",
    )
    confidence_samples_high: int = Field(
        default=100,
        ge=20,
        le=500,
        description="Sample count threshold for high confidence",
    )

    # ==========================================================================
    # Sample-count-based confidence mapping - confidence values
    # ==========================================================================
    confidence_value_very_low: float = Field(
        default=0.3,
        ge=0.1,
        le=0.5,
        description="Confidence value when sample count < very_low threshold",
    )
    confidence_value_low: float = Field(
        default=0.5,
        ge=0.3,
        le=0.7,
        description="Confidence value when sample count < low threshold",
    )
    confidence_value_medium: float = Field(
        default=0.65,
        ge=0.4,
        le=0.8,
        description="Confidence value when sample count < medium threshold",
    )
    confidence_value_high: float = Field(
        default=0.75,
        ge=0.5,
        le=0.9,
        description="Confidence value when sample count < high threshold",
    )
    confidence_value_very_high: float = Field(
        default=0.9,
        ge=0.7,
        le=1.0,
        description="Confidence value when sample count >= high threshold",
    )

    # ==========================================================================
    # Variability (CV) based stability factor - CV thresholds
    # ==========================================================================
    stability_cv_high: float = Field(
        default=0.5,
        ge=0.2,
        le=1.0,
        description="CV threshold for unstable classification",
    )
    stability_cv_medium: float = Field(
        default=0.2,
        ge=0.05,
        le=0.5,
        description="CV threshold for moderate stability classification",
    )

    # ==========================================================================
    # Variability (CV) based stability factor - factor values
    # ==========================================================================
    stability_factor_unstable: float = Field(
        default=0.7,
        ge=0.3,
        le=0.9,
        description="Stability factor when CV > cv_high",
    )
    stability_factor_moderate: float = Field(
        default=0.85,
        ge=0.6,
        le=0.95,
        description="Stability factor when CV > cv_medium",
    )
    stability_factor_stable: float = Field(
        default=1.0,
        ge=0.8,
        le=1.0,
        description="Stability factor when CV <= cv_medium",
    )

    @model_validator(mode="after")
    def validate_thresholds(self) -> "DecisionEngineSettings":
        """Validate the ordering of sample counts and confidence values."""
        # Sample count ordering
        if not (
            self.confidence_samples_very_low
            < self.confidence_samples_low
            < self.confidence_samples_medium
            < self.confidence_samples_high
        ):
            raise ValueError(
                "Sample thresholds must be in ascending order: "
                "very_low < low < medium < high"
            )

        # Confidence value ordering
        if not (
            self.confidence_value_very_low
            < self.confidence_value_low
            < self.confidence_value_medium
            < self.confidence_value_high
            < self.confidence_value_very_high
        ):
            raise ValueError(
                "Confidence values must be in ascending order: "
                "very_low < low < medium < high < very_high"
            )

        # Stability factor ordering
        if not (
            self.stability_factor_unstable
            < self.stability_factor_moderate
            < self.stability_factor_stable
        ):
            raise ValueError(
                "Stability factors must be in ascending order: "
                "unstable < moderate < stable"
            )

        # CV threshold ordering
        if self.stability_cv_medium >= self.stability_cv_high:
            raise ValueError(
                f"stability_cv_medium ({self.stability_cv_medium}) must be less than "
                f"stability_cv_high ({self.stability_cv_high})"
            )

        return self

    def get_sample_confidence(self, sample_count: int) -> float:
        """
        Return the confidence for a given sample count.

        Args:
            sample_count: Number of samples

        Returns:
            Confidence value (0.0 ~ 1.0)
        """
        if sample_count < self.confidence_samples_very_low:
            return self.confidence_value_very_low
        if sample_count < self.confidence_samples_low:
            return self.confidence_value_low
        if sample_count < self.confidence_samples_medium:
            return self.confidence_value_medium
        if sample_count < self.confidence_samples_high:
            return self.confidence_value_high
        return self.confidence_value_very_high

    def get_stability_factor(self, coefficient_of_variation: float) -> float:
        """
        Return the stability factor for a given coefficient of variation (CV).

        Args:
            coefficient_of_variation: Coefficient of variation (stddev/mean)

        Returns:
            Stability factor (0.0 ~ 1.0)
        """
        if coefficient_of_variation > self.stability_cv_high:
            return self.stability_factor_unstable
        if coefficient_of_variation > self.stability_cv_medium:
            return self.stability_factor_moderate
        return self.stability_factor_stable


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_decision_engine_settings() -> "DecisionEngineSettings":
    """
    Return the cached DecisionEngineSettings instance.

    Returns:
        DecisionEngineSettings: Singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().services_group.decision_engine


def reset_decision_engine_settings() -> None:
    """
    Reset the cached settings (for testing).
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["decision_engine"]
    except KeyError:
        pass
