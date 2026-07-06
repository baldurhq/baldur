"""
InMemoryCircuitBreakerStateRepository Sliding Window 단위 테스트 (#227).

테스트 대상: adapters/memory/circuit_breaker.py
- Ring Buffer 기반 Sliding Window 카운팅
- record_failure(), record_success()의 window 기반 count
- _get_or_create_window(), _clear_window() 내부 메서드
- reset_counts(), reset(), clear_manual_control() 시 window 초기화

코드 근거:
- circuit_breaker.py L62: __init__(sliding_window_size=100)
- circuit_breaker.py L67: _call_windows: dict[str, deque[bool]]
- circuit_breaker.py L240-268: record_failure() — window.append(False), window 기반 count
- circuit_breaker.py L270-298: record_success() — window.append(True), window 기반 count
- circuit_breaker.py L225-231: _get_or_create_window() — deque(maxlen=sliding_window_size)
- circuit_breaker.py L233-236: _clear_window() — window.clear()
- circuit_breaker.py L180-194: reset_counts() — _clear_window() 호출
- config.py L46: sliding_window_size: int = 100

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증: sliding_window_size 기본값, deque maxlen
- 동작 검증: 소스 참조 (config 기본값)
"""

from __future__ import annotations

from collections import deque

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig

# =============================================================================
# Sliding Window 계약 검증 (Contract)
# =============================================================================


class TestSlidingWindowContract:
    """Sliding Window Ring Buffer 계약 검증."""

    def test_default_sliding_window_size_matches_config(self):
        """InMemoryRepo의 기본 sliding_window_size는 CircuitBreakerConfig 기본값과 동일하다."""
        config = CircuitBreakerConfig()
        repo = InMemoryCircuitBreakerStateRepository()
        assert repo._sliding_window_size == config.sliding_window_size

    def test_default_sliding_window_size_is_100(self):
        """circuit_breaker.py L62: 기본 sliding_window_size는 100이다."""
        repo = InMemoryCircuitBreakerStateRepository()
        assert repo._sliding_window_size == 100

    def test_custom_sliding_window_size(self):
        """사용자 정의 sliding_window_size가 적용된다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=50)
        assert repo._sliding_window_size == 50

    def test_call_windows_initialized_empty(self):
        """circuit_breaker.py L67: _call_windows는 빈 dict로 초기화된다."""
        repo = InMemoryCircuitBreakerStateRepository()
        assert repo._call_windows == {}


# =============================================================================
# Sliding Window record_failure 동작 검증 (Behavior)
# =============================================================================


class TestSlidingWindowRecordFailureBehavior:
    """record_failure() Sliding Window 동작 검증 — circuit_breaker.py L240-268."""

    def test_record_failure_increments_failure_count(self):
        """record_failure() 호출 시 failure_count가 window 내에서 증가한다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        state = repo.record_failure("svc")
        assert state.failure_count == 1
        assert state.success_count == 0

    def test_record_failure_creates_window(self):
        """record_failure() 최초 호출 시 서비스별 window가 생성된다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        repo.record_failure("svc")
        assert "svc" in repo._call_windows
        assert len(repo._call_windows["svc"]) == 1

    def test_record_failure_appends_false_to_window(self):
        """record_failure()는 window에 False를 추가한다 (False=failure)."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        repo.record_failure("svc")
        assert list(repo._call_windows["svc"]) == [False]

    def test_multiple_failures_counted(self):
        """여러 번 실패 시 failure_count가 정확히 누적된다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        for _ in range(5):
            state = repo.record_failure("svc")
        assert state.failure_count == 5
        assert state.success_count == 0

    def test_window_evicts_oldest_on_overflow(self):
        """sliding_window_size 초과 시 가장 오래된 항목이 제거된다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=3)
        # 3개 실패 기록
        for _ in range(3):
            repo.record_failure("svc")
        assert repo._call_windows["svc"].maxlen == 3
        assert len(repo._call_windows["svc"]) == 3

        # 4번째 실패 — 가장 오래된 것 제거, 여전히 3개
        state = repo.record_failure("svc")
        assert len(repo._call_windows["svc"]) == 3
        assert state.failure_count == 3  # window 내 모두 실패

    def test_window_eviction_changes_count(self):
        """window overflow 시 evicted 항목의 타입에 따라 count가 변한다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=3)
        # [S, S, S] → success=3, failure=0
        for _ in range(3):
            repo.record_success("svc")

        # [S, S, F] → 가장 오래된 S가 밀려남 → success=2, failure=1
        state = repo.record_failure("svc")
        assert state.success_count == 2
        assert state.failure_count == 1

    def test_record_failure_sets_last_failure_at(self):
        """record_failure()는 last_failure_at을 갱신한다."""
        repo = InMemoryCircuitBreakerStateRepository()
        state = repo.record_failure("svc")
        assert state.last_failure_at is not None


# =============================================================================
# Sliding Window record_success 동작 검증 (Behavior)
# =============================================================================


class TestSlidingWindowRecordSuccessBehavior:
    """record_success() Sliding Window 동작 검증 — circuit_breaker.py L270-298."""

    def test_record_success_increments_success_count(self):
        """record_success() 호출 시 success_count가 window 내에서 증가한다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        state = repo.record_success("svc")
        assert state.success_count == 1
        assert state.failure_count == 0

    def test_record_success_appends_true_to_window(self):
        """record_success()는 window에 True를 추가한다 (True=success)."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        repo.record_success("svc")
        assert list(repo._call_windows["svc"]) == [True]

    def test_mixed_success_failure_counts(self):
        """실패와 성공이 섞인 경우 window 내 정확한 카운트를 반환한다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        repo.record_failure("svc")  # [F]
        repo.record_success("svc")  # [F, S]
        repo.record_failure("svc")  # [F, S, F]
        state = repo.record_success("svc")  # [F, S, F, S]
        assert state.failure_count == 2
        assert state.success_count == 2

    def test_record_success_preserves_last_failure_at(self):
        """record_success()는 last_failure_at을 변경하지 않는다."""
        repo = InMemoryCircuitBreakerStateRepository()
        failure_state = repo.record_failure("svc")
        original_last_failure = failure_state.last_failure_at

        success_state = repo.record_success("svc")
        assert success_state.last_failure_at == original_last_failure


