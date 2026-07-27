"""
X-Test regional boundary metric unit tests.

Covers:
- xtest_cross_region_denied_total definition and recording
- xtest_global_scope_requests_total definition and recording
"""

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs


class _RaisingCollector:
    """A metric double that records the touch it then refuses to serve.

    Patching a Prometheus collector with ``side_effect`` does nothing: the
    recorder never calls the collector, it calls ``.labels(...)`` on it, so the
    side effect never fires and the fail-open arm never runs. The double raises
    from the attribute the recorder actually reaches, and remembers that it was
    reached so the test can prove the fault happened at all.
    """

    def __init__(self) -> None:
        self.touched = False

    def labels(self, **_kwargs: object) -> None:
        self.touched = True
        raise RuntimeError("metrics registry down")


class TestXTestRegionalMetricDefinitions:
    """X-Test regional metric definitions."""

    def test_xtest_cross_region_denied_metric_defined(self):
        """xtest_cross_region_denied_total is defined."""
        from baldur.services.metrics.recorders import (
            _xtest_cross_region_denied_total as xtest_cross_region_denied_total,
        )

        assert xtest_cross_region_denied_total is not None
        assert hasattr(xtest_cross_region_denied_total, "labels")

    def test_xtest_global_scope_requests_metric_defined(self):
        """xtest_global_scope_requests_total is defined."""
        from baldur.services.metrics.recorders import (
            _xtest_global_scope_requests_total as xtest_global_scope_requests_total,
        )

        assert xtest_global_scope_requests_total is not None
        assert hasattr(xtest_global_scope_requests_total, "labels")

    def test_cross_region_denied_metric_labels(self):
        """xtest_cross_region_denied_total carries the right labels."""
        from baldur.services.metrics.recorders import (
            _xtest_cross_region_denied_total as xtest_cross_region_denied_total,
        )

        # The labels call must not raise
        labeled = xtest_cross_region_denied_total.labels(
            current_region="seoul",
            target_region="tokyo",
        )
        assert labeled is not None

    def test_global_scope_requests_metric_labels(self):
        """xtest_global_scope_requests_total carries the right labels."""
        from baldur.services.metrics.recorders import (
            _xtest_global_scope_requests_total as xtest_global_scope_requests_total,
        )

        # The labels call must not raise
        labeled = xtest_global_scope_requests_total.labels(
            endpoint_pattern="emergency",
            region="seoul",
            result="allowed",
        )
        assert labeled is not None


class TestXTestRegionalMetricRecorders:
    """X-Test regional metric recording functions."""

    def test_record_xtest_cross_region_denied_exists(self):
        """record_xtest_cross_region_denied exists."""
        from baldur.services.metrics.recorders import (
            record_xtest_cross_region_denied,
        )

        assert callable(record_xtest_cross_region_denied)

    def test_record_xtest_global_scope_request_exists(self):
        """record_xtest_global_scope_request exists."""
        from baldur.services.metrics.recorders import (
            record_xtest_global_scope_request,
        )

        assert callable(record_xtest_global_scope_request)

    @patch("baldur.services.metrics.recorders._xtest_cross_region_denied_total")
    def test_record_xtest_cross_region_denied_increments(self, mock_metric):
        """record_xtest_cross_region_denied increments the counter."""
        from baldur.services.metrics.recorders import (
            record_xtest_cross_region_denied,
        )

        mock_labeled = MagicMock()
        mock_metric.labels.return_value = mock_labeled

        record_xtest_cross_region_denied(
            current_region="seoul",
            target_region="tokyo",
        )

        mock_metric.labels.assert_called_once_with(
            current_region="seoul",
            target_region="tokyo",
        )
        mock_labeled.inc.assert_called_once()

    @patch("baldur.services.metrics.recorders._xtest_global_scope_requests_total")
    def test_record_xtest_global_scope_request_allowed(self, mock_metric):
        """record_xtest_global_scope_request records an allowed request."""
        from baldur.services.metrics.recorders import (
            record_xtest_global_scope_request,
        )

        mock_labeled = MagicMock()
        mock_metric.labels.return_value = mock_labeled

        record_xtest_global_scope_request(
            endpoint_pattern="emergency",
            region="seoul",
            result="allowed",
        )

        mock_metric.labels.assert_called_once_with(
            endpoint_pattern="emergency",
            region="seoul",
            result="allowed",
        )
        mock_labeled.inc.assert_called_once()

    @patch("baldur.services.metrics.recorders._xtest_global_scope_requests_total")
    def test_record_xtest_global_scope_request_denied(self, mock_metric):
        """record_xtest_global_scope_request records a denied request."""
        from baldur.services.metrics.recorders import (
            record_xtest_global_scope_request,
        )

        mock_labeled = MagicMock()
        mock_metric.labels.return_value = mock_labeled

        record_xtest_global_scope_request(
            endpoint_pattern="isolation",
            region="seoul",
            result="denied_mismatch",
        )

        mock_metric.labels.assert_called_once_with(
            endpoint_pattern="isolation",
            region="seoul",
            result="denied_mismatch",
        )
        mock_labeled.inc.assert_called_once()

    @pytest.mark.parametrize(
        ("collector_name", "args", "event"),
        [
            (
                "_xtest_cross_region_denied_total",
                ("seoul", "tokyo"),
                "metrics.record_cross_region_failed",
            ),
            (
                "_xtest_global_scope_requests_total",
                ("emergency", "seoul", "allowed"),
                "metrics.record_global_scope_failed",
            ),
        ],
        ids=["cross_region_denied", "global_scope_request"],
    )
    def test_record_functions_swallow_a_broken_collector_and_warn(
        self, collector_name, args, event
    ):
        """A registry fault is swallowed, and the swallow is visible at WARNING.

        The double raises from ``.labels(...)`` — the attribute the recorder
        actually reaches — because patching the collector itself with a
        ``side_effect`` never fires: production never calls the collector, it
        calls ``.labels()`` on it.
        """
        from baldur.services.metrics import recorders

        record_fn = {
            "_xtest_cross_region_denied_total": (
                recorders.record_xtest_cross_region_denied
            ),
            "_xtest_global_scope_requests_total": (
                recorders.record_xtest_global_scope_request
            ),
        }[collector_name]

        collector = _RaisingCollector()
        with (
            patch(
                f"baldur.services.metrics.recorders.{collector_name}",
                collector,
            ),
            capture_logs() as logs,
        ):
            record_fn(*args)

        assert collector.touched, "the fail-open arm was never reached"
        record = next(log for log in logs if log["event"] == event)
        assert record["log_level"] == "warning"


