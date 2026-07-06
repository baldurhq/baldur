"""
Unit tests for baldur.settings.field_types.

Tests Annotated field type boundary constraints, shared constant values,
and __all__ completeness.
"""

import pytest
from pydantic import BaseModel, ValidationError

from baldur.settings.field_types import (
    STANDARD_BACKOFF_MULTIPLIER,
    STANDARD_BASE_DELAY,
    STANDARD_BATCH_SIZE,
    STANDARD_CHECK_INTERVAL,
    STANDARD_JITTER_FACTOR,
    STANDARD_MAX_DELAY,
    STANDARD_POOL_SIZE,
    STANDARD_RETRY_COUNT,
    STANDARD_TIMEOUT_SECONDS,
    BackoffMultiplier,
    HugeCount,
    IntervalDuration,
    JitterFactor,
    LargeCount,
    LongDuration,
    MediumCount,
    MediumDuration,
    Percentage,
    Probability,
    ShortDuration,
    ShortInterval,
    SmallCount,
    StrictProbability,
    TinyCount,
    ZeroableSmallCount,
)

# =========================================================================
# Test helper models — one field per Annotated type for boundary testing
# =========================================================================


class _ProbabilityModel(BaseModel):
    v: Probability


class _StrictProbabilityModel(BaseModel):
    v: StrictProbability


class _PercentageModel(BaseModel):
    v: Percentage


class _TinyCountModel(BaseModel):
    v: TinyCount


class _SmallCountModel(BaseModel):
    v: SmallCount


class _MediumCountModel(BaseModel):
    v: MediumCount


class _LargeCountModel(BaseModel):
    v: LargeCount


class _HugeCountModel(BaseModel):
    v: HugeCount


class _ZeroableSmallCountModel(BaseModel):
    v: ZeroableSmallCount


class _ShortDurationModel(BaseModel):
    v: ShortDuration


class _MediumDurationModel(BaseModel):
    v: MediumDuration


class _LongDurationModel(BaseModel):
    v: LongDuration


class _IntervalDurationModel(BaseModel):
    v: IntervalDuration


class _ShortIntervalModel(BaseModel):
    v: ShortInterval


class _BackoffMultiplierModel(BaseModel):
    v: BackoffMultiplier


class _JitterFactorModel(BaseModel):
    v: JitterFactor


# =========================================================================
# Contract: Shared Default Constants
# =========================================================================


class TestFieldTypesConstantsContract:
    """Shared default constant values defined in doc 359."""

    def test_standard_retry_count_equals_three(self):
        """STANDARD_RETRY_COUNT = 3."""
        assert STANDARD_RETRY_COUNT == 3

    def test_standard_base_delay_equals_one(self):
        """STANDARD_BASE_DELAY = 1.0."""
        assert STANDARD_BASE_DELAY == 1.0

    def test_standard_max_delay_equals_sixty(self):
        """STANDARD_MAX_DELAY = 60.0 (aligned with CB recovery_timeout default)."""
        assert STANDARD_MAX_DELAY == 60.0

    def test_standard_backoff_multiplier_equals_two(self):
        """STANDARD_BACKOFF_MULTIPLIER = 2.0."""
        assert STANDARD_BACKOFF_MULTIPLIER == 2.0

    def test_standard_jitter_factor_equals_zero_point_two(self):
        """STANDARD_JITTER_FACTOR = 0.2."""
        assert STANDARD_JITTER_FACTOR == 0.2

    def test_standard_timeout_seconds_equals_thirty(self):
        """STANDARD_TIMEOUT_SECONDS = 30.0."""
        assert STANDARD_TIMEOUT_SECONDS == 30.0

    def test_standard_check_interval_equals_five(self):
        """STANDARD_CHECK_INTERVAL = 5.0."""
        assert STANDARD_CHECK_INTERVAL == 5.0

    def test_standard_batch_size_equals_hundred(self):
        """STANDARD_BATCH_SIZE = 100."""
        assert STANDARD_BATCH_SIZE == 100

    def test_standard_pool_size_equals_ten(self):
        """STANDARD_POOL_SIZE = 10."""
        assert STANDARD_POOL_SIZE == 10

    def test_constants_are_correct_types(self):
        """All constants have correct Python types."""
        assert isinstance(STANDARD_RETRY_COUNT, int)
        assert isinstance(STANDARD_BASE_DELAY, float)
        assert isinstance(STANDARD_MAX_DELAY, float)
        assert isinstance(STANDARD_BACKOFF_MULTIPLIER, float)
        assert isinstance(STANDARD_JITTER_FACTOR, float)
        assert isinstance(STANDARD_TIMEOUT_SECONDS, float)
        assert isinstance(STANDARD_CHECK_INTERVAL, float)
        assert isinstance(STANDARD_BATCH_SIZE, int)
        assert isinstance(STANDARD_POOL_SIZE, int)


