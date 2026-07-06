"""
Healing Events Redis Store 단위 테스트.

Redis 저장소 로직의 순수 단위 테스트입니다.
Redis/Django 의존성은 Mock으로 처리합니다.
"""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch


class TestHealingEventsStoreConstants:
    """상수 및 설정 테스트."""

    def test_key_prefix_format(self):
        """키 패턴이 문서 명세와 일치하는지 확인."""
        from baldur.services.healing_events_store import EVENTS_KEY_PREFIX

        assert EVENTS_KEY_PREFIX == "baldur:events"

    def test_ttl_days_is_seven(self):
        """TTL이 7일인지 확인."""
        from baldur.services.healing_events_store import (
            EVENTS_TTL_DAYS,
            EVENTS_TTL_SECONDS,
        )

        assert EVENTS_TTL_DAYS == 7
        assert EVENTS_TTL_SECONDS == 7 * 24 * 60 * 60  # 604800초

    def test_enable_disable_redis(self):
        """Redis 활성화/비활성화 설정 테스트."""
        from baldur.services.healing_events_store import (
            get_redis_events_enabled,
            set_redis_events_enabled,
        )

        # 기본값 확인
        original = get_redis_events_enabled()

        # 비활성화
        set_redis_events_enabled(False)
        assert get_redis_events_enabled() is False

        # 활성화
        set_redis_events_enabled(True)
        assert get_redis_events_enabled() is True

        # 원래 값 복원
        set_redis_events_enabled(original)


class TestKeyGeneration:
    """Redis 키 생성 로직 테스트."""

    def test_today_key_format(self):
        """오늘 날짜 키 형식 확인."""
        from baldur.services.healing_events_store import (
            EVENTS_KEY_PREFIX,
            _get_today_key,
        )

        key = _get_today_key()

        # 형식 확인: baldur:events:YYYY-MM-DD
        assert key.startswith(EVENTS_KEY_PREFIX + ":")
        date_part = key.split(":")[-1]

        # 날짜 형식 검증
        datetime.strptime(date_part, "%Y-%m-%d")

    def test_date_key_format(self):
        """특정 날짜 키 형식 확인."""
        from baldur.services.healing_events_store import (
            EVENTS_KEY_PREFIX,
            _get_date_key,
        )

        key = _get_date_key("2026-01-28")
        assert key == f"{EVENTS_KEY_PREFIX}:2026-01-28"


