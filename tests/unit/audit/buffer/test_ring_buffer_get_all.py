"""
RingBuffer get_all() 메서드 테스트.

get_all() 메서드는 버퍼의 모든 항목을 비파괴적으로 반환합니다.
"""

import pytest

from baldur.audit.ring_buffer import (
    BackpressureStrategy,
    RingBuffer,
)


class TestRingBufferGetAll:
    """RingBuffer get_all() 메서드 테스트."""

    def test_get_all_empty_buffer(self):
        """빈 버퍼에서 get_all()."""
        buffer = RingBuffer[int](capacity=10)

        result = buffer.get_all()

        assert result == []
        assert buffer.is_empty is True

    def test_get_all_with_items(self):
        """항목이 있는 버퍼에서 get_all()."""
        buffer = RingBuffer[int](capacity=10)

        for i in range(5):
            buffer.put(i)

        result = buffer.get_all()

        assert result == [0, 1, 2, 3, 4]
        # 비파괴적: 버퍼 내용 유지
        assert buffer.size == 5

    def test_get_all_is_non_destructive(self):
        """get_all()은 버퍼 내용을 유지."""
        buffer = RingBuffer[str](capacity=10)
        buffer.put("a")
        buffer.put("b")
        buffer.put("c")

        # 여러 번 호출해도 동일 결과
        result1 = buffer.get_all()
        result2 = buffer.get_all()

        assert result1 == ["a", "b", "c"]
        assert result2 == ["a", "b", "c"]
        assert buffer.size == 3

    def test_get_all_returns_copy(self):
        """get_all()은 복사본 반환 (원본 수정 불가)."""
        buffer = RingBuffer[int](capacity=10)
        buffer.put(1)
        buffer.put(2)

        result = buffer.get_all()
        result.append(3)  # 복사본 수정

        # 원본 버퍼는 영향 없음
        assert buffer.size == 2
        assert buffer.get_all() == [1, 2]

    def test_get_all_after_overflow(self):
        """오버플로우 후 get_all()."""
        buffer = RingBuffer[int](capacity=5, strategy=BackpressureStrategy.DROP_OLDEST)

        for i in range(10):
            buffer.put(i)

        result = buffer.get_all()

        # 최신 5개만 남음
        assert result == [5, 6, 7, 8, 9]
        assert buffer.size == 5

    def test_get_all_thread_safe(self):
        """스레드 안전성 확인."""
        import threading

        buffer = RingBuffer[int](capacity=1000)
        results = []
        errors = []

        # 동시에 put과 get_all 수행
        def producer():
            for i in range(100):
                buffer.put(i)

        def consumer():
            try:
                for _ in range(10):
                    result = buffer.get_all()
                    results.append(len(result))
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(3):
            threads.append(threading.Thread(target=producer))
            threads.append(threading.Thread(target=consumer))

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # 에러 없음
        assert len(errors) == 0


class TestRingBufferStats:
    """RingBuffer 통계 테스트."""

    def test_stats_initial(self):
        """초기 통계."""
        buffer = RingBuffer[int](capacity=100)

        stats = buffer.get_stats()

        assert stats.capacity == 100
        assert stats.size == 0
        assert stats.total_enqueued == 0
        assert stats.total_dropped == 0
        assert stats.drop_rate == 0.0

    def test_stats_after_puts(self):
        """put 후 통계."""
        buffer = RingBuffer[int](capacity=10)

        for i in range(15):
            buffer.put(i)

        stats = buffer.get_stats()

        assert stats.capacity == 10
        assert stats.size == 10
        assert stats.total_enqueued == 15
        assert stats.total_dropped == 5
        assert stats.drop_rate == pytest.approx(5 / 15, rel=0.01)

    def test_reset_stats(self):
        """통계 초기화."""
        buffer = RingBuffer[int](capacity=10)

        for i in range(15):
            buffer.put(i)

        buffer.reset_stats()
        stats = buffer.get_stats()

        # 버퍼 내용은 유지, 통계만 초기화
        assert stats.size == 10
        assert stats.total_enqueued == 0
        assert stats.total_dropped == 0
