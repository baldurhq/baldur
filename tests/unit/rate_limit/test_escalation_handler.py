"""
RateLimitEscalationHandler 단위 테스트.

테스트 대상:
- 임계치 도달 시 에스컬레이션 발동
- 동일 key 중복 에스컬레이션 방지
- 에스컬레이션 리셋 및 재발동
- escalated_keys 속성
- 에스컬레이션 실패 처리 (Fail-Open)
- subscribe / reset_all 동작
- 독립 key 에스컬레이션
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.unit.rate_limit.conftest import (
    make_429_baldur_event as make_429_event,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_escalation_manager():
    """MagicMock EscalationManager (성공 반환)."""
    manager = MagicMock()
    result = MagicMock()
    result.success = True
    result.channels_sent = ["pagerduty"]
    manager.escalate.return_value = result
    return manager


@pytest.fixture
def escalation_handler(mock_escalation_manager):
    """기본 threshold를 사용하는 RateLimitEscalationHandler."""
    from baldur.meta.rate_limit_escalation import RateLimitEscalationHandler

    return RateLimitEscalationHandler(
        escalation_manager=mock_escalation_manager,
        threshold=5,
    )


# =============================================================================
# 기본 에스컬레이션 테스트
# =============================================================================


class TestRateLimitEscalationHandler:
    """RateLimitEscalationHandler 에스컬레이션 테스트."""

    def test_escalation_triggered_at_threshold(
        self, escalation_handler, mock_escalation_manager
    ):
        """임계치 도달 시 에스컬레이션 발동."""
        threshold = escalation_handler.threshold

        # 임계치 미만 — 에스컬레이션 없음
        escalation_handler._handle_rate_limit_429(
            make_429_event(key="payment_api", consecutive_429s=threshold - 2)
        )
        mock_escalation_manager.escalate.assert_not_called()

        # 임계치 도달 — 에스컬레이션 발동
        escalation_handler._handle_rate_limit_429(
            make_429_event(key="payment_api", consecutive_429s=threshold)
        )
        mock_escalation_manager.escalate.assert_called_once()

        # EscalationEvent 검증
        call_args = mock_escalation_manager.escalate.call_args
        event = call_args[0][0]
        assert "payment_api" in event.title
        assert event.details["consecutive_429s"] == threshold

    def test_no_duplicate_escalation_for_same_key(
        self, escalation_handler, mock_escalation_manager
    ):
        """동일 key에 대한 중복 에스컬레이션 방지."""
        high_count = escalation_handler.threshold * 2

        escalation_handler._handle_rate_limit_429(
            make_429_event(key="payment_api", consecutive_429s=high_count)
        )
        escalation_handler._handle_rate_limit_429(
            make_429_event(key="payment_api", consecutive_429s=high_count + 5)
        )

        assert mock_escalation_manager.escalate.call_count == 1

    def test_reset_escalation_allows_new_escalation(
        self, escalation_handler, mock_escalation_manager
    ):
        """에스컬레이션 리셋 후 새로운 에스컬레이션 허용."""
        high_count = escalation_handler.threshold * 2

        escalation_handler._handle_rate_limit_429(
            make_429_event(key="payment_api", consecutive_429s=high_count)
        )
        assert mock_escalation_manager.escalate.call_count == 1

        escalation_handler.reset_escalation("payment_api")

        escalation_handler._handle_rate_limit_429(
            make_429_event(key="payment_api", consecutive_429s=high_count)
        )
        assert mock_escalation_manager.escalate.call_count == 2

    def test_escalated_keys_property(self, escalation_handler):
        """escalated_keys 속성 확인."""
        api_keys = ["api_a", "api_b"]

        for key in api_keys:
            escalation_handler._handle_rate_limit_429(
                make_429_event(
                    key=key, consecutive_429s=escalation_handler.threshold * 2
                )
            )

        for key in api_keys:
            assert key in escalation_handler.escalated_keys

        assert isinstance(escalation_handler.escalated_keys, frozenset)


# =============================================================================
# 엣지케이스 테스트
# =============================================================================


class TestRateLimitEscalationHandlerEdgeCases:
    """RateLimitEscalationHandler 엣지케이스 테스트."""

    def test_escalation_failure_logs_error(self):
        """에스컬레이션 실패 시 에러 로그 (예외 없음)."""
        from baldur.meta.rate_limit_escalation import RateLimitEscalationHandler

        mock_manager = MagicMock()
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.error_message = "PagerDuty down"
        mock_manager.escalate.return_value = mock_result

        handler = RateLimitEscalationHandler(
            escalation_manager=mock_manager,
            threshold=5,
        )

        handler._handle_rate_limit_429(
            make_429_event(key="payment_api", consecutive_429s=10)
        )

        mock_manager.escalate.assert_called_once()
        assert "payment_api" in handler.escalated_keys

    def test_reset_all_escalations(self, mock_escalation_manager):
        """모든 에스컬레이션 상태 일괄 초기화."""
        from baldur.meta.rate_limit_escalation import RateLimitEscalationHandler

        handler = RateLimitEscalationHandler(
            escalation_manager=mock_escalation_manager,
            threshold=5,
        )

        api_keys = ["api_a", "api_b", "api_c"]
        for key in api_keys:
            handler._handle_rate_limit_429(make_429_event(key=key, consecutive_429s=10))

        assert len(handler.escalated_keys) == len(api_keys)

        handler.reset_all_escalations()
        assert len(handler.escalated_keys) == 0

    def test_threshold_property(self):
        """threshold 속성 확인."""
        from baldur.meta.rate_limit_escalation import RateLimitEscalationHandler

        custom_threshold = 15
        handler = RateLimitEscalationHandler(threshold=custom_threshold)
        assert handler.threshold == custom_threshold

    def test_subscribe_returns_true_with_mock_bus(self):
        """subscribe() EventBus 구독 성공 확인."""
        from baldur.meta.rate_limit_escalation import RateLimitEscalationHandler

        handler = RateLimitEscalationHandler(threshold=5)

        mock_bus = MagicMock()
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            result = handler.subscribe()

        assert result is True
        mock_bus.subscribe.assert_called_once()

    def test_subscribe_returns_false_when_no_eventbus(self):
        """EventBus 없을 때 subscribe() False 반환."""
        from baldur.meta.rate_limit_escalation import RateLimitEscalationHandler

        handler = RateLimitEscalationHandler(threshold=5)

        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=ImportError("no eventbus"),
        ):
            result = handler.subscribe()

        assert result is False

    def test_multiple_keys_can_escalate_independently(self, mock_escalation_manager):
        """서로 다른 key는 독립적으로 에스컬레이션."""
        from baldur.meta.rate_limit_escalation import RateLimitEscalationHandler

        handler = RateLimitEscalationHandler(
            escalation_manager=mock_escalation_manager,
            threshold=5,
        )

        api_keys = ["api_a", "api_b"]
        for key in api_keys:
            handler._handle_rate_limit_429(make_429_event(key=key, consecutive_429s=10))

        assert mock_escalation_manager.escalate.call_count == len(api_keys)
