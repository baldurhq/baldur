"""
RequestHandler 단위 테스트.

테스트 항목:
- 핸들러 초기화 및 등록
- Circuit Breaker 메서드 핸들링
- DLQ 메서드 핸들링
- Learning 메서드 핸들링
- 에러 케이스 처리
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.adapters.ipc.exceptions import (
    IPCInvalidParamsError,
    IPCMethodNotFoundError,
)
from baldur.adapters.ipc.request_handler import (
    RequestHandler,
    get_request_handler,
    reset_request_handler,
)


class TestRequestHandlerInit:
    """RequestHandler 초기화 테스트."""

    def test_init_creates_handler_registry(self):
        """핸들러 레지스트리가 생성됨."""
        handler = RequestHandler()

        assert handler._handlers is not None
        assert len(handler._handlers) > 0

    def test_registered_methods(self):
        """기본 메서드들이 등록됨."""
        handler = RequestHandler()
        methods = handler.get_registered_methods()

        # Circuit Breaker 메서드
        assert "circuit_breaker.should_allow" in methods
        assert "circuit_breaker.get_state" in methods
        assert "circuit_breaker.force_open" in methods
        assert "circuit_breaker.force_close" in methods

        # DLQ 메서드
        assert "dlq.store" in methods
        assert "dlq.is_enabled" in methods

        # Learning 메서드
        assert "learning.get_suggestions" in methods

        # Health 메서드
        assert "health.check" in methods

    def test_register_custom_handler(self):
        """커스텀 핸들러 등록."""
        handler = RequestHandler()

        def custom_handler(params):
            return {"custom": True}

        handler.register_handler("custom.test", custom_handler)
        methods = handler.get_registered_methods()

        assert "custom.test" in methods


class TestRequestHandlerCircuitBreaker:
    """Circuit Breaker 핸들러 테스트."""

    def test_should_allow_returns_dict(self):
        """should_allow가 딕셔너리 반환."""
        handler = RequestHandler()
        result = handler.handle(
            "circuit_breaker.should_allow", {"service_name": "test_service"}
        )

        assert isinstance(result, dict)
        assert "allowed" in result
        assert "state" in result

    def test_should_allow_missing_service_name_raises_error(self):
        """service_name 누락 시 에러."""
        handler = RequestHandler()

        with pytest.raises(IPCInvalidParamsError) as exc_info:
            handler.handle("circuit_breaker.should_allow", {})

        assert "service_name" in str(exc_info.value)

    def test_should_allow_batch_returns_results(self):
        """배치 요청이 결과 딕셔너리 반환."""
        handler = RequestHandler()
        result = handler.handle(
            "circuit_breaker.should_allow_batch",
            {"service_names": ["service1", "service2"]},
        )

        assert isinstance(result, dict)
        assert "results" in result

    def test_should_allow_batch_empty_returns_empty(self):
        """빈 배치는 빈 결과 반환."""
        handler = RequestHandler()
        result = handler.handle(
            "circuit_breaker.should_allow_batch", {"service_names": []}
        )

        assert result == {"results": {}}

    def test_get_state_returns_dict(self):
        """get_state가 딕셔너리 반환."""
        handler = RequestHandler()
        result = handler.handle(
            "circuit_breaker.get_state", {"service_name": "test_service"}
        )

        assert isinstance(result, dict)
        assert "service_name" in result
        assert "state" in result

    def test_get_all_states_returns_list(self):
        """get_all_states가 리스트 포함 딕셔너리 반환."""
        handler = RequestHandler()
        result = handler.handle("circuit_breaker.get_all_states", {})

        assert isinstance(result, dict)
        assert "states" in result

    def test_force_open_with_mock_service(self):
        """force_open이 서비스를 호출."""
        handler = RequestHandler()

        # Mock 서비스 주입
        mock_service = MagicMock()
        mock_service.force_open.return_value = MagicMock(
            success=True, message="Opened", new_state="open"
        )
        handler._cb_service = mock_service

        result = handler.handle(
            "circuit_breaker.force_open",
            {
                "service_name": "test_service",
                "reason": "test reason",
                "controlled_by": "test",
            },
        )

        assert result["success"] is True
        mock_service.force_open.assert_called_once()

    def test_force_close_with_mock_service(self):
        """force_close가 서비스를 호출."""
        handler = RequestHandler()

        # Mock 서비스 주입
        mock_service = MagicMock()
        mock_service.force_close.return_value = MagicMock(
            success=True, message="Closed", new_state="closed"
        )
        handler._cb_service = mock_service

        result = handler.handle(
            "circuit_breaker.force_close",
            {
                "service_name": "test_service",
                "reason": "test reason",
                "controlled_by": "test",
            },
        )

        assert result["success"] is True
        mock_service.force_close.assert_called_once()


class TestRequestHandlerDLQ:
    """DLQ 핸들러 테스트."""

    def test_is_enabled_returns_dict(self):
        """is_enabled가 딕셔너리 반환."""
        handler = RequestHandler()
        result = handler.handle("dlq.is_enabled", {})

        assert isinstance(result, dict)
        assert "enabled" in result

    def test_store_missing_domain_raises_error(self):
        """domain 누락 시 에러."""
        handler = RequestHandler()

        with pytest.raises(IPCInvalidParamsError) as exc_info:
            handler.handle(
                "dlq.store",
                {"failure_type": "timeout", "error_message": "test"},
            )

        assert "domain" in str(exc_info.value)

    def test_store_missing_failure_type_raises_error(self):
        """failure_type 누락 시 에러."""
        handler = RequestHandler()

        with pytest.raises(IPCInvalidParamsError) as exc_info:
            handler.handle(
                "dlq.store",
                {"domain": "order", "error_message": "test"},
            )

        assert "failure_type" in str(exc_info.value)

    def test_get_entry_missing_entry_id_raises_error(self):
        """entry_id 누락 시 에러."""
        handler = RequestHandler()

        with pytest.raises(IPCInvalidParamsError) as exc_info:
            handler.handle("dlq.get_entry", {})

        assert "entry_id" in str(exc_info.value)

    def test_list_returns_dict(self):
        """list가 딕셔너리 반환."""
        handler = RequestHandler()

        # Mock DLQ 서비스 주입
        mock_service = MagicMock()
        mock_service.list_entries.return_value = {
            "results": [],
            "total_count": 0,
            "page": 1,
            "page_size": 20,
        }
        handler._dlq_service = mock_service

        result = handler.handle("dlq.list", {})

        assert isinstance(result, dict)
        assert "entries" in result
        assert "total_count" in result


class TestRequestHandlerLearning:
    """Learning 핸들러 테스트."""

    def test_get_suggestions_returns_dict(self):
        """get_suggestions가 딕셔너리 반환."""
        handler = RequestHandler()

        # Mock Learning 서비스 주입
        mock_service = MagicMock()
        mock_service.get_suggestions.return_value = []
        handler._learning_service = mock_service

        result = handler.handle("learning.get_suggestions", {})

        assert isinstance(result, dict)
        assert "suggestions" in result

    def test_get_suggestions_with_service_name(self):
        """service_name 파라미터 처리."""
        handler = RequestHandler()

        # Mock Learning 서비스 주입
        mock_service = MagicMock()
        mock_service.get_suggestions.return_value = []
        handler._learning_service = mock_service

        result = handler.handle(
            "learning.get_suggestions", {"service_name": "test_stage"}
        )

        assert isinstance(result, dict)

    def test_record_success_missing_pattern_type_raises_error(self):
        """pattern_type 누락 시 에러."""
        handler = RequestHandler()

        with pytest.raises(IPCInvalidParamsError) as exc_info:
            handler.handle("learning.record_success", {})

        assert "pattern_type" in str(exc_info.value)

    def test_record_failure_missing_pattern_type_raises_error(self):
        """pattern_type 누락 시 에러."""
        handler = RequestHandler()

        with pytest.raises(IPCInvalidParamsError) as exc_info:
            handler.handle("learning.record_failure", {})

        assert "pattern_type" in str(exc_info.value)


class TestRequestHandlerHealth:
    """Health 핸들러 테스트."""

    def test_health_check_returns_status(self):
        """health.check가 상태 반환."""
        handler = RequestHandler()
        result = handler.handle("health.check", {})

        assert isinstance(result, dict)
        assert "status" in result
        assert "components" in result


class TestRequestHandlerErrors:
    """에러 케이스 테스트."""

    def test_unknown_method_raises_error(self):
        """알 수 없는 메서드 시 에러."""
        handler = RequestHandler()

        with pytest.raises(IPCMethodNotFoundError) as exc_info:
            handler.handle("unknown.method", {})

        assert "unknown.method" in str(exc_info.value)


class TestRequestHandlerSingleton:
    """싱글톤 패턴 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 리셋."""
        reset_request_handler()

    def teardown_method(self):
        """테스트 후 싱글톤 리셋."""
        reset_request_handler()

    def test_get_request_handler_returns_same_instance(self):
        """싱글톤 인스턴스가 동일."""
        handler1 = get_request_handler()
        handler2 = get_request_handler()

        assert handler1 is handler2

    def test_reset_creates_new_instance(self):
        """리셋 후 새 인스턴스."""
        handler1 = get_request_handler()
        reset_request_handler()
        handler2 = get_request_handler()

        assert handler1 is not handler2
