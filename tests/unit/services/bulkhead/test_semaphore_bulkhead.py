"""
SemaphoreBulkhead 단위 테스트.

동기 세마포어 기반 격벽의 동작을 검증합니다:
- 기본 획득/반환 동작
- 최대 동시 실행 제한
- 타임아웃 대기
- 거부 통계 추적
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from baldur.services.bulkhead.base import BulkheadState, BulkheadType
from baldur.services.bulkhead.exceptions import BulkheadFullError
from baldur.services.bulkhead.semaphore import SemaphoreBulkhead


class TestSemaphoreBulkheadBasic:
    """기본 동작 테스트."""

    def test_create_bulkhead_with_default_values(self):
        """기본값으로 격벽 생성."""
        bulkhead = SemaphoreBulkhead("test")

        assert bulkhead.name == "test"
        state = bulkhead.get_state()
        assert state.max_concurrent == 10
        assert state.active_count == 0
        assert state.bulkhead_type == BulkheadType.SEMAPHORE

    def test_create_bulkhead_with_custom_concurrent(self):
        """커스텀 동시 실행 수로 격벽 생성."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=5)

        state = bulkhead.get_state()
        assert state.max_concurrent == 5

    def test_acquire_and_release(self):
        """획득 후 반환."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=2)

        with bulkhead.acquire():
            state = bulkhead.get_state()
            assert state.active_count == 1

        state = bulkhead.get_state()
        assert state.active_count == 0

    def test_nested_acquire(self):
        """중첩 획득."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=3)

        with bulkhead.acquire():
            with bulkhead.acquire():
                state = bulkhead.get_state()
                assert state.active_count == 2

            state = bulkhead.get_state()
            assert state.active_count == 1

        state = bulkhead.get_state()
        assert state.active_count == 0


class TestSemaphoreBulkheadConcurrency:
    """동시성 제한 테스트."""

    def test_reject_when_full_no_timeout(self):
        """격벽이 가득 찬 경우 즉시 거부 (timeout=None)."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)

        with bulkhead.acquire():
            # 이미 1개 사용 중, timeout=None이면 즉시 실패
            with pytest.raises(BulkheadFullError) as exc_info:
                with bulkhead.acquire():
                    pass

            assert exc_info.value.bulkhead_name == "test"
            assert exc_info.value.max_concurrent == 1

    def test_reject_increments_counter(self):
        """거부 시 카운터 증가."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)

        with bulkhead.acquire():
            for _ in range(3):
                with pytest.raises(BulkheadFullError):
                    with bulkhead.acquire():
                        pass

        state = bulkhead.get_state()
        assert state.rejected_count == 3
        assert state.last_rejection_time is not None

    def test_concurrent_workers_limited(self):
        """동시 워커 수 제한 검증."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=3)
        max_concurrent_observed = 0
        lock = threading.Lock()

        def worker():
            nonlocal max_concurrent_observed
            try:
                with bulkhead.acquire(timeout=1.0):
                    with lock:
                        current = bulkhead.get_state().active_count
                        if current > max_concurrent_observed:
                            max_concurrent_observed = current
                    time.sleep(0.1)
            except BulkheadFullError:
                pass

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker) for _ in range(10)]
            for f in futures:
                f.result()

        # 최대 동시 실행 수는 3을 넘지 않아야 함
        assert max_concurrent_observed <= 3


class TestSemaphoreBulkheadTimeout:
    """타임아웃 테스트."""

    def test_acquire_with_timeout_success(self):
        """타임아웃 내에 획득 성공."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)

        def release_after_delay():
            time.sleep(0.1)
            # 첫 번째 acquire가 release되면 두 번째가 획득 가능

        # 첫 번째 획득
        acquired = bulkhead.try_acquire()
        assert acquired

        # 백그라운드에서 release
        def release_worker():
            time.sleep(0.1)
            bulkhead.release()

        t = threading.Thread(target=release_worker)
        t.start()

        # 타임아웃 내에 획득 시도
        with bulkhead.acquire(timeout=1.0):
            pass

        t.join()

    def test_acquire_with_timeout_failure(self):
        """타임아웃 초과 시 실패."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)

        with bulkhead.acquire():
            start = time.time()
            with pytest.raises(BulkheadFullError):
                with bulkhead.acquire(timeout=0.1):
                    pass
            elapsed = time.time() - start
            # 타임아웃만큼 대기했어야 함
            assert elapsed >= 0.1


class TestSemaphoreBulkheadState:
    """상태 조회 테스트."""

    def test_get_state_returns_correct_values(self):
        """상태 조회가 올바른 값 반환."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=5)

        state = bulkhead.get_state()
        assert isinstance(state, BulkheadState)
        assert state.name == "test"
        assert state.bulkhead_type == BulkheadType.SEMAPHORE
        assert state.max_concurrent == 5
        assert state.active_count == 0
        assert state.waiting_count == 0
        assert state.rejected_count == 0

    def test_available_permits_calculation(self):
        """사용 가능 허가 수 계산."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=3)

        state = bulkhead.get_state()
        assert state.available_permits == 3

        with bulkhead.acquire():
            state = bulkhead.get_state()
            assert state.available_permits == 2

    def test_utilization_percent_calculation(self):
        """사용률 계산."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=4)

        state = bulkhead.get_state()
        assert state.utilization_percent == 0.0

        with bulkhead.acquire():
            with bulkhead.acquire():
                state = bulkhead.get_state()
                assert state.utilization_percent == 50.0  # 2/4 = 50%


