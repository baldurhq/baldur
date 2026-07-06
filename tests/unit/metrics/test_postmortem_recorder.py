"""
PostmortemMetricRecorder Unit Tests (408 — C5).

Test targets:
    - baldur.metrics.recorders.postmortem.PostmortemMetricRecorder
    - Module-level convenience functions (DD-7)
    - Facade registration in BaldurMetrics

Reference:
    408
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def postmortem_recorder():
    from baldur.metrics.recorders.postmortem import PostmortemMetricRecorder

    return PostmortemMetricRecorder()


class TestPostmortemRecorderContract:
    """C5: PostmortemMetricRecorder export contract."""

    def test_exports_class_and_convenience_function(self):
        """__all__ includes class + 1 convenience function."""
        from baldur.metrics.recorders.postmortem import __all__

        assert "PostmortemMetricRecorder" in __all__
        assert "record_postmortem_generated" in __all__


class TestPostmortemRecorderBehavior:
    """C5: PostmortemMetricRecorder methods do not raise."""

    def test_record_generated_auto(self, postmortem_recorder):
        """record_generated with 'auto' type does not raise."""
        postmortem_recorder.record_generated("auto")

    def test_record_generated_group(self, postmortem_recorder):
        """record_generated with 'group' type does not raise."""
        postmortem_recorder.record_generated("group")

    def test_record_generated_emergency(self, postmortem_recorder):
        """record_generated with 'emergency' type does not raise."""
        postmortem_recorder.record_generated("emergency")


class TestPostmortemConvenienceFunctionsBehavior:
    """DD-7: Postmortem convenience function delegates to lazy recorder."""

    def test_record_postmortem_generated_delegates(self):
        """record_postmortem_generated delegates to recorder.record_generated."""
        from baldur.metrics.recorders.postmortem import record_postmortem_generated

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.postmortem._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_postmortem_generated("auto")
        mock_recorder.record_generated.assert_called_once_with("auto")


class TestPostmortemFacadeRegistrationContract:
    """PostmortemMetricRecorder registered in BaldurMetrics facade."""

    def test_facade_has_postmortem_attribute(self):
        """BaldurMetrics exposes postmortem recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.postmortem import PostmortemMetricRecorder

        m = get_metrics()
        assert isinstance(m.postmortem, PostmortemMetricRecorder)