# =========================================================================
# Contract: __all__ completeness
# =========================================================================


class TestFieldTypesExportsContract:
    """__all__ contains all 24 public names (15 types + 9 constants)."""

    def test_all_contains_all_annotated_types(self):
        """__all__ includes all 15 Annotated type names."""
        import baldur.settings.field_types as ft

        expected_types = [
            "Probability",
            "StrictProbability",
            "Percentage",
            "TinyCount",
            "SmallCount",
            "MediumCount",
            "LargeCount",
            "HugeCount",
            "ZeroableSmallCount",
            "ShortDuration",
            "MediumDuration",
            "LongDuration",
            "IntervalDuration",
            "ShortInterval",
            "BackoffMultiplier",
            "JitterFactor",
        ]
        for name in expected_types:
            assert name in ft.__all__, f"{name} missing from __all__"

    def test_all_contains_all_constants(self):
        """__all__ includes all 9 shared constants."""
        import baldur.settings.field_types as ft

        expected_constants = [
            "STANDARD_RETRY_COUNT",
            "STANDARD_BASE_DELAY",
            "STANDARD_MAX_DELAY",
            "STANDARD_BACKOFF_MULTIPLIER",
            "STANDARD_JITTER_FACTOR",
            "STANDARD_TIMEOUT_SECONDS",
            "STANDARD_CHECK_INTERVAL",
            "STANDARD_BATCH_SIZE",
            "STANDARD_POOL_SIZE",
        ]
        for name in expected_constants:
            assert name in ft.__all__, f"{name} missing from __all__"

    def test_all_has_exactly_25_entries(self):
        """__all__ has 16 types + 9 constants = 25 entries."""
        import baldur.settings.field_types as ft

        assert len(ft.__all__) == 25


# =========================================================================
# Contract: Annotated Type Boundary Values
# =========================================================================


class TestProbabilityBoundaryContract:
    """Probability (float, ge=0.0, le=1.0) boundary verification."""

    def test_minimum_boundary_accepts_zero(self):
        """ge=0.0: value 0.0 is accepted."""
        m = _ProbabilityModel(v=0.0)
        assert m.v == 0.0

    def test_below_minimum_rejects_negative(self):
        """ge=0.0: value -0.01 is rejected."""
        with pytest.raises(ValidationError):
            _ProbabilityModel(v=-0.01)

    def test_maximum_boundary_accepts_one(self):
        """le=1.0: value 1.0 is accepted."""
        m = _ProbabilityModel(v=1.0)
        assert m.v == 1.0

    def test_above_maximum_rejects(self):
        """le=1.0: value 1.01 is rejected."""
        with pytest.raises(ValidationError):
            _ProbabilityModel(v=1.01)


class TestStrictProbabilityBoundaryContract:
    """StrictProbability (float, ge=0.01, le=1.0) boundary verification."""

    def test_minimum_boundary_accepts_zero_point_zero_one(self):
        """ge=0.01: value 0.01 is accepted."""
        m = _StrictProbabilityModel(v=0.01)
        assert m.v == 0.01

    def test_below_minimum_rejects_zero(self):
        """ge=0.01: value 0.0 is rejected."""
        with pytest.raises(ValidationError):
            _StrictProbabilityModel(v=0.0)

    def test_maximum_boundary_accepts_one(self):
        """le=1.0: value 1.0 is accepted."""
        m = _StrictProbabilityModel(v=1.0)
        assert m.v == 1.0


