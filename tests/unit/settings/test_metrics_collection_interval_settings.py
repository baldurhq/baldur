"""``MetricsSettings.collection_interval_seconds`` — the collection cadence knob.

The interval is the one operator-facing number that couples to the bundled
alert rules: the staleness threshold is five times it, and the DLQ paging
expressions read a window that must stay at least twice it. So the default and
both bounds are spec values, hardcoded here rather than derived.

Reference:
    src/baldur/settings/metrics.py
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.metrics import MetricsSettings


class TestMetricsCollectionIntervalSettingsContract:
    """Default and bounds of the collection interval, as specified."""

    def test_collection_interval_default_is_sixty_seconds(self):
        """60s keeps the alert ``for:`` windows sampling a moving series."""
        assert MetricsSettings().collection_interval_seconds == 60.0

    def test_collection_interval_lower_bound_is_accepted(self):
        """5s is the floor that bounds repository read pressure."""
        assert (
            MetricsSettings(collection_interval_seconds=5.0).collection_interval_seconds
            == 5.0
        )

    def test_collection_interval_below_the_lower_bound_is_rejected(self):
        """Just under the floor fails — the bound is real, not advisory."""
        with pytest.raises(ValidationError):
            MetricsSettings(collection_interval_seconds=4.9)

    def test_collection_interval_upper_bound_is_accepted(self):
        """3600s lets a cost-sensitive operator trade staleness for load."""
        assert (
            MetricsSettings(
                collection_interval_seconds=3600.0
            ).collection_interval_seconds
            == 3600.0
        )

    def test_collection_interval_above_the_upper_bound_is_rejected(self):
        """Just over the ceiling fails."""
        with pytest.raises(ValidationError):
            MetricsSettings(collection_interval_seconds=3600.1)


class TestMetricsCollectionIntervalEnvBehavior:
    """The documented environment variable resolves to the field."""

    def test_collection_interval_reads_its_env_var(self, monkeypatch):
        """``BALDUR_METRICS_COLLECTION_INTERVAL_SECONDS`` is the operator surface."""
        monkeypatch.setenv("BALDUR_METRICS_COLLECTION_INTERVAL_SECONDS", "30")

        assert MetricsSettings().collection_interval_seconds == 30.0

    def test_collection_interval_env_value_is_bound_checked(self, monkeypatch):
        """An out-of-range env value fails loudly instead of being clamped."""
        monkeypatch.setenv("BALDUR_METRICS_COLLECTION_INTERVAL_SECONDS", "1")

        with pytest.raises(ValidationError):
            MetricsSettings()
