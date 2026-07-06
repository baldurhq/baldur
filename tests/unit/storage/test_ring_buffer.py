"""
RingBuffer 단위 테스트.

Tests:
- 기본 put/get 동작
- DROP_OLDEST 배압 전략
- DROP_NEWEST 배압 전략
- 배치 조회
- 통계
- 스레드 안전성
"""

import threading
import time

import pytest

from baldur.audit.ring_buffer import (
    BackpressureStrategy,
    RingBuffer,
)


class TestRingBufferBasics:
    """RingBuffer 기본 동작 테스트."""

    def test_init_with_default_capacity(self):
        """기본 용량으로 초기화."""
        buffer = RingBuffer()
        assert buffer.capacity == 10000
        assert buffer.size == 0
        assert buffer.is_empty

    def test_init_with_custom_capacity(self):
        """사용자 정의 용량으로 초기화."""
        buffer = RingBuffer(capacity=100)
        assert buffer.capacity == 100

    def test_init_with_invalid_capacity(self):
        """잘못된 용량으로 초기화 시 예외."""
        with pytest.raises(ValueError):
            RingBuffer(capacity=0)
        with pytest.raises(ValueError):
            RingBuffer(capacity=-1)

    def test_put_and_get(self):
        """기본 put/get 동작."""
        buffer = RingBuffer[str](capacity=10)

        buffer.put("item1")
        buffer.put("item2")

        assert buffer.size == 2
        assert buffer.get() == "item1"
        assert buffer.get() == "item2"
        assert buffer.is_empty

    def test_put_returns_true(self):
        """put 성공 시 True 반환."""
        buffer = RingBuffer[str](capacity=10)
        assert buffer.put("item") is True

    def test_get_empty_returns_none(self):
        """빈 버퍼에서 get 시 None 반환."""
        buffer = RingBuffer()
        assert buffer.get() is None

    def test_peek_does_not_remove(self):
        """peek는 아이템을 제거하지 않음."""
        buffer = RingBuffer[str](capacity=10)
        buffer.put("item")

        assert buffer.peek() == "item"
        assert buffer.size == 1
        assert buffer.peek() == "item"

    def test_peek_empty_returns_none(self):
        """빈 버퍼에서 peek 시 None 반환."""
        buffer = RingBuffer()
        assert buffer.peek() is None

    def test_is_full(self):
        """is_full 속성 테스트."""
        buffer = RingBuffer[int](capacity=3)
        assert not buffer.is_full

        buffer.put(1)
        buffer.put(2)
        buffer.put(3)
        assert buffer.is_full


class TestBackpressureDropOldest:
    """DROP_OLDEST 배압 전략 테스트."""

    def test_drop_oldest_on_overflow(self):
        """용량 초과 시 가장 오래된 아이템 삭제."""
        buffer = RingBuffer[int](
            capacity=3,
            strategy=BackpressureStrategy.DROP_OLDEST,
        )

        buffer.put(1)
        buffer.put(2)
        buffer.put(3)
        # 4 추가 시 1이 삭제됨
        result = buffer.put(4)

        assert result is True
        assert buffer.get() == 2
        assert buffer.get() == 3
        assert buffer.get() == 4

    def test_drop_oldest_tracks_stats(self):
        """DROP_OLDEST 시 통계 기록."""
        buffer = RingBuffer[int](
            capacity=2,
            strategy=BackpressureStrategy.DROP_OLDEST,
        )

        buffer.put(1)
        buffer.put(2)
        buffer.put(3)  # 1 dropped
        buffer.put(4)  # 2 dropped

        stats = buffer.get_stats()
        assert stats.total_dropped == 2
        assert stats.total_enqueued == 4


class TestBackpressureDropNewest:
    """DROP_NEWEST 배압 전략 테스트."""

    def test_drop_newest_on_overflow(self):
        """용량 초과 시 새 아이템 거부."""
        buffer = RingBuffer[int](
            capacity=3,
            strategy=BackpressureStrategy.DROP_NEWEST,
        )

        buffer.put(1)
        buffer.put(2)
        buffer.put(3)
        # 4 추가 시 거부됨
        result = buffer.put(4)

        assert result is False
        assert buffer.get() == 1
        assert buffer.get() == 2
        assert buffer.get() == 3

    def test_drop_newest_tracks_stats(self):
        """DROP_NEWEST 시 통계 기록."""
        buffer = RingBuffer[int](
            capacity=2,
            strategy=BackpressureStrategy.DROP_NEWEST,
        )

        buffer.put(1)
        buffer.put(2)
        buffer.put(3)  # rejected
        buffer.put(4)  # rejected

        stats = buffer.get_stats()
        assert stats.total_dropped == 2
        assert stats.total_enqueued == 4