class TestPercentageBoundaryContract:
    """Percentage (float, ge=0.0, le=100.0) boundary verification."""

    def test_minimum_boundary_accepts_zero(self):
        """ge=0.0: value 0.0 is accepted."""
        m = _PercentageModel(v=0.0)
        assert m.v == 0.0

    def test_maximum_boundary_accepts_hundred(self):
        """le=100.0: value 100.0 is accepted."""
        m = _PercentageModel(v=100.0)
        assert m.v == 100.0

    def test_above_maximum_rejects(self):
        """le=100.0: value 100.01 is rejected."""
        with pytest.raises(ValidationError):
            _PercentageModel(v=100.01)


class TestCountTypeBoundaryContract:
    """Count types (TinyCount through ZeroableSmallCount) boundary verification."""

    @pytest.mark.parametrize(
        ("model_cls", "ge", "le"),
        [
            (_TinyCountModel, 1, 10),
            (_SmallCountModel, 1, 20),
            (_MediumCountModel, 1, 100),
            (_LargeCountModel, 1, 1000),
            (_HugeCountModel, 1, 10000),
        ],
        ids=["TinyCount", "SmallCount", "MediumCount", "LargeCount", "HugeCount"],
    )
    def test_positive_count_minimum_boundary(self, model_cls, ge, le):
        """Minimum boundary (ge) accepts the boundary value."""
        m = model_cls(v=ge)
        assert m.v == ge

    @pytest.mark.parametrize(
        ("model_cls", "ge", "le"),
        [
            (_TinyCountModel, 1, 10),
            (_SmallCountModel, 1, 20),
            (_MediumCountModel, 1, 100),
            (_LargeCountModel, 1, 1000),
            (_HugeCountModel, 1, 10000),
        ],
        ids=["TinyCount", "SmallCount", "MediumCount", "LargeCount", "HugeCount"],
    )
    def test_positive_count_below_minimum_rejects(self, model_cls, ge, le):
        """Below minimum boundary (ge-1) is rejected."""
        with pytest.raises(ValidationError):
            model_cls(v=ge - 1)

    @pytest.mark.parametrize(
        ("model_cls", "ge", "le"),
        [
            (_TinyCountModel, 1, 10),
            (_SmallCountModel, 1, 20),
            (_MediumCountModel, 1, 100),
            (_LargeCountModel, 1, 1000),
            (_HugeCountModel, 1, 10000),
        ],
        ids=["TinyCount", "SmallCount", "MediumCount", "LargeCount", "HugeCount"],
    )
    def test_positive_count_maximum_boundary(self, model_cls, ge, le):
        """Maximum boundary (le) accepts the boundary value."""
        m = model_cls(v=le)
        assert m.v == le

    @pytest.mark.parametrize(
        ("model_cls", "ge", "le"),
        [
            (_TinyCountModel, 1, 10),
            (_SmallCountModel, 1, 20),
            (_MediumCountModel, 1, 100),
            (_LargeCountModel, 1, 1000),
            (_HugeCountModel, 1, 10000),
        ],
        ids=["TinyCount", "SmallCount", "MediumCount", "LargeCount", "HugeCount"],
    )
    def test_positive_count_above_maximum_rejects(self, model_cls, ge, le):
        """Above maximum boundary (le+1) is rejected."""
        with pytest.raises(ValidationError):
            model_cls(v=le + 1)

    def test_zeroable_small_count_accepts_zero(self):
        """ZeroableSmallCount ge=0: value 0 is accepted."""
        m = _ZeroableSmallCountModel(v=0)
        assert m.v == 0

    def test_zeroable_small_count_rejects_negative(self):
        """ZeroableSmallCount ge=0: value -1 is rejected."""
        with pytest.raises(ValidationError):
            _ZeroableSmallCountModel(v=-1)

    def test_zeroable_small_count_rejects_above_ten(self):
        """ZeroableSmallCount le=10: value 11 is rejected."""
        with pytest.raises(ValidationError):
            _ZeroableSmallCountModel(v=11)