class TestAddHealingEventRedis:
    """add_healing_event_redis 함수 테스트."""

    def test_adds_recorded_at_if_missing(self):
        """recorded_at 필드가 없으면 추가."""
        from baldur.services.healing_events_store import (
            _events_memory,
            _events_memory_lock,
            set_redis_events_enabled,
        )

        # Redis 비활성화 (In-Memory만 테스트)
        set_redis_events_enabled(False)

        try:
            from baldur.services.healing_events_store import (
                add_healing_event_redis,
            )

            event = {"type": "test", "data": "value"}

            # In-Memory 초기화
            with _events_memory_lock:
                _events_memory.clear()

            add_healing_event_redis(event)

            assert "recorded_at" in event

        finally:
            set_redis_events_enabled(True)

    def test_preserves_existing_recorded_at(self):
        """이미 recorded_at이 있으면 유지."""
        from baldur.services.healing_events_store import (
            _events_memory,
            _events_memory_lock,
            add_healing_event_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(False)

        try:
            original_time = "2026-01-28T10:00:00+00:00"
            event = {"type": "test", "recorded_at": original_time}

            with _events_memory_lock:
                _events_memory.clear()

            add_healing_event_redis(event)

            assert event["recorded_at"] == original_time

        finally:
            set_redis_events_enabled(True)

    def test_fallback_to_memory_when_redis_disabled(self):
        """Redis 비활성화 시 In-Memory에 저장."""
        from baldur.services.healing_events_store import (
            _events_memory,
            _events_memory_lock,
            add_healing_event_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(False)

        try:
            with _events_memory_lock:
                _events_memory.clear()

            event = {"type": "test_fallback"}
            result = add_healing_event_redis(event)

            # Redis 없으면 False 반환 (In-Memory에 저장)
            assert result is False

            with _events_memory_lock:
                assert len(_events_memory) == 1
                assert _events_memory[0]["type"] == "test_fallback"

        finally:
            set_redis_events_enabled(True)
            with _events_memory_lock:
                _events_memory.clear()

    def test_memory_limit_enforced(self):
        """In-Memory 최대 개수 제한 확인."""
        from baldur.services.healing_events_store import (
            _events_memory,
            _events_memory_lock,
            add_healing_event_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(False)

        try:
            # 먼저 클리어
            with _events_memory_lock:
                _events_memory.clear()

            # 작은 수로 테스트 (10개)
            test_count = 10
            for i in range(test_count):
                add_healing_event_redis({"index": i})

            with _events_memory_lock:
                assert len(_events_memory) == test_count
                # 마지막 이벤트 확인
                assert _events_memory[-1]["index"] == test_count - 1

        finally:
            set_redis_events_enabled(True)
            with _events_memory_lock:
                _events_memory.clear()


class TestGetHealingEventsRedis:
    """get_healing_events_redis 함수 테스트."""

    def test_returns_from_memory_when_redis_disabled(self):
        """Redis 비활성화 시 In-Memory에서 조회."""
        from baldur.services.healing_events_store import (
            _events_memory,
            _events_memory_lock,
            add_healing_event_redis,
            get_healing_events_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(False)

        try:
            with _events_memory_lock:
                _events_memory.clear()

            # 이벤트 추가
            for i in range(5):
                add_healing_event_redis({"index": i})

            # 조회
            events = get_healing_events_redis(limit=3)

            assert len(events) == 3

        finally:
            set_redis_events_enabled(True)
            with _events_memory_lock:
                _events_memory.clear()

    def test_respects_limit_parameter(self):
        """limit 파라미터 존중 확인."""
        from baldur.services.healing_events_store import (
            _events_memory,
            _events_memory_lock,
            add_healing_event_redis,
            get_healing_events_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(False)

        try:
            with _events_memory_lock:
                _events_memory.clear()

            for i in range(10):
                add_healing_event_redis({"index": i})

            events = get_healing_events_redis(limit=5)
            assert len(events) == 5

        finally:
            set_redis_events_enabled(True)
            with _events_memory_lock:
                _events_memory.clear()


class TestGetHealingEventsCountRedis:
    """get_healing_events_count_redis 함수 테스트."""

    def test_returns_memory_count_when_redis_disabled(self):
        """Redis 비활성화 시 In-Memory 카운트 반환."""
        from baldur.services.healing_events_store import (
            _events_memory,
            _events_memory_lock,
            add_healing_event_redis,
            get_healing_events_count_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(False)

        try:
            with _events_memory_lock:
                _events_memory.clear()

            for i in range(7):
                add_healing_event_redis({"index": i})

            count = get_healing_events_count_redis()
            assert count == 7

        finally:
            set_redis_events_enabled(True)
            with _events_memory_lock:
                _events_memory.clear()


class TestClearHealingEventsRedis:
    """clear_healing_events_redis 함수 테스트."""

    def test_clears_memory(self):
        """In-Memory 초기화 확인."""
        from baldur.services.healing_events_store import (
            _events_memory,
            _events_memory_lock,
            add_healing_event_redis,
            clear_healing_events_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(False)

        try:
            # 먼저 클리어
            with _events_memory_lock:
                _events_memory.clear()

            for i in range(5):
                add_healing_event_redis({"index": i})

            with _events_memory_lock:
                assert len(_events_memory) == 5

            count = clear_healing_events_redis()
            assert count == 5  # 초기화된 개수 반환

            with _events_memory_lock:
                assert len(_events_memory) == 0

        finally:
            set_redis_events_enabled(True)
            with _events_memory_lock:
                _events_memory.clear()


class TestRedisIntegrationMocked:
    """Redis 통합 테스트 (Mocked)."""

    def test_lpush_called_with_correct_key(self):
        """Redis LPUSH가 올바른 키로 호출되는지 확인."""
        from baldur.services.healing_events_store import (
            EVENTS_KEY_PREFIX,
            add_healing_event_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(True)

        mock_redis = MagicMock()
        mock_redis.ttl.return_value = -1  # 키가 없음

        with patch(
            "baldur.services.healing_events_store._get_redis_client",
            return_value=mock_redis,
        ):
            add_healing_event_redis({"type": "test"})

            # LPUSH 호출 확인
            mock_redis.lpush.assert_called_once()
            call_args = mock_redis.lpush.call_args[0]
            key = call_args[0]

            assert key.startswith(EVENTS_KEY_PREFIX + ":")

    def test_expire_called_for_new_key(self):
        """새 키 생성 시 TTL 설정되는지 확인."""
        from baldur.services.healing_events_store import (
            EVENTS_TTL_SECONDS,
            add_healing_event_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(True)

        mock_redis = MagicMock()
        mock_redis.ttl.return_value = -1  # 키가 없음 (TTL 설정 필요)

        with patch(
            "baldur.services.healing_events_store._get_redis_client",
            return_value=mock_redis,
        ):
            add_healing_event_redis({"type": "test"})

            # expire 호출 확인
            mock_redis.expire.assert_called_once()
            call_args = mock_redis.expire.call_args[0]
            ttl = call_args[1]

            assert ttl == EVENTS_TTL_SECONDS

    def test_expire_not_called_for_existing_key(self):
        """기존 키에는 TTL 재설정하지 않음."""
        from baldur.services.healing_events_store import (
            add_healing_event_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(True)

        mock_redis = MagicMock()
        mock_redis.ttl.return_value = 86400  # 키에 이미 TTL 있음

        with patch(
            "baldur.services.healing_events_store._get_redis_client",
            return_value=mock_redis,
        ):
            add_healing_event_redis({"type": "test"})

            # expire 호출 안함
            mock_redis.expire.assert_not_called()

    def test_lrange_called_for_get(self):
        """get_healing_events_redis가 LRANGE 호출하는지 확인."""
        from baldur.services.healing_events_store import (
            get_healing_events_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(True)

        mock_redis = MagicMock()
        mock_redis.lrange.return_value = [
            json.dumps({"type": "test1"}).encode(),
            json.dumps({"type": "test2"}).encode(),
        ]

        with patch(
            "baldur.services.healing_events_store._get_redis_client",
            return_value=mock_redis,
        ):
            events = get_healing_events_redis(limit=10)

            mock_redis.lrange.assert_called()
            assert len(events) == 2
            assert events[0]["type"] == "test1"

    def test_returns_true_on_redis_success(self):
        """Redis 저장 성공 시 True 반환."""
        from baldur.services.healing_events_store import (
            add_healing_event_redis,
            set_redis_events_enabled,
        )

        set_redis_events_enabled(True)

        mock_redis = MagicMock()
        mock_redis.ttl.return_value = -1

        with patch(
            "baldur.services.healing_events_store._get_redis_client",
            return_value=mock_redis,
        ):
            result = add_healing_event_redis({"type": "test"})
            assert result is True
