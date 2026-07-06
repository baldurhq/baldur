"""
DLQ Size Limit Settings Unit Tests (329_DLQ_SIZE_LIMIT).

Test targets:
    - baldur.settings.dlq.DLQSettings (size limit fields)

Test Categories:
    A. Contract: Default values from design doc
    B. Behavior: Boundary validation (ge/le constraints)
    C. Behavior: overflow_strategy regex pattern validation
"""

import pytest
from pydantic import ValidationError

from baldur.settings.dlq import DLQSettings

# =============================================================================
# A. Contract Tests — Default values
# =============================================================================


class TestDLQSizeLimitSettingsContract:
    """DLQ size limit settings design contract values."""

    def test_max_size_default(self):
        """max_size default is 100,000."""
        settings = DLQSettings()
        assert settings.max_size == 100_000

    def test_max_size_per_domain_default(self):
        """max_size_per_domain default is 20,000."""
        settings = DLQSettings()
        assert settings.max_size_per_domain == 20_000

    def test_overflow_strategy_default(self):
        """overflow_strategy default is 'drop_oldest'."""
        settings = DLQSettings()
        assert settings.overflow_strategy == "drop_oldest"

    def test_emergency_purge_threshold_default(self):
        """emergency_purge_threshold default is 0.8."""
        settings = DLQSettings()
        assert settings.emergency_purge_threshold == 0.8

    def test_overflow_evict_batch_size_default(self):
        """overflow_evict_batch_size default is 500."""
        settings = DLQSettings()
        assert settings.overflow_evict_batch_size == 500


# =============================================================================
# B. Behavior Tests — Boundary Validation
# =============================================================================


class TestDLQMaxSizeBoundaryBehavior:
    """max_size field boundary validation."""

    def test_max_size_minimum_boundary_accepted(self):
        """max_size=1000 (ge=1000) is accepted."""
        settings = DLQSettings(max_size=1_000)
        assert settings.max_size == 1_000

    def test_max_size_below_minimum_raises(self):
        """max_size=999 (below ge=1000) raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(max_size=999)

    def test_max_size_maximum_boundary_accepted(self):
        """max_size=10,000,000 (le=10000000) is accepted."""
        settings = DLQSettings(max_size=10_000_000)
        assert settings.max_size == 10_000_000

    def test_max_size_above_maximum_raises(self):
        """max_size=10,000,001 (above le=10000000) raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(max_size=10_000_001)


class TestDLQMaxSizePerDomainBoundaryBehavior:
    """max_size_per_domain field boundary validation."""

    def test_max_size_per_domain_minimum_boundary_accepted(self):
        """max_size_per_domain=100 (ge=100) is accepted."""
        settings = DLQSettings(max_size_per_domain=100)
        assert settings.max_size_per_domain == 100

    def test_max_size_per_domain_below_minimum_raises(self):
        """max_size_per_domain=99 (below ge=100) raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(max_size_per_domain=99)

    def test_max_size_per_domain_maximum_boundary_accepted(self):
        """max_size_per_domain=1,000,000 (le=1000000) is accepted."""
        settings = DLQSettings(max_size_per_domain=1_000_000)
        assert settings.max_size_per_domain == 1_000_000

    def test_max_size_per_domain_above_maximum_raises(self):
        """max_size_per_domain=1,000,001 raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(max_size_per_domain=1_000_001)


class TestDLQEmergencyPurgeThresholdBoundaryBehavior:
    """emergency_purge_threshold field boundary validation."""

    def test_emergency_purge_threshold_minimum_boundary_accepted(self):
        """emergency_purge_threshold=0.5 (ge=0.5) is accepted."""
        settings = DLQSettings(emergency_purge_threshold=0.5)
        assert settings.emergency_purge_threshold == 0.5

    def test_emergency_purge_threshold_below_minimum_raises(self):
        """emergency_purge_threshold=0.49 raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(emergency_purge_threshold=0.49)

    def test_emergency_purge_threshold_maximum_boundary_accepted(self):
        """emergency_purge_threshold=1.0 (le=1.0) is accepted."""
        settings = DLQSettings(emergency_purge_threshold=1.0)
        assert settings.emergency_purge_threshold == 1.0

    def test_emergency_purge_threshold_above_maximum_raises(self):
        """emergency_purge_threshold=1.01 raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(emergency_purge_threshold=1.01)


class TestDLQOverflowEvictBatchSizeBoundaryBehavior:
    """overflow_evict_batch_size field boundary validation."""

    def test_overflow_evict_batch_size_minimum_boundary_accepted(self):
        """overflow_evict_batch_size=100 (ge=100) is accepted."""
        settings = DLQSettings(overflow_evict_batch_size=100)
        assert settings.overflow_evict_batch_size == 100

    def test_overflow_evict_batch_size_below_minimum_raises(self):
        """overflow_evict_batch_size=99 raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(overflow_evict_batch_size=99)

    def test_overflow_evict_batch_size_maximum_boundary_accepted(self):
        """overflow_evict_batch_size=50,000 (le=50000) is accepted."""
        settings = DLQSettings(overflow_evict_batch_size=50_000)
        assert settings.overflow_evict_batch_size == 50_000

    def test_overflow_evict_batch_size_above_maximum_raises(self):
        """overflow_evict_batch_size=50,001 raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(overflow_evict_batch_size=50_001)


# =============================================================================
# C. Behavior Tests — overflow_strategy pattern validation
# =============================================================================


class TestDLQOverflowStrategyPatternBehavior:
    """overflow_strategy regex pattern validation."""

    def test_drop_oldest_strategy_accepted(self):
        """'drop_oldest' is a valid overflow strategy."""
        settings = DLQSettings(overflow_strategy="drop_oldest")
        assert settings.overflow_strategy == "drop_oldest"

    def test_reject_strategy_accepted(self):
        """'reject' is a valid overflow strategy."""
        settings = DLQSettings(overflow_strategy="reject")
        assert settings.overflow_strategy == "reject"

    def test_compress_oldest_strategy_accepted(self):
        """'compress_oldest' is a valid overflow strategy."""
        settings = DLQSettings(overflow_strategy="compress_oldest")
        assert settings.overflow_strategy == "compress_oldest"

    def test_invalid_strategy_raises(self):
        """Invalid strategy string raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(overflow_strategy="invalid_strategy")

    def test_empty_strategy_raises(self):
        """Empty string strategy raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(overflow_strategy="")

    def test_partial_match_strategy_raises(self):
        """Partial match 'drop' raises ValidationError."""
        with pytest.raises(ValidationError):
            DLQSettings(overflow_strategy="drop")
