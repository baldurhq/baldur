"""
PrecomputedCacheSettings new fields tests (doc 445).

Covers:
- Contract: default values and boundary constraints for CB/jitter/backoff fields
- Behavior: field validation boundary analysis
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.precomputed_cache import PrecomputedCacheSettings


class TestPrecomputedCacheSettings445Contract:
    """Contract verification for 445 new settings fields."""

    def test_l3_cb_enabled_default_true(self):
        """l3_cb_enabled default: True (doc 445 D3)."""
        settings = PrecomputedCacheSettings()
        assert settings.l3_cb_enabled is True

    def test_l3_cb_failure_threshold_default_3(self):
        """l3_cb_failure_threshold default: 3 (doc 445 D3)."""
        settings = PrecomputedCacheSettings()
        assert settings.l3_cb_failure_threshold == 3

    def test_l3_cb_recovery_timeout_default_30(self):
        """l3_cb_recovery_timeout default: 30 (doc 445 D3)."""
        settings = PrecomputedCacheSettings()
        assert settings.l3_cb_recovery_timeout == 30

    def test_jitter_enabled_default_true(self):
        """jitter_enabled default: True (doc 445 G2)."""
        settings = PrecomputedCacheSettings()
        assert settings.jitter_enabled is True

    def test_backoff_max_delay_seconds_default_300(self):
        """backoff_max_delay_seconds default: 300.0 (doc 445 G2)."""
        settings = PrecomputedCacheSettings()
        assert settings.backoff_max_delay_seconds == 300.0


class TestPrecomputedCacheSettings445Boundary:
    """Boundary analysis for 445 new settings field constraints."""

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (0, False),
            (1, True),
            (20, True),
            (21, False),
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_l3_cb_failure_threshold_boundary(self, value, should_pass):
        """l3_cb_failure_threshold boundary: ge=1, le=20."""
        if should_pass:
            s = PrecomputedCacheSettings(l3_cb_failure_threshold=value)
            assert s.l3_cb_failure_threshold == value
        else:
            with pytest.raises(ValidationError):
                PrecomputedCacheSettings(l3_cb_failure_threshold=value)

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (4, False),
            (5, True),
            (600, True),
            (601, False),
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_l3_cb_recovery_timeout_boundary(self, value, should_pass):
        """l3_cb_recovery_timeout boundary: ge=5, le=600."""
        if should_pass:
            s = PrecomputedCacheSettings(l3_cb_recovery_timeout=value)
            assert s.l3_cb_recovery_timeout == value
        else:
            with pytest.raises(ValidationError):
                PrecomputedCacheSettings(l3_cb_recovery_timeout=value)

    @pytest.mark.parametrize(
        ("value", "should_pass"),
        [
            (0.9, False),
            (1.0, True),
            (3600.0, True),
            (3600.1, False),
        ],
        ids=["below_min", "at_min", "at_max", "above_max"],
    )
    def test_backoff_max_delay_seconds_boundary(self, value, should_pass):
        """backoff_max_delay_seconds boundary: ge=1.0, le=3600.0."""
        if should_pass:
            s = PrecomputedCacheSettings(backoff_max_delay_seconds=value)
            assert s.backoff_max_delay_seconds == value
        else:
            with pytest.raises(ValidationError):
                PrecomputedCacheSettings(backoff_max_delay_seconds=value)