class TestXTestModeMixinMetricsIntegration:
    """XTestModeMixin metric integration.

    Exercises the metric wiring without Django settings by reimplementing the
    _get_endpoint_pattern_name / _record_regional_scope_metrics logic here.
    """

    def _get_endpoint_pattern_name(self, request) -> str:
        """Extract the endpoint pattern name (mirrors XTestModeMixin)."""
        path = getattr(request, "path", "")
        if "emergency" in path:
            return "emergency"
        elif "isolation" in path:
            return "isolation"
        elif "governance" in path:
            return "governance"
        return "unknown"

    def _record_regional_scope_metrics(
        self, request, current_region: str, target_region: str | None, result: str
    ) -> None:
        """Record regional scope metrics (mirrors XTestModeMixin)."""
        from baldur.services.metrics.recorders import (
            record_xtest_cross_region_denied,
            record_xtest_global_scope_request,
        )

        endpoint_pattern = self._get_endpoint_pattern_name(request)

        record_xtest_global_scope_request(
            endpoint_pattern=endpoint_pattern,
            region=current_region,
            result=result,
        )

        if result == "denied_mismatch" and target_region:
            record_xtest_cross_region_denied(
                current_region=current_region,
                target_region=target_region,
            )

    def test_get_endpoint_pattern_name_emergency(self):
        """The emergency endpoint pattern is detected."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/emergency/global/set/"

        result = self._get_endpoint_pattern_name(request)
        assert result == "emergency"

    def test_get_endpoint_pattern_name_isolation(self):
        """The isolation endpoint pattern is detected."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/isolation/region/isolate/"

        result = self._get_endpoint_pattern_name(request)
        assert result == "isolation"

    def test_get_endpoint_pattern_name_governance(self):
        """The governance endpoint pattern is detected."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/governance/global/update/"

        result = self._get_endpoint_pattern_name(request)
        assert result == "governance"

    def test_get_endpoint_pattern_name_unknown(self):
        """An unrecognized endpoint resolves to unknown."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/dlq/inject/"

        result = self._get_endpoint_pattern_name(request)
        assert result == "unknown"

    @patch("baldur.services.metrics.recorders.record_xtest_global_scope_request")
    @patch("baldur.services.metrics.recorders.record_xtest_cross_region_denied")
    def test_record_regional_scope_metrics_allowed(
        self,
        mock_denied,
        mock_global,
    ):
        """An allowed request is recorded."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/emergency/global/set/"

        self._record_regional_scope_metrics(request, "seoul", "seoul", "allowed")

        mock_global.assert_called_once_with(
            endpoint_pattern="emergency",
            region="seoul",
            result="allowed",
        )
        mock_denied.assert_not_called()

    @patch("baldur.services.metrics.recorders.record_xtest_global_scope_request")
    @patch("baldur.services.metrics.recorders.record_xtest_cross_region_denied")
    def test_record_regional_scope_metrics_denied_mismatch(
        self,
        mock_denied,
        mock_global,
    ):
        """A region-mismatch denial is recorded."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/isolation/region/isolate/"

        self._record_regional_scope_metrics(
            request, "seoul", "tokyo", "denied_mismatch"
        )

        mock_global.assert_called_once_with(
            endpoint_pattern="isolation",
            region="seoul",
            result="denied_mismatch",
        )
        mock_denied.assert_called_once_with(
            current_region="seoul",
            target_region="tokyo",
        )

    @patch("baldur.services.metrics.recorders.record_xtest_global_scope_request")
    @patch("baldur.services.metrics.recorders.record_xtest_cross_region_denied")
    def test_record_regional_scope_metrics_no_header(
        self,
        mock_denied,
        mock_global,
    ):
        """A missing-header denial is recorded."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/governance/global/update/"

        self._record_regional_scope_metrics(request, "seoul", None, "denied_no_header")

        mock_global.assert_called_once_with(
            endpoint_pattern="governance",
            region="seoul",
            result="denied_no_header",
        )
        # target_region is None, so no cross-region metric is recorded
        mock_denied.assert_not_called()
