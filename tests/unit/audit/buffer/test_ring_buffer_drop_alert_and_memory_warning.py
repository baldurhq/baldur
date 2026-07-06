"""
RingBuffer 드랍률 알림 및 메모리 경고 테스트.

테스트 대상:
1. 드랍률 알림 연동 (on_drop_threshold 콜백)
2. 메모리 경고 (CAPACITY_WARNING_THRESHOLD)
3. reset_alert() 메서드
"""

import logging
from unittest.mock import MagicMock, patch

from baldur.audit.ring_buffer import (
    RingBuffer,
    RingBufferStats,
)


class TestRingBufferDropRateAlert:
    """드랍률 알림 연동 테스트."""

    def test_on_drop_threshold_callback_called(self):
        """드랍률 임계치 초과 시 콜백 호출."""
        callback = MagicMock()

        # 용량 10, 드랍률 임계치 10%
        buffer: RingBuffer[int] = RingBuffer(
            capacity=10,
            on_drop_threshold=callback,
            drop_rate_threshold=0.1,
        )

        # 150개 추가 → 140개 드랍 (드랍률 93%)
        for i in range(150):
            buffer.put(i)

        # 콜백 호출됨
        callback.assert_called_once()

        # 콜백에 전달된 stats 확인
        stats: RingBufferStats = callback.call_args[0][0]
        assert stats.capacity == 10
        assert stats.total_dropped > 0
        assert stats.drop_rate > 0.1

    def test_callback_called_only_once(self):
        """콜백은 한 번만 호출됨 (중복 방지)."""
        callback = MagicMock()

        buffer: RingBuffer[int] = RingBuffer(
            capacity=10,
            on_drop_threshold=callback,
            drop_rate_threshold=0.1,
        )

        # 300개 추가 (대량 드랍)
        for i in range(300):
            buffer.put(i)

        # 콜백은 한 번만 호출
        assert callback.call_count == 1

    def test_reset_alert_allows_callback_again(self):
        """reset_alert() 후 콜백 재호출 가능."""
        callback = MagicMock()

        buffer: RingBuffer[int] = RingBuffer(
            capacity=10,
            on_drop_threshold=callback,
            drop_rate_threshold=0.1,
        )

        # 첫 번째 대량 드랍
        for i in range(150):
            buffer.put(i)
        assert callback.call_count == 1

        # 알림 리셋
        buffer.reset_alert()

        # 두 번째 대량 드랍
        for i in range(150):
            buffer.put(i)
        assert callback.call_count == 2

    def test_no_callback_when_below_threshold(self):
        """드랍률 임계치 미만 시 콜백 미호출."""
        callback = MagicMock()

        # 큰 용량, 높은 임계치
        buffer: RingBuffer[int] = RingBuffer(
            capacity=1000,
            on_drop_threshold=callback,
            drop_rate_threshold=0.9,  # 90% 임계치
        )

        # 용량 내 추가 (드랍 없음)
        for i in range(500):
            buffer.put(i)

        # 콜백 미호출
        callback.assert_not_called()

    def test_callback_not_called_with_insufficient_samples(self):
        """최소 샘플 수 미만 시 콜백 미호출."""
        callback = MagicMock()

        buffer: RingBuffer[int] = RingBuffer(
            capacity=5,
            on_drop_threshold=callback,
            drop_rate_threshold=0.01,
        )

        # 50개만 추가 (MIN_SAMPLES_FOR_ALERT=100 미만)
        for i in range(50):
            buffer.put(i)

        # 콜백 미호출 (샘플 부족)
        callback.assert_not_called()

    def test_callback_exception_does_not_break_put(self):
        """콜백 예외가 put() 동작을 방해하지 않음."""
        callback = MagicMock(side_effect=Exception("Callback error"))

        buffer: RingBuffer[int] = RingBuffer(
            capacity=10,
            on_drop_threshold=callback,
            drop_rate_threshold=0.1,
        )

        # 예외 발생해도 put() 성공
        for i in range(150):
            result = buffer.put(i)
            assert result is True


class TestRingBufferHighCapacityWarning:
    """메모리 경고 테스트."""

    def test_capacity_warning_threshold_constant(self):
        """CAPACITY_WARNING_THRESHOLD 상수 존재."""
        assert hasattr(RingBuffer, "CAPACITY_WARNING_THRESHOLD")
        assert RingBuffer.CAPACITY_WARNING_THRESHOLD == 100000

    def test_estimated_event_size_constant(self):
        """ESTIMATED_EVENT_SIZE_BYTES 상수 존재."""
        assert hasattr(RingBuffer, "ESTIMATED_EVENT_SIZE_BYTES")
        assert RingBuffer.ESTIMATED_EVENT_SIZE_BYTES == 1024

    def test_high_capacity_logs_warning(self, caplog):
        """고용량 설정 시 경고 로그 출력."""
        # Use patch to capture logger.warning calls directly

        with patch("baldur.audit.ring_buffer.logger") as mock_logger:
            RingBuffer(capacity=200000)

            # Verify warning was called with expected message
            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            assert call_args[0][0] == "ring_buffer.high_use_mb_ram"
            assert call_args[1]["capacity"] == 200000

    def test_normal_capacity_no_warning(self, caplog):
        """정상 용량 시 경고 로그 없음."""
        with caplog.at_level(logging.WARNING):
            RingBuffer(capacity=10000)

        # RingBuffer 관련 경고 없음
        ringbuffer_warnings = [r for r in caplog.records if "ring_buffer" in r.message]
        assert len(ringbuffer_warnings) == 0


class TestRingBufferFromSettingsWithNewParams:
    """from_settings()에서 새 파라미터 지원 테스트."""

    def test_from_settings_accepts_drop_threshold_override(self):
        """from_settings()가 on_drop_threshold 오버라이드 지원."""
        callback = MagicMock()

        buffer = RingBuffer.from_settings(
            on_drop_threshold=callback,
            drop_rate_threshold=0.05,
        )

        assert buffer._on_drop_threshold == callback
        assert buffer._drop_rate_threshold == 0.05
