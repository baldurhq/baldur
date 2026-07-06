"""
단위 테스트 — deadline_context 모듈.

테스트 항목:
- 헤더 파싱 (유효 형식, 무효 형식, 빈 문자열)
- ContextVar 기반 deadline 설정/조회/만료 확인
- Fast-Fail 판정 (남은 시간 vs 예상 처리시간)
- 네트워크 레이턴시 Buffer 차감 검증
- 네트워크 혼잡 감지 (도착 시점 만료)
- 하위 서비스 전파 헤더 생성
- DB statement_timeout 연동
- deadline_scope 컨텍스트 매니저
"""

from __future__ import annotations

import pytest

from baldur.scaling.deadline_context import (
    _RTT_MIN_SAMPLE_MS,
    _RTT_SAMPLE_RATE,
    DEADLINE_HEADER,
    DEADLINE_META_KEY,
    DEFAULT_MINIMUM_USEFUL_TIME_MS,
    DEFAULT_NETWORK_LATENCY_BUFFER_MS,
    _request_deadline,
    clear_deadline,
    deadline_scope,
    get_deadline_aware_statement_timeout,
    get_propagation_header_value,
    get_remaining_ms,
    is_expired,
    parse_deadline_header,
    set_deadline,
    should_fast_fail,
)


@pytest.fixture(autouse=True)
def _reset_deadline():
    """각 테스트 전후로 deadline ContextVar를 초기화한다."""
    _request_deadline.set(None)
    yield
    _request_deadline.set(None)


class TestParseDeadlineHeaderContract:
    """X-Deadline-Remaining 헤더 파싱 계약값 검증."""

    def test_valid_ms_suffix(self):
        """'2500ms' → 2500.0."""
        assert parse_deadline_header("2500ms") == 2500.0

    def test_valid_no_suffix(self):
        """'2500' → 2500.0."""
        assert parse_deadline_header("2500") == 2500.0

    def test_valid_float(self):
        """'1500.5ms' → 1500.5."""
        assert parse_deadline_header("1500.5ms") == 1500.5

    def test_valid_with_whitespace(self):
        """' 2500ms ' → 2500.0 (공백 허용)."""
        assert parse_deadline_header(" 2500ms ") == 2500.0

    def test_invalid_format(self):
        """'abc' → None."""
        assert parse_deadline_header("abc") is None

    def test_empty_string(self):
        """'' → None."""
        assert parse_deadline_header("") is None

    def test_negative_not_matched(self):
        """'-100ms' → None (음수 불허)."""
        assert parse_deadline_header("-100ms") is None

    def test_case_insensitive_suffix(self):
        """'2500MS' → 2500.0 (대소문자 무관)."""
        assert parse_deadline_header("2500MS") == 2500.0


class TestDeadlineHeaderConstantsContract:
    """헤더 상수 계약값 검증."""

    def test_header_name(self):
        assert DEADLINE_HEADER == "X-Deadline-Remaining"

    def test_meta_key(self):
        assert DEADLINE_META_KEY == "HTTP_X_DEADLINE_REMAINING"


class TestDeadlineContextRttConstants:
    """RTT sampling constant contract values (relocated from the Django middleware, 592).

    The two ``BALDUR_DEADLINE_RTT_*`` constants are consumed by the framework-
    free ``record_rtt_sample`` helper (``api/middleware/deadline.py``); they live
    here, cohesive with the other ``BALDUR_DEADLINE_*`` constants. The OSS-always
    test owns the default contract so it is verified even on a no-PRO checkout.
    """

    def test_rtt_min_sample_ms_default(self):
        assert _RTT_MIN_SAMPLE_MS == 5.0

    def test_rtt_sample_rate_default(self):
        assert _RTT_SAMPLE_RATE == 0.1


class TestDeadlineContextBehavior:
    """Deadline ContextVar 설정/조회 동작 검증."""

    def test_set_and_get_remaining(self):
        """set_deadline 후 get_remaining_ms로 남은 시간 조회 (Buffer 차감 반영)."""
        set_deadline(2000.0)
        remaining = get_remaining_ms()
        assert remaining is not None
        # 2000ms - 50ms(buffer) = 1950ms, float round-trip 오차 + 실행 시간 허용
        assert 1900.0 < remaining <= 1951.0

    def test_no_deadline_returns_none(self):
        """deadline 미설정 시 None 반환."""
        assert get_remaining_ms() is None

    def test_is_expired_with_zero_remaining(self):
        """0ms 설정 시 expired."""
        set_deadline(0.0)
        assert is_expired() is True

    def test_is_expired_with_plenty_remaining(self):
        """충분한 시간이 남아있으면 not expired."""
        set_deadline(5000.0)
        assert is_expired() is False

    def test_is_expired_without_deadline(self):
        """deadline 미설정 시 False (Fail-Open)."""
        assert is_expired() is False

    def test_clear_deadline(self):
        """clear_deadline 후 get_remaining_ms는 None."""
        set_deadline(3000.0)
        clear_deadline()
        assert get_remaining_ms() is None

    def test_network_buffer_deduction(self):
        """2000ms 설정 → ~1950ms 반환 (50ms Buffer 차감)."""
        set_deadline(2000.0)
        remaining = get_remaining_ms()
        assert remaining is not None
        expected_max = 2000.0 - DEFAULT_NETWORK_LATENCY_BUFFER_MS
        # float round-trip 오차(나누기→더하기→빼기→곱하기)로 ~1e-8ms 초과 가능
        assert remaining <= expected_max + 1.0