class TestDurationTypeBoundaryContract:
    """Duration types boundary verification."""

    @pytest.mark.parametrize(
        ("model_cls", "ge", "le", "label"),
        [
            (_ShortDurationModel, 0.1, 60.0, "ShortDuration"),
            (_MediumDurationModel, 1.0, 600.0, "MediumDuration"),
            (_LongDurationModel, 1.0, 3600.0, "LongDuration"),
        ],
    )
    def test_float_duration_minimum_boundary(self, model_cls, ge, le, label):
        """Float duration minimum boundary (ge) accepted."""
        m = model_cls(v=ge)
        assert m.v == ge

    @pytest.mark.parametrize(
        ("model_cls", "ge", "le", "label"),
        [
            (_ShortDurationModel, 0.1, 60.0, "ShortDuration"),
            (_MediumDurationModel, 1.0, 600.0, "MediumDuration"),
            (_LongDurationModel, 1.0, 3600.0, "LongDuration"),
        ],
    )
    def test_float_duration_maximum_boundary(self, model_cls, ge, le, label):
        """Float duration maximum boundary (le) accepted."""
        m = model_cls(v=le)
        assert m.v == le

    @pytest.mark.parametrize(
        ("model_cls", "ge", "le", "label"),
        [
            (_ShortDurationModel, 0.1, 60.0, "ShortDuration"),
            (_MediumDurationModel, 1.0, 600.0, "MediumDuration"),
            (_LongDurationModel, 1.0, 3600.0, "LongDuration"),
        ],
    )
    def test_float_duration_above_maximum_rejects(self, model_cls, ge, le, label):
        """Float duration above maximum boundary rejects."""
        with pytest.raises(ValidationError):
            model_cls(v=le + 0.01)

    def test_interval_duration_minimum_boundary(self):
        """IntervalDuration ge=1: value 1 accepted."""
        m = _IntervalDurationModel(v=1)
        assert m.v == 1

    def test_interval_duration_maximum_boundary(self):
        """IntervalDuration le=3600: value 3600 accepted."""
        m = _IntervalDurationModel(v=3600)
        assert m.v == 3600

    def test_interval_duration_above_maximum_rejects(self):
        """IntervalDuration le=3600: value 3601 rejected."""
        with pytest.raises(ValidationError):
            _IntervalDurationModel(v=3601)

    def test_short_interval_minimum_boundary(self):
        """ShortInterval ge=1: value 1 accepted."""
        m = _ShortIntervalModel(v=1)
        assert m.v == 1

    def test_short_interval_maximum_boundary(self):
        """ShortInterval le=60: value 60 accepted."""
        m = _ShortIntervalModel(v=60)
        assert m.v == 60

    def test_short_interval_above_maximum_rejects(self):
        """ShortInterval le=60: value 61 rejected."""
        with pytest.raises(ValidationError):
            _ShortIntervalModel(v=61)


class TestMultiplierTypeBoundaryContract:
    """BackoffMultiplier and JitterFactor boundary verification."""

    def test_backoff_multiplier_minimum_boundary(self):
        """BackoffMultiplier ge=1.0: value 1.0 accepted."""
        m = _BackoffMultiplierModel(v=1.0)
        assert m.v == 1.0

    def test_backoff_multiplier_below_minimum_rejects(self):
        """BackoffMultiplier ge=1.0: value 0.99 rejected."""
        with pytest.raises(ValidationError):
            _BackoffMultiplierModel(v=0.99)

    def test_backoff_multiplier_maximum_boundary(self):
        """BackoffMultiplier le=10.0: value 10.0 accepted."""
        m = _BackoffMultiplierModel(v=10.0)
        assert m.v == 10.0

    def test_backoff_multiplier_above_maximum_rejects(self):
        """BackoffMultiplier le=10.0: value 10.01 rejected."""
        with pytest.raises(ValidationError):
            _BackoffMultiplierModel(v=10.01)

    def test_jitter_factor_minimum_boundary(self):
        """JitterFactor ge=0.0: value 0.0 accepted."""
        m = _JitterFactorModel(v=0.0)
        assert m.v == 0.0

    def test_jitter_factor_maximum_boundary(self):
        """JitterFactor le=1.0: value 1.0 accepted."""
        m = _JitterFactorModel(v=1.0)
        assert m.v == 1.0

    def test_jitter_factor_above_maximum_rejects(self):
        """JitterFactor le=1.0: value 1.01 rejected."""
        with pytest.raises(ValidationError):
            _JitterFactorModel(v=1.01)