# =============================================================================
# Window 초기화 동작 검증 (Behavior)
# =============================================================================


class TestSlidingWindowResetBehavior:
    """Window 초기화 동작 검증."""

    def test_reset_counts_clears_window(self):
        """reset_counts()는 window를 초기화한다 — circuit_breaker.py L186."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        repo.record_failure("svc")
        repo.record_failure("svc")
        repo.reset_counts("svc")
        assert len(repo._call_windows.get("svc", deque())) == 0

    def test_reset_clears_window(self):
        """reset()은 window를 초기화한다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        repo.record_failure("svc")
        repo.reset("svc")
        assert len(repo._call_windows.get("svc", deque())) == 0

    def test_clear_manual_control_clears_window(self):
        """clear_manual_control()은 window를 초기화한다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        repo.get_or_create("svc")
        repo.record_failure("svc")
        repo.set_manual_control("svc", "open", controlled_by_id=1)
        repo.clear_manual_control("svc")
        assert len(repo._call_windows.get("svc", deque())) == 0

    def test_clear_window_for_nonexistent_service(self):
        """존재하지 않는 서비스의 window 초기화는 에러 없이 무시된다."""
        repo = InMemoryCircuitBreakerStateRepository()
        repo._clear_window("nonexistent")  # 에러 없이 통과

    def test_reset_counts_zeroes_failure_and_success(self):
        """reset_counts() 후 failure_count와 success_count가 모두 0이다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        repo.record_failure("svc")
        repo.record_failure("svc")
        repo.record_success("svc")
        repo.reset_counts("svc")
        state = repo.get_or_create("svc")
        assert state.failure_count == 0
        assert state.success_count == 0


# =============================================================================
# 서비스 격리 동작 검증 (Behavior)
# =============================================================================


class TestSlidingWindowIsolationBehavior:
    """서비스별 Sliding Window 격리 검증."""

    def test_different_services_have_separate_windows(self):
        """서로 다른 서비스는 독립적인 window를 가진다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        repo.record_failure("svc_a")
        repo.record_failure("svc_a")
        repo.record_success("svc_b")

        state_a = repo.get_or_create("svc_a")
        state_b = repo.get_or_create("svc_b")

        assert state_a.failure_count == 2
        assert state_a.success_count == 0
        assert state_b.failure_count == 0
        assert state_b.success_count == 1

    def test_window_per_service_independence(self):
        """한 서비스의 window 변경이 다른 서비스에 영향을 주지 않는다."""
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=3)
        # svc_a: 3개 실패 → window 가득 참
        for _ in range(3):
            repo.record_failure("svc_a")
        # svc_b: 1개 성공
        repo.record_success("svc_b")

        # svc_a에 추가 기록해도 svc_b에 영향 없음
        repo.record_failure("svc_a")
        state_b = repo.get_or_create("svc_b")
        assert state_b.success_count == 1
        assert state_b.failure_count == 0