class TestNetworkCongestionDetectionBehavior:
    """네트워크 혼잡 감지 동작 검증."""

    def test_exhausted_on_arrival(self):
        """Buffer보다 적은 시간(30ms) 설정 시 adjusted ≤ 0, 즉시 만료."""
        set_deadline(30.0)
        assert is_expired() is True

    def test_buffer_equals_remaining(self):
        """Buffer와 동일한 50ms 설정 시 adjusted == 0, 만료."""
        set_deadline(DEFAULT_NETWORK_LATENCY_BUFFER_MS)
        assert is_expired() is True

    def test_just_above_buffer(self):
        """Buffer + 1ms → 만료되지 않음."""
        set_deadline(DEFAULT_NETWORK_LATENCY_BUFFER_MS + 100.0)
        assert is_expired() is False


class TestShouldFastFailBehavior:
    """Fast-Fail 판정 동작 검증."""

    def test_remaining_less_than_estimated(self):
        """남은 500ms, 예상 2000ms → Fast-Fail."""
        set_deadline(550.0)  # buffer 차감 후 ~500ms
        assert should_fast_fail(estimated_processing_ms=2000.0) is True

    def test_remaining_more_than_estimated(self):
        """남은 5000ms, 예상 2000ms → 처리 가능."""
        set_deadline(5050.0)  # buffer 차감 후 ~5000ms
        assert should_fast_fail(estimated_processing_ms=2000.0) is False

    def test_no_deadline_no_fast_fail(self):
        """deadline 미설정 시 Fast-Fail 하지 않음 (Fail-Open)."""
        assert should_fast_fail(estimated_processing_ms=2000.0) is False

    def test_below_minimum_useful_time(self):
        """남은 시간이 최소 유효 시간 미만이면 Fast-Fail."""
        # Buffer 차감 후 약 10ms 남도록 설정
        set_deadline(DEFAULT_NETWORK_LATENCY_BUFFER_MS + 10.0)
        assert (
            should_fast_fail(
                estimated_processing_ms=1.0,
                minimum_useful_ms=DEFAULT_MINIMUM_USEFUL_TIME_MS,
            )
            is True
        )


class TestDeadlinePropagationBehavior:
    """하위 서비스 전파 헤더 생성 동작 검증."""

    def test_propagation_header_value(self):
        """남은 시간 → 'NNNms' 형식."""
        set_deadline(3000.0)
        value = get_propagation_header_value()
        assert value is not None
        assert value.endswith("ms")
        # 파싱 가능 확인
        parsed = parse_deadline_header(value)
        assert parsed is not None
        assert parsed > 0

    def test_propagation_when_no_deadline(self):
        """deadline 미설정 시 None."""
        assert get_propagation_header_value() is None

    def test_propagation_when_expired(self):
        """만료 시 None."""
        set_deadline(0.0)
        assert get_propagation_header_value() is None


class TestDeadlineAwareStatementTimeoutBehavior:
    """DB statement_timeout 연동 동작 검증."""

    def test_deadline_shorter_than_db_default(self):
        """남은 1000ms < DB 기본 30000ms → timeout=1000."""
        set_deadline(1050.0)  # buffer 차감 후 ~1000ms
        timeout = get_deadline_aware_statement_timeout(default_db_timeout_ms=30_000)
        assert timeout is not None
        assert 950 <= timeout <= 1000

    def test_deadline_longer_than_db_default(self):
        """남은 35000ms > DB 기본 30000ms → None (SET 불필요)."""
        set_deadline(35050.0)  # buffer 차감 후 ~35000ms
        timeout = get_deadline_aware_statement_timeout(default_db_timeout_ms=30_000)
        assert timeout is None

    def test_no_deadline_returns_none(self):
        """deadline 미설정 → None."""
        timeout = get_deadline_aware_statement_timeout()
        assert timeout is None

    def test_minimum_timeout_is_one_ms(self):
        """남은 시간이 극도로 짧아도 최소 1ms."""
        set_deadline(DEFAULT_NETWORK_LATENCY_BUFFER_MS + 0.5)  # buffer 차감 후 ~0.5ms
        timeout = get_deadline_aware_statement_timeout()
        assert timeout is not None
        assert timeout >= 1


class TestDeadlineScopeBehavior:
    """deadline_scope 컨텍스트 매니저 동작 검증."""

    def test_scope_sets_and_restores(self):
        """deadline_scope 블록 내에서 deadline 활성, 블록 후 원래 값 복원."""
        assert get_remaining_ms() is None

        with deadline_scope(3000.0):
            remaining = get_remaining_ms()
            assert remaining is not None
            assert remaining > 0

        assert get_remaining_ms() is None

    def test_scope_restores_previous_deadline(self):
        """기존 deadline이 있을 때 scope 종료 후 복원."""
        set_deadline(5000.0)
        original = get_remaining_ms()
        assert original is not None

        with deadline_scope(1000.0):
            inner = get_remaining_ms()
            assert inner is not None
            assert inner < original  # 1000ms - buffer < 5000ms - buffer

        restored = get_remaining_ms()
        assert restored is not None
        # 시간 경과를 고려하여 원래 값보다 약간 적어야 함
        assert restored > 0

    def test_scope_restores_on_exception(self):
        """예외 발생 시에도 이전 값 복원."""
        assert get_remaining_ms() is None

        with pytest.raises(ValueError):
            with deadline_scope(3000.0):
                assert get_remaining_ms() is not None
                raise ValueError("test error")

        assert get_remaining_ms() is None
