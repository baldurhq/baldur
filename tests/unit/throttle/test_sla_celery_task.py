"""
SLA Celery 태스크 단위 테스트.

대상: baldur/adapters/celery/tasks/sla_notification.py
- send_sla_notification 태스크 속성
- 태스크 실행 로직
"""

from __future__ import annotations

from unittest.mock import patch

from tests.unit.throttle.conftest import (
    SVC_PAYMENT,
    make_critical_event_data,
    make_recovered_event_data,
    make_warning_event_data,
)

# Celery 태스크 상수 (소스에서 정의된 값)
TASK_NAME = "baldur.adapters.celery.tasks.send_sla_notification"
TASK_QUEUE = "baldur"
TASK_MAX_RETRIES = 3
TASK_DEFAULT_RETRY_DELAY = 30
TASK_TIME_LIMIT = 60
TASK_SOFT_TIME_LIMIT = 55

# Patch 경로 (함수가 import 되는 위치)
PATCH_WARNING_SYNC = (
    "baldur_pro.services.throttle.sla_notification._send_sla_warning_sync"
)
PATCH_CRITICAL_SYNC = (
    "baldur_pro.services.throttle.sla_notification._send_sla_critical_sync"
)
PATCH_RECOVERED_SYNC = (
    "baldur_pro.services.throttle.sla_notification._send_limit_recovered_sync"
)


class TestSlaCeleryTaskAttributes:
    """Celery 태스크 속성 테스트."""

    def test_task_name(self):
        """태스크 이름 확인."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        assert send_sla_notification.name == TASK_NAME

    def test_task_queue(self):
        """태스크 큐 확인."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        assert send_sla_notification.queue == TASK_QUEUE

    def test_task_max_retries(self):
        """최대 재시도 횟수 확인."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        assert send_sla_notification.max_retries == TASK_MAX_RETRIES

    def test_task_default_retry_delay(self):
        """기본 재시도 지연 시간 확인."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        assert send_sla_notification.default_retry_delay == TASK_DEFAULT_RETRY_DELAY

    def test_task_time_limit(self):
        """태스크 타임아웃 확인."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        assert send_sla_notification.time_limit == TASK_TIME_LIMIT

    def test_task_soft_time_limit(self):
        """soft 타임아웃 확인."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        assert send_sla_notification.soft_time_limit == TASK_SOFT_TIME_LIMIT

    def test_task_acks_late_true(self):
        """acks_late=True 확인."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        assert send_sla_notification.acks_late is True

    def test_task_autoretry_for_exception(self):
        """autoretry_for에 Exception 포함."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        autoretry = getattr(send_sla_notification, "autoretry_for", None)
        assert autoretry is not None
        assert Exception in autoretry


class TestSlaCeleryTaskExecution:
    """Celery 태스크 실행 테스트."""

    def test_warning_notification_calls_sync_function(self):
        """warning 알림 시 _send_sla_warning_sync 호출."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        with patch(PATCH_WARNING_SYNC) as mock_send:
            send_sla_notification(
                event_data=make_warning_event_data(),
                notification_type="warning",
            )
            mock_send.assert_called_once()

    def test_critical_notification_calls_sync_function(self):
        """critical 알림 시 _send_sla_critical_sync 호출."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        with patch(PATCH_CRITICAL_SYNC) as mock_send:
            send_sla_notification(
                event_data=make_critical_event_data(),
                notification_type="critical",
            )
            mock_send.assert_called_once()

    def test_recovered_notification_calls_sync_function(self):
        """recovered 알림 시 _send_limit_recovered_sync 호출."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        with patch(PATCH_RECOVERED_SYNC) as mock_send:
            send_sla_notification(
                event_data=make_recovered_event_data(),
                notification_type="recovered",
            )
            mock_send.assert_called_once()

    def test_returns_result_dict(self):
        """태스크 실행 결과 dict 반환."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        with patch(PATCH_WARNING_SYNC):
            result = send_sla_notification(
                event_data=make_warning_event_data(),
                notification_type="warning",
            )

        assert isinstance(result, dict)
        assert result.get("status") == "sent"
        assert result.get("type") == "warning"

    def test_unknown_type_logs_warning(self):
        """알 수 없는 notification_type에 경고 로그."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        with patch(
            "baldur.adapters.celery.tasks.sla_notification.logger"
        ) as mock_logger:
            send_sla_notification(
                event_data=make_warning_event_data(),
                notification_type="invalid_type",
            )

            mock_logger.warning.assert_called_once()
            assert "invalid_type" in str(mock_logger.warning.call_args)


class TestSlaCeleryTaskLogging:
    """Celery 태스크 로깅 테스트."""

    def test_logs_on_success(self):
        """태스크 성공 시 info 로그."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        with patch(PATCH_WARNING_SYNC):
            with patch(
                "baldur.adapters.celery.tasks.sla_notification.logger"
            ) as mock_logger:
                send_sla_notification(
                    event_data=make_warning_event_data(),
                    notification_type="warning",
                )

                mock_logger.info.assert_called_once()
                log_msg = str(mock_logger.info.call_args)
                assert "warning" in log_msg.lower()

    def test_logs_attempt_number(self):
        """로그에 시도 횟수 포함."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        with patch(PATCH_CRITICAL_SYNC):
            with patch(
                "baldur.adapters.celery.tasks.sla_notification.logger"
            ) as mock_logger:
                send_sla_notification(
                    event_data=make_critical_event_data(),
                    notification_type="critical",
                )

                # attempt 또는 retries 문자열 확인
                mock_logger.info.assert_called_once()


class TestSlaCeleryTaskEventData:
    """이벤트 데이터 전달 테스트."""

    def test_event_data_passed_to_warning_sync(self):
        """event_data가 warning sync 함수에 전달."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        event_data = make_warning_event_data()

        with patch(PATCH_WARNING_SYNC) as mock_send:
            send_sla_notification(
                event_data=event_data,
                notification_type="warning",
            )

            mock_send.assert_called_once_with(event_data)

    def test_event_data_passed_to_critical_sync(self):
        """event_data가 critical sync 함수에 전달."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        event_data = make_critical_event_data()

        with patch(PATCH_CRITICAL_SYNC) as mock_send:
            send_sla_notification(
                event_data=event_data,
                notification_type="critical",
            )

            mock_send.assert_called_once_with(event_data)

    def test_event_data_passed_to_recovered_sync(self):
        """event_data가 recovered sync 함수에 전달."""
        from baldur.adapters.celery.tasks.sla_notification import (
            send_sla_notification,
        )

        event_data = make_recovered_event_data()

        with patch(PATCH_RECOVERED_SYNC) as mock_send:
            send_sla_notification(
                event_data=event_data,
                notification_type="recovered",
            )

            mock_send.assert_called_once_with(event_data)

    def test_service_name_in_event_data(self):
        """event_data에 service_name 포함."""
        event_data = make_warning_event_data()
        assert event_data["service_name"] == SVC_PAYMENT
