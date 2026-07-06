"""
trace_id_middleware try/finally 동작 검증.

대상: baldur.audit.trace.trace_id_middleware
검증: 뷰에서 예외 발생 시에도 clear_trace_id()가 호출되는지 확인.
"""

from unittest.mock import MagicMock

import pytest

from baldur.audit.trace import (
    _trace_id_var,
    clear_trace_id,
    trace_id_middleware,
)


class TestTraceIdMiddlewareTryFinallyBehavior:
    """trace_id_middleware의 예외 안전성 검증."""

    def setup_method(self):
        clear_trace_id()

    def teardown_method(self):
        clear_trace_id()

    def _make_request(self):
        """META가 빈 딕셔너리인 최소한의 request mock 생성."""
        request = MagicMock()
        request.META = {}
        return request

    def test_clears_trace_id_on_normal_response(self):
        """정상 응답 후 ContextVar의 trace_id가 정리된다."""
        mock_response = MagicMock()

        def get_response(request):
            # 미들웨어 내부에서 trace_id가 존재해야 함
            assert _trace_id_var.get() is not None
            return mock_response

        middleware = trace_id_middleware(get_response)
        request = self._make_request()

        middleware(request)
        # clear_trace_id() 호출 후 ContextVar가 None이어야 함
        assert _trace_id_var.get() is None

    def test_clears_trace_id_on_view_exception(self):
        """뷰에서 예외 발생 시에도 trace_id가 정리된다 (try/finally 보장)."""

        def raising_view(request):
            assert _trace_id_var.get() is not None
            raise RuntimeError("View crashed")

        middleware = trace_id_middleware(raising_view)
        request = self._make_request()

        with pytest.raises(RuntimeError, match="View crashed"):
            middleware(request)

        # 예외 발생 후에도 trace_id가 정리되어야 함
        assert _trace_id_var.get() is None

    def test_adds_trace_id_to_response_header(self):
        """정상 응답에 X-Request-ID 헤더가 추가된다."""
        mock_response = MagicMock()

        def get_response(request):
            return mock_response

        middleware = trace_id_middleware(get_response)
        request = self._make_request()

        middleware(request)
        mock_response.__setitem__.assert_called_once()
        header_name = mock_response.__setitem__.call_args[0][0]
        assert header_name == "X-Request-ID"

    def test_sets_trace_id_on_request_attribute(self):
        """request.trace_id 어트리뷰트가 설정된다."""

        captured_trace_id = None

        def get_response(request):
            nonlocal captured_trace_id
            captured_trace_id = request.trace_id
            return MagicMock()

        middleware = trace_id_middleware(get_response)
        request = self._make_request()

        middleware(request)
        assert captured_trace_id is not None
        assert captured_trace_id.startswith("req-")