class TestSemaphoreBulkheadTryAcquire:
    """try_acquire 메서드 테스트."""

    def test_try_acquire_success(self):
        """try_acquire 성공."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=2)

        assert bulkhead.try_acquire() is True
        state = bulkhead.get_state()
        assert state.active_count == 1

        # 정리
        bulkhead.release()

    def test_try_acquire_failure_when_full(self):
        """격벽이 가득 찬 경우 try_acquire 실패."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)

        assert bulkhead.try_acquire() is True

        # 두 번째 시도는 실패
        assert bulkhead.try_acquire() is False

        # 정리
        bulkhead.release()


class TestSemaphoreBulkheadRejectionMetric:
    """644 D3: each reject path increments baldur_bulkhead_rejected_total via
    increment_rejected_count, emitted *outside* self._lock.

    self._lock is a non-reentrant threading.Lock, so recording the rejection
    under it while the prometheus client takes its own lock would nest two locks.
    Asserting lock.locked() is False at emit time proves the call site is outside
    the critical section; the called_once assertion proves the +1 wiring fires
    (the never-populated series 644 D3 wired).
    """

    def test_acquire_rejection_emits_counter_outside_lock(self):
        """acquire() rejection emits the counter once, outside self._lock."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)
        lock_held_at_emit: list[bool] = []

        with patch(
            "baldur.services.bulkhead.semaphore.increment_rejected_count",
            autospec=True,
            side_effect=lambda name: lock_held_at_emit.append(bulkhead._lock.locked()),
        ) as mock_inc:
            with bulkhead.acquire():
                with pytest.raises(BulkheadFullError):
                    with bulkhead.acquire():
                        pass

        mock_inc.assert_called_once_with("test")
        assert lock_held_at_emit == [False]

    def test_try_acquire_rejection_emits_counter_outside_lock(self):
        """try_acquire() rejection emits the counter once, outside self._lock."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)
        lock_held_at_emit: list[bool] = []

        with patch(
            "baldur.services.bulkhead.semaphore.increment_rejected_count",
            autospec=True,
            side_effect=lambda name: lock_held_at_emit.append(bulkhead._lock.locked()),
        ) as mock_inc:
            assert bulkhead.try_acquire() is True
            assert bulkhead.try_acquire() is False

        mock_inc.assert_called_once_with("test")
        assert lock_held_at_emit == [False]


class TestSemaphoreBulkheadWrap:
    """wrap 데코레이터 테스트."""

    def test_wrap_decorator(self):
        """wrap 데코레이터 동작."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=2)
        call_count = 0

        @bulkhead.wrap
        def my_function():
            nonlocal call_count
            call_count += 1
            return "result"

        result = my_function()
        assert result == "result"
        assert call_count == 1

    def test_wrap_respects_concurrency_limit(self):
        """wrap 데코레이터가 동시 실행 제한 준수."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)

        @bulkhead.wrap
        def slow_function():
            time.sleep(0.2)

        with bulkhead.acquire():
            # 이미 1개 사용 중이므로 wrap된 함수도 실패해야 함
            with pytest.raises(BulkheadFullError):
                slow_function()
