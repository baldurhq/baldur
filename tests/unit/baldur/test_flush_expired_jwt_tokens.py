"""
만료된 JWT OutstandingToken 정리 태스크 단위 테스트.

flush_expired_jwt_tokens 함수의 동작 검증 및
Cleanup Beat Schedule 계약 검증.

Reference:
    docs/baldur/middleware_system/217_JWT_BLACKLIST_AND_SECRETS_VALIDATION.md §7.3
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.tasks.cleanup_tasks import (
    flush_expired_jwt_tokens,
    get_cleanup_beat_schedule,
)

# =============================================================================
# Behavior Tests — Thin Wrapper 동작 검증
# =============================================================================


class TestFlushExpiredJWTTokensBehavior:
    """flush_expired_jwt_tokens 동작 검증."""

    def test_calls_flushexpiredtokens_command(self):
        """token_blacklist 설치 시 flushexpiredtokens 관리 명령이 실행된다."""
        with patch("django.apps.apps.is_installed", return_value=True):
            with patch("django.core.management.call_command") as mock_call_command:
                result = flush_expired_jwt_tokens()

                mock_call_command.assert_called_once_with("flushexpiredtokens")
                assert result["success"] is True

    def test_skips_when_token_blacklist_not_installed(self):
        """token_blacklist 미설치 시 건너뛰고 skipped=True를 반환한다."""
        with patch("django.apps.apps.is_installed", return_value=False):
            result = flush_expired_jwt_tokens()

        assert result["success"] is True
        assert result["skipped"] is True

    def test_raises_on_exception(self):
        """예외 발생 시 re-raise 한다 (Thin Wrapper 패턴)."""
        with patch("django.apps.apps.is_installed", return_value=True):
            with patch(
                "django.core.management.call_command",
                side_effect=Exception("DB connection failed"),
            ):
                with pytest.raises(Exception, match="DB connection failed"):
                    flush_expired_jwt_tokens()

    def test_success_result_has_no_skipped_key(self):
        """정상 실행 결과에는 skipped 키가 없다."""
        with patch("django.apps.apps.is_installed", return_value=True):
            with patch("django.core.management.call_command"):
                result = flush_expired_jwt_tokens()

        assert "skipped" not in result
        assert result["success"] is True

    def test_success_result_has_message(self):
        """정상 실행 결과에 message가 포함된다."""
        with patch("django.apps.apps.is_installed", return_value=True):
            with patch("django.core.management.call_command"):
                result = flush_expired_jwt_tokens()

        assert "message" in result
        assert len(result["message"]) > 0


# =============================================================================
# Contract Tests — Beat Schedule 계약값 검증
# =============================================================================


class TestFlushExpiredJWTTokensBeatScheduleContract:
    """Cleanup Beat Schedule 내 flush-expired-jwt-tokens 계약값 검증."""

    @pytest.fixture
    def schedule(self):
        """청소부 레인 Beat Schedule."""
        return get_cleanup_beat_schedule()

    def test_schedule_entry_exists(self, schedule):
        """flush-expired-jwt-tokens 스케줄 엔트리가 존재한다."""
        assert "flush-expired-jwt-tokens" in schedule

    def test_task_name_contract(self, schedule):
        """태스크 이름이 baldur.flush_expired_jwt_tokens이다."""
        entry = schedule["flush-expired-jwt-tokens"]
        assert entry["task"] == "baldur.flush_expired_jwt_tokens"

    def test_schedule_is_daily_crontab(self, schedule):
        """스케줄이 crontab이다."""
        from celery.schedules import crontab

        entry = schedule["flush-expired-jwt-tokens"]
        assert isinstance(entry["schedule"], crontab)

    def test_schedule_hour_contract(self, schedule):
        """실행 시각이 02시대이다 (새벽 정리 시간대)."""
        entry = schedule["flush-expired-jwt-tokens"]
        assert entry["schedule"].hour == {2}

    def test_schedule_minute_contract(self, schedule):
        """실행 분이 30분이다."""
        entry = schedule["flush-expired-jwt-tokens"]
        assert entry["schedule"].minute == {30}

    def test_queue_is_maintenance(self, schedule):
        """maintenance 큐를 사용한다."""
        entry = schedule["flush-expired-jwt-tokens"]
        assert entry["options"]["queue"] == "maintenance"