class TestBatchOperations:
    """배치 조회 테스트."""

    def test_get_batch(self):
        """get_batch로 여러 아이템 조회."""
        buffer = RingBuffer[int](capacity=10)
        for i in range(5):
            buffer.put(i)

        batch = buffer.get_batch(3)
        assert batch == [0, 1, 2]
        assert buffer.size == 2

    def test_get_batch_more_than_available(self):
        """사용 가능한 것보다 많이 요청 시."""
        buffer = RingBuffer[int](capacity=10)
        buffer.put(1)
        buffer.put(2)

        batch = buffer.get_batch(10)
        assert batch == [1, 2]
        assert buffer.is_empty

    def test_get_batch_empty(self):
        """빈 버퍼에서 get_batch."""
        buffer = RingBuffer()
        batch = buffer.get_batch(10)
        assert batch == []

    def test_peek_batch(self):
        """peek_batch로 여러 아이템 미리보기."""
        buffer = RingBuffer[int](capacity=10)
        for i in range(5):
            buffer.put(i)

        batch = buffer.peek_batch(3)
        assert batch == [0, 1, 2]
        assert buffer.size == 5  # 제거되지 않음

    def test_put_many(self):
        """put_many로 여러 아이템 추가."""
        buffer = RingBuffer[int](capacity=10)
        added = buffer.put_many([1, 2, 3, 4, 5])
        assert added == 5
        assert buffer.size == 5


class TestStats:
    """통계 테스트."""

    def test_stats_initial(self):
        """초기 통계."""
        buffer = RingBuffer(capacity=100)
        stats = buffer.get_stats()

        assert stats.capacity == 100
        assert stats.size == 0
        assert stats.total_enqueued == 0
        assert stats.total_dropped == 0
        assert stats.drop_rate == 0.0

    def test_drop_rate_calculation(self):
        """드롭률 계산."""
        buffer = RingBuffer[int](
            capacity=2,
            strategy=BackpressureStrategy.DROP_OLDEST,
        )

        buffer.put(1)
        buffer.put(2)
        buffer.put(3)
        buffer.put(4)

        stats = buffer.get_stats()
        assert stats.total_enqueued == 4
        assert stats.total_dropped == 2
        assert stats.drop_rate == 0.5

    def test_reset_stats(self):
        """통계 리셋."""
        buffer = RingBuffer[int](capacity=10)
        buffer.put(1)
        buffer.put(2)
        buffer.get()

        buffer.reset_stats()
        stats = buffer.get_stats()

        assert stats.total_enqueued == 0
        assert stats.total_dropped == 0
        assert stats.size == 1  # 버퍼 내용은 유지

    def test_repr(self):
        """__repr__ 테스트."""
        buffer = RingBuffer[int](capacity=100)
        buffer.put(1)
        buffer.put(2)

        repr_str = repr(buffer)
        assert "capacity=100" in repr_str
        assert "size=2" in repr_str


class TestClear:
    """clear 테스트."""

    def test_clear(self):
        """버퍼 비우기."""
        buffer = RingBuffer[int](capacity=10)
        buffer.put(1)
        buffer.put(2)
        buffer.put(3)

        cleared = buffer.clear()

        assert cleared == 3
        assert buffer.is_empty
        assert buffer.size == 0


class TestThreadSafety:
    """스레드 안전성 테스트."""

    def test_concurrent_put(self):
        """동시 put 테스트."""
        buffer = RingBuffer[int](capacity=1000)
        threads = []
        items_per_thread = 100

        def put_items(start):
            for i in range(items_per_thread):
                buffer.put(start + i)

        for t in range(10):
            thread = threading.Thread(target=put_items, args=(t * 1000,))
            threads.append(thread)

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        stats = buffer.get_stats()
        assert stats.total_enqueued == 1000

    def test_concurrent_put_and_get(self):
        """동시 put/get 테스트."""
        buffer = RingBuffer[int](capacity=100)
        produced = []
        consumed = []
        stop_event = threading.Event()

        def producer():
            for i in range(500):
                buffer.put(i)
                produced.append(i)
                time.sleep(0.001)
            stop_event.set()

        def consumer():
            while not stop_event.is_set() or not buffer.is_empty:
                item = buffer.get()
                if item is not None:
                    consumed.append(item)
                else:
                    time.sleep(0.001)

        producer_thread = threading.Thread(target=producer)
        consumer_thread = threading.Thread(target=consumer)

        producer_thread.start()
        consumer_thread.start()

        producer_thread.join()
        consumer_thread.join()

        # 모든 아이템이 소비됨
        assert len(consumed) == len(produced)
