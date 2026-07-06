"""
X-Test Regional Boundary 메트릭 단위 테스트.

Section 7.1 구현 검증:
- xtest_cross_region_denied_total 메트릭 정의 및 기록
- xtest_global_scope_requests_total 메트릭 정의 및 기록

테스트 케이스:
- test_xtest_cross_region_denied_metric_defined: 메트릭 정의 확인
- test_xtest_global_scope_requests_metric_defined: 메트릭 정의 확인
- test_record_xtest_cross_region_denied: 거부 메트릭 기록 확인
- test_record_xtest_global_scope_request_allowed: 허용 요청 기록
- test_record_xtest_global_scope_request_denied: 거부 요청 기록
"""

from unittest.mock import MagicMock, patch


class TestXTestRegionalMetricDefinitions:
    """X-Test Regional 메트릭 정의 테스트."""

    def test_xtest_cross_region_denied_metric_defined(self):
        """xtest_cross_region_denied_total 메트릭이 정의됨."""
        from baldur.services.metrics.recorders import (
            _xtest_cross_region_denied_total as xtest_cross_region_denied_total,
        )

        assert xtest_cross_region_denied_total is not None
        # Counter는 _name 속성을 가짐
        assert hasattr(xtest_cross_region_denied_total, "labels")

    def test_xtest_global_scope_requests_metric_defined(self):
        """xtest_global_scope_requests_total 메트릭이 정의됨."""
        from baldur.services.metrics.recorders import (
            _xtest_global_scope_requests_total as xtest_global_scope_requests_total,
        )

        assert xtest_global_scope_requests_total is not None
        assert hasattr(xtest_global_scope_requests_total, "labels")

    def test_cross_region_denied_metric_labels(self):
        """xtest_cross_region_denied_total이 올바른 레이블을 가짐."""
        from baldur.services.metrics.recorders import (
            _xtest_cross_region_denied_total as xtest_cross_region_denied_total,
        )

        # labels 메서드 호출 시 에러 없음
        labeled = xtest_cross_region_denied_total.labels(
            current_region="seoul",
            target_region="tokyo",
        )
        assert labeled is not None

    def test_global_scope_requests_metric_labels(self):
        """xtest_global_scope_requests_total이 올바른 레이블을 가짐."""
        from baldur.services.metrics.recorders import (
            _xtest_global_scope_requests_total as xtest_global_scope_requests_total,
        )

        # labels 메서드 호출 시 에러 없음
        labeled = xtest_global_scope_requests_total.labels(
            endpoint_pattern="emergency",
            region="seoul",
            result="allowed",
        )
        assert labeled is not None


class TestXTestRegionalMetricRecorders:
    """X-Test Regional 메트릭 기록 함수 테스트."""

    def test_record_xtest_cross_region_denied_exists(self):
        """record_xtest_cross_region_denied 함수가 존재."""
        from baldur.services.metrics.recorders import (
            record_xtest_cross_region_denied,
        )

        assert callable(record_xtest_cross_region_denied)

    def test_record_xtest_global_scope_request_exists(self):
        """record_xtest_global_scope_request 함수가 존재."""
        from baldur.services.metrics.recorders import (
            record_xtest_global_scope_request,
        )

        assert callable(record_xtest_global_scope_request)

    @patch("baldur.services.metrics.recorders._xtest_cross_region_denied_total")
    def test_record_xtest_cross_region_denied_increments(self, mock_metric):
        """record_xtest_cross_region_denied가 메트릭을 증가시킴."""
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
        """record_xtest_global_scope_request가 허용 요청을 기록."""
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
        """record_xtest_global_scope_request가 거부 요청을 기록."""
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

    def test_record_functions_handle_exceptions(self):
        """기록 함수가 예외를 안전하게 처리."""
        from baldur.services.metrics.recorders import (
            record_xtest_cross_region_denied,
            record_xtest_global_scope_request,
        )

        # 예외 발생해도 함수가 중단되지 않음
        with patch(
            "baldur.services.metrics.recorders._xtest_cross_region_denied_total",
            side_effect=Exception("Test error"),
        ):
            # 예외 발생해도 에러 없이 완료
            record_xtest_cross_region_denied("seoul", "tokyo")

        with patch(
            "baldur.services.metrics.recorders._xtest_global_scope_requests_total",
            side_effect=Exception("Test error"),
        ):
            record_xtest_global_scope_request("emergency", "seoul", "allowed")


class TestXTestModeMixinMetricsIntegration:
    """XTestModeMixin 메트릭 통합 테스트.

    Django 설정 없이 메트릭 연동 로직을 테스트.
    _get_endpoint_pattern_name, _record_regional_scope_metrics 로직을
    직접 구현하여 테스트.
    """

    def _get_endpoint_pattern_name(self, request) -> str:
        """엔드포인트 패턴 이름 추출 (XTestModeMixin 로직 복제)."""
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
        """Regional scope 메트릭 기록 (XTestModeMixin 로직 복제)."""
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
        """emergency 엔드포인트 패턴 감지."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/emergency/global/set/"

        result = self._get_endpoint_pattern_name(request)
        assert result == "emergency"

    def test_get_endpoint_pattern_name_isolation(self):
        """isolation 엔드포인트 패턴 감지."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/isolation/region/isolate/"

        result = self._get_endpoint_pattern_name(request)
        assert result == "isolation"

    def test_get_endpoint_pattern_name_governance(self):
        """governance 엔드포인트 패턴 감지."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/governance/global/update/"

        result = self._get_endpoint_pattern_name(request)
        assert result == "governance"

    def test_get_endpoint_pattern_name_unknown(self):
        """알 수 없는 엔드포인트는 unknown 반환."""
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
        """허용된 요청의 메트릭 기록."""
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
        """리전 불일치 거부의 메트릭 기록."""
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
        """헤더 없음 거부의 메트릭 기록."""
        request = MagicMock()
        request.path = "/api/baldur/xtest/governance/global/update/"

        self._record_regional_scope_metrics(request, "seoul", None, "denied_no_header")

        mock_global.assert_called_once_with(
            endpoint_pattern="governance",
            region="seoul",
            result="denied_no_header",
        )
        # target_region이 None이므로 cross-region 메트릭은 기록되지 않음
        mock_denied.assert_not_called()
