"""
BaggageSyncMiddleware 단위 테스트.

대상: baldur.api.django.cell.middleware.BaggageSyncMiddleware
"""

from unittest.mock import MagicMock, patch

import pytest


class TestBaggageSyncMiddlewareBehavior:
    """BaggageSyncMiddleware 동작 검증."""

    def _make_middleware(self, get_response=None):
        from baldur.api.django.cell.middleware import BaggageSyncMiddleware

        if get_response is None:
            get_response = MagicMock(return_value=MagicMock(status_code=200))
        return BaggageSyncMiddleware(get_response)

    def test_calls_restore_then_sync_then_response(self):
        """restore → sync → get_response → detach 순서로 호출한다."""
        call_order = []

        mock_response = MagicMock(status_code=200)

        def mock_get_response(request):
            call_order.append("get_response")
            return mock_response

        middleware = self._make_middleware(mock_get_response)
        request = MagicMock()

        mock_token = object()

        with (
            patch(
                "baldur.observability.baggage.restore_contextvars_from_baggage",
                side_effect=lambda: call_order.append("restore"),
            ),
            patch(
                "baldur.observability.baggage.sync_contextvars_to_baggage",
                side_effect=lambda: (call_order.append("sync"), mock_token)[1],
            ),
            patch(
                "baldur.observability.baggage.detach_baggage_token",
                side_effect=lambda t: call_order.append("detach"),
            ),
        ):
            response = middleware(request)

        assert call_order == ["restore", "sync", "get_response", "detach"]
        assert response is mock_response

    def test_detaches_token_even_on_exception(self):
        """get_response에서 예외 발생 시에도 token이 detach 된다."""

        def raising_response(request):
            raise ValueError("view error")

        middleware = self._make_middleware(raising_response)
        request = MagicMock()
        mock_token = object()
        detach_called = False

        def track_detach(t):
            nonlocal detach_called
            detach_called = True
            assert t is mock_token

        with (
            patch(
                "baldur.observability.baggage.restore_contextvars_from_baggage",
            ),
            patch(
                "baldur.observability.baggage.sync_contextvars_to_baggage",
                return_value=mock_token,
            ),
            patch(
                "baldur.observability.baggage.detach_baggage_token",
                side_effect=track_detach,
            ),
        ):
            with pytest.raises(ValueError, match="view error"):
                middleware(request)

        assert detach_called is True

    def test_passes_token_to_detach(self):
        """sync가 반환한 token이 정확히 detach에 전달된다."""
        middleware = self._make_middleware()
        request = MagicMock()

        sentinel_token = object()
        captured_token = None

        def capture_detach(t):
            nonlocal captured_token
            captured_token = t

        with (
            patch(
                "baldur.observability.baggage.restore_contextvars_from_baggage",
            ),
            patch(
                "baldur.observability.baggage.sync_contextvars_to_baggage",
                return_value=sentinel_token,
            ),
            patch(
                "baldur.observability.baggage.detach_baggage_token",
                side_effect=capture_detach,
            ),
        ):
            middleware(request)

        assert captured_token is sentinel_token
