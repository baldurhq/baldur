"""
CellTaggingMiddleware 동작 검증 테스트.

토글 패턴, request.cell_id 설정, ContextVar 전파, 응답 헤더 확인.

참조 소스:
- api/django/cell/middleware.py (CellTaggingMiddleware)
- context/cell_context.py (_current_cell_id, get_current_cell_id)
- services/cell_topology/tagger.py (CellTagger)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.context.cell_context import get_current_cell_id

# _reset_cell_context fixture는 conftest.py에서 autouse로 제공 (§5.1: 2+ 파일 공유)


def _make_response():
    """dict처럼 헤더를 담는 Mock Response."""
    response = MagicMock()
    response.headers = {}
    response.__setitem__ = lambda self, key, val: self.headers.__setitem__(key, val)
    response.__getitem__ = lambda self, key: self.headers[key]
    return response


class TestCellTaggingMiddlewareToggleBehavior:
    """CellTaggingMiddleware 토글 동작 검증."""

    def test_disabled_when_topology_not_enabled(self):
        """BALDUR_CELL_TOPOLOGY_ENABLED=False 시 패스스루해야 한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        response = MagicMock()
        get_response = MagicMock(return_value=response)
        request = MagicMock()

        with patch("django.conf.settings") as mock_settings:
            mock_settings.BALDUR_CELL_TOPOLOGY_ENABLED = False
            mock_settings.BALDUR_CELL_TAGGING_ENABLED = True

            mw = CellTaggingMiddleware(get_response)
            result = mw(request)

        assert result == response
        get_response.assert_called_once_with(request)
        assert not hasattr(request, "cell_id") or request.cell_id == request.cell_id

    def test_disabled_when_tagging_not_enabled(self):
        """BALDUR_CELL_TAGGING_ENABLED=False 시 패스스루해야 한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        response = MagicMock()
        get_response = MagicMock(return_value=response)
        request = MagicMock()

        with patch("django.conf.settings") as mock_settings:
            mock_settings.BALDUR_CELL_TOPOLOGY_ENABLED = True
            mock_settings.BALDUR_CELL_TAGGING_ENABLED = False

            mw = CellTaggingMiddleware(get_response)
            result = mw(request)

        assert result == response
        get_response.assert_called_once_with(request)


class TestCellTaggingMiddlewareActiveBehavior:
    """CellTaggingMiddleware 활성 시 동작 검증."""

    def test_sets_request_cell_id(self):
        """활성 시 request.cell_id를 설정해야 한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        response = MagicMock()
        response_headers = {}
        response.__setitem__ = lambda self, k, v: response_headers.__setitem__(k, v)
        response.__getitem__ = lambda self, k: response_headers[k]

        get_response = MagicMock(return_value=response)
        request = MagicMock()
        request.META = {"REMOTE_ADDR": "127.0.0.1"}
        request.user = MagicMock()
        request.user.pk = 42
        request.session = MagicMock()
        request.session.session_key = None
        del request.tenant_id
        del request.trace_id

        with patch("django.conf.settings") as mock_settings:
            mock_settings.BALDUR_CELL_TOPOLOGY_ENABLED = True
            mock_settings.BALDUR_CELL_TAGGING_ENABLED = True

            mock_tagger = MagicMock()
            mock_tagger.resolve_cell_id_from_request.return_value = "cell-5"

            mw = CellTaggingMiddleware(get_response)
            mw._tagger = mock_tagger

            mw(request)

        # request.cell_id 설정 확인
        assert request.cell_id == "cell-5"

    def test_sets_response_header(self):
        """활성 시 응답에 X-Cell-Id 헤더를 추가해야 한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        response_headers = {}
        response = MagicMock()
        response.__setitem__ = lambda self, k, v: response_headers.__setitem__(k, v)

        get_response = MagicMock(return_value=response)
        request = MagicMock()
        request.META = {"REMOTE_ADDR": "127.0.0.1"}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.BALDUR_CELL_TOPOLOGY_ENABLED = True
            mock_settings.BALDUR_CELL_TAGGING_ENABLED = True

            mock_tagger = MagicMock()
            mock_tagger.resolve_cell_id_from_request.return_value = "cell-7"

            mw = CellTaggingMiddleware(get_response)
            mw._tagger = mock_tagger
            mw(request)

        assert response_headers["X-Cell-Id"] == "cell-7"

    def test_contextvar_restored_after_request(self):
        """요청 처리 후 ContextVar가 이전 상태로 복원되어야 한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        response = MagicMock()
        response.__setitem__ = MagicMock()

        captured_cell_id = []

        def mock_get_response(req):
            captured_cell_id.append(get_current_cell_id())
            return response

        request = MagicMock()
        request.META = {"REMOTE_ADDR": "127.0.0.1"}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.BALDUR_CELL_TOPOLOGY_ENABLED = True
            mock_settings.BALDUR_CELL_TAGGING_ENABLED = True

            mock_tagger = MagicMock()
            mock_tagger.resolve_cell_id_from_request.return_value = "cell-2"

            mw = CellTaggingMiddleware(mock_get_response)
            mw._tagger = mock_tagger
            mw(request)

        # 미들웨어 내부에서 cell_id가 설정됨
        assert captured_cell_id == ["cell-2"]
        # 미들웨어 이후 복원됨
        assert get_current_cell_id() is None

    def test_contextvar_restored_on_exception(self):
        """요청 처리 중 예외 발생 시에도 ContextVar가 복원되어야 한다."""
        from baldur.api.django.cell.middleware import CellTaggingMiddleware

        def failing_get_response(req):
            raise RuntimeError("view error")

        request = MagicMock()
        request.META = {"REMOTE_ADDR": "127.0.0.1"}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.BALDUR_CELL_TOPOLOGY_ENABLED = True
            mock_settings.BALDUR_CELL_TAGGING_ENABLED = True

            mock_tagger = MagicMock()
            mock_tagger.resolve_cell_id_from_request.return_value = "cell-4"

            mw = CellTaggingMiddleware(failing_get_response)
            mw._tagger = mock_tagger

            with pytest.raises(RuntimeError, match="view error"):
                mw(request)

        assert get_current_cell_id() is None
