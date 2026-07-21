"""
Cascading Timeout 방지 통합 테스트.

243번 문서에서 정의한 통합 테스트 시나리오:
1. A → B → C 호출 체인 Fast-Fail 시뮬레이션
2. Deadline 전파 정확도 검증
3. RTT 피드백 루프 (응답 시간 → GradientCalculator → Fast-Fail 임계치 자동 조정)
4. Cold Start Fast-Fail (서비스 재시작 직후 Tier별 기본값 작동)
5. Data Pollution Prevention (거절 요청 RTT가 smoothed_rtt에 영향 없음)
6. Tier별 RTT 격리 (critical RTT ↔ non_essential RTT 독립)

Django 의존 없음 — 순수 baldur 패키지 내부 컴포넌트 간 상호작용 검증.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import time
from unittest.mock import MagicMock

import pytest

from baldur.scaling.config import BackpressureLevel
from baldur.scaling.deadline_context import (
    DEFAULT_ESTIMATED_MS_STANDARD,
    DEFAULT_NETWORK_LATENCY_BUFFER_MS,
    _request_deadline,
    clear_deadline,
    deadline_scope,
    get_estimated_processing_ms,
    get_propagation_header_value,
    get_remaining_ms,
    get_tier_default_estimated_ms,
    parse_deadline_header,
    set_deadline,
)
from baldur.scaling.traffic_gate import TrafficGate, reset_traffic_gate
from baldur_pro.services.throttle.gradient import (
    GradientCalculator,
    get_gradient_calculator,
    reset_gradient_calculators,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_all_state():
    """각 테스트 전후로 모든 상태를 초기화한다."""
    reset_traffic_gate()
    reset_gradient_calculators()
    _request_deadline.set(None)
    yield
    reset_traffic_gate()
    reset_gradient_calculators()
    _request_deadline.set(None)


@pytest.fixture
def mock_rate_controller():
    """RateController Mock — should_process()는 항상 True."""
    controller = MagicMock()
    state = MagicMock()
    state.level = BackpressureLevel.NONE
    controller.get_state.return_value = state
    controller.should_process.return_value = True
    return controller


# =============================================================================
# 1. Full Chain Fast-Fail: A → B → C 시뮬레이션
# =============================================================================


class TestFullChainFastFail:
    """
    MSA 호출 체인 A → B → C 시뮬레이션.

    시나리오:
    - A가 3초 timeout으로 B를 호출
    - B가 2.5초 소요 후 C를 호출
    - C는 남은 시간 < 예상 처리시간 → Fast-Fail
    - 전체 체인이 조기 종료됨
    """

    def test_full_chain_service_c_fast_fails(self, mock_rate_controller):
        """서비스 C가 남은 시간 부족으로 Fast-Fail → 전체 체인 조기 종료."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        # --- 서비스 A: 3000ms deadline 설정 (클라이언트 → A) ---
        set_deadline(3000.0)
        remaining_at_a = get_remaining_ms()
        assert remaining_at_a is not None
        # 네트워크 buffer 차감 후 약 2950ms
        assert remaining_at_a == pytest.approx(
            3000.0 - DEFAULT_NETWORK_LATENCY_BUFFER_MS, abs=10.0
        )

        # --- 서비스 A에서 처리 (2500ms 소요 시뮬레이션) ---
        # 실제 sleep 대신 deadline을 직접 조작하여 시간 경과를 시뮬레이션
        # A가 B를 호출할 때 전파 헤더 값 생성
        propagation_value = get_propagation_header_value()
        assert propagation_value is not None

        # --- 서비스 B: A의 deadline 수신 + 2500ms 소요 후 C 호출 ---
        # B가 수신한 시점의 남은 시간에서 2500ms를 빼는 것을 시뮬레이션
        clear_deadline()
        remaining_for_b = parse_deadline_header(propagation_value)
        assert remaining_for_b is not None

        # B에서 2500ms 소요 후 C에 전파할 남은 시간
        # 실제로는 time.monotonic() 경과가 반영되지만,
        # 테스트에서는 직접 계산하여 C의 deadline을 설정
        remaining_for_c = remaining_for_b - 2500.0  # B에서 2500ms 소요

        # --- 서비스 C: 남은 시간 아주 적음 ---
        if remaining_for_c > 0:
            set_deadline(remaining_for_c)
        else:
            set_deadline(0.0)

        # C의 GradientCalculator: 예상 처리시간 ~2000ms
        calc_c = get_gradient_calculator("admission_control:standard")
        calc_c.add_sample(2000.0)
        calc_c.add_sample(2000.0)

        # C에서 TrafficGate 판정
        decision = gate.should_allow(
            priority=50,
            metadata={"tier_id": "standard"},
        )

        # C의 estimated ≈ 2000ms × 1.5 = 3000ms >> remaining ≈ 450ms → Fast-Fail
        assert decision.allowed is False
        assert decision.gate == "DeadlineContext"
        assert (
            "fast-fail" in decision.reason.lower()
            or "expired" in decision.reason.lower()
        )

    def test_full_chain_service_b_passes_when_enough_time(self, mock_rate_controller):
        """서비스 B가 충분한 시간이 있으면 정상 통과한다."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        # A: 10초 deadline
        set_deadline(10000.0)

        # B: RTT가 짧은 서비스
        calc_b = get_gradient_calculator("admission_control:standard")
        calc_b.add_sample(100.0)
        calc_b.add_sample(100.0)

        decision = gate.should_allow(
            priority=50,
            metadata={"tier_id": "standard"},
        )

        # estimated ≈ 150ms, remaining ≈ 9950ms → 통과
        assert decision.allowed is True

    def test_chain_propagation_reduces_remaining(self):
        """호출 체인에서 전파될 때마다 남은 시간이 감소한다."""
        # 서비스 A: 5000ms deadline
        set_deadline(5000.0)
        remaining_a = get_remaining_ms()
        assert remaining_a is not None

        # A → B 전파 헤더
        header_a_to_b = get_propagation_header_value()
        assert header_a_to_b is not None

        # B가 수신
        clear_deadline()
        remaining_parsed = parse_deadline_header(header_a_to_b)
        assert remaining_parsed is not None

        # B에 deadline 설정 (네트워크 buffer 추가 차감)
        set_deadline(remaining_parsed)
        remaining_b = get_remaining_ms()
        assert remaining_b is not None

        # B의 남은 시간은 A보다 적다 (네트워크 buffer 2회 차감)
        assert remaining_b < remaining_a


# =============================================================================
# 2. Deadline Propagation Accuracy
# =============================================================================


class TestDeadlinePropagationAccuracy:
    """Deadline 전파 시 경과 시간이 정확히 반영되는지 검증."""

    def test_propagation_header_reflects_elapsed_time(self):
        """전파 헤더 값이 경과 시간만큼 감소한다."""
        set_deadline(5000.0)
        initial_remaining = get_remaining_ms()
        assert initial_remaining is not None

        # 약간의 시간 경과 후 전파 헤더 생성
        time.sleep(0.01)  # 10ms

        header = get_propagation_header_value()
        assert header is not None
        propagated = parse_deadline_header(header)
        assert propagated is not None

        # 전파 값은 초기 remaining보다 적어야 함 (최소 10ms 차이)
        # get_propagation_header_value()는 f"{remaining:.0f}ms" 포맷팅으로
        # 반올림이 발생하므로 +1ms 허용 (4949.5 → "4950ms")
        assert propagated < initial_remaining + 1.0
        assert propagated == pytest.approx(initial_remaining - 10.0, abs=20.0)

    def test_network_buffer_deducted_on_set(self):
        """set_deadline() 호출 시 네트워크 버퍼가 차감된다."""
        set_deadline(1000.0)
        remaining = get_remaining_ms()
        assert remaining is not None

        # 1000ms - 50ms(기본 buffer) = 950ms 근처
        assert remaining == pytest.approx(
            1000.0 - DEFAULT_NETWORK_LATENCY_BUFFER_MS, abs=10.0
        )

    def test_expired_deadline_produces_no_propagation_header(self):
        """만료된 deadline은 전파 헤더를 생성하지 않는다."""
        set_deadline(0.0)  # 즉시 만료 (buffer 차감 후 0 이하)

        header = get_propagation_header_value()
        assert header is None

    def test_deadline_scope_restores_previous(self):
        """deadline_scope 종료 시 이전 deadline이 복원된다."""
        set_deadline(5000.0)
        outer_remaining = get_remaining_ms()
        assert outer_remaining is not None

        with deadline_scope(1000.0):
            inner_remaining = get_remaining_ms()
            assert inner_remaining is not None
            assert inner_remaining < outer_remaining

        # Scope 종료 후 외부 deadline 복원
        restored = get_remaining_ms()
        assert restored is not None
        # 약간의 시간 경과는 있지만 outer_remaining에 가까움
        assert restored == pytest.approx(outer_remaining, abs=50.0)


# =============================================================================
# 3. RTT Feedback Loop
# =============================================================================


class TestRttFeedbackLoop:
    """
    RTT 피드백 루프: 응답 시간 → GradientCalculator → Fast-Fail 임계치 자동 조정.

    시나리오:
    1. 초기 RTT 100ms → estimated ≈ 150ms
    2. RTT 증가 → 500ms → estimated 상승
    3. 짧은 deadline에서 자동으로 Fast-Fail 발생
    """

    def test_rtt_increase_raises_estimated(self, mock_rate_controller):
        """RTT 증가 시 예상 처리시간도 자동으로 증가한다."""
        calc = get_gradient_calculator("admission_control:standard")

        # Phase 1: 안정 상태 (100ms)
        for _ in range(5):
            calc.add_sample(100.0)

        est_stable = get_estimated_processing_ms(
            calculator_name="admission_control:standard",
            tier_id="standard",
        )

        # Phase 2: RTT 급증 (500ms)
        for _ in range(5):
            calc.add_sample(500.0)

        est_increased = get_estimated_processing_ms(
            calculator_name="admission_control:standard",
            tier_id="standard",
        )

        # RTT 증가 추세 → estimated가 상승해야 함
        assert est_increased > est_stable

    def test_rtt_increase_triggers_fast_fail(self, mock_rate_controller):
        """RTT 증가로 estimated가 높아지면 짧은 deadline에서 Fast-Fail 발생."""
        gate = TrafficGate(rate_controller=mock_rate_controller)
        calc = get_gradient_calculator("admission_control:standard")

        # 안정 상태: RTT 100ms → estimated ≈ 150ms
        for _ in range(5):
            calc.add_sample(100.0)

        # 1초 deadline → 충분한 여유
        set_deadline(1000.0)
        decision_ok = gate.should_allow(priority=50, metadata={"tier_id": "standard"})
        assert decision_ok.allowed is True

        # RTT 급증: 800ms → estimated 상승
        clear_deadline()
        for _ in range(5):
            calc.add_sample(800.0)

        # 같은 1초 deadline → 이제 Fast-Fail
        set_deadline(1000.0)
        decision_fail = gate.should_allow(priority=50, metadata={"tier_id": "standard"})
        assert decision_fail.allowed is False
        assert decision_fail.gate == "DeadlineContext"

    def test_rtt_recovery_allows_traffic_again(self, mock_rate_controller):
        """RTT가 감소하면 다시 트래픽이 허용된다."""
        gate = TrafficGate(rate_controller=mock_rate_controller)
        calc = get_gradient_calculator("admission_control:standard")

        # Phase 1: 높은 RTT (600ms) → Fast-Fail 발생
        for _ in range(5):
            calc.add_sample(600.0)

        set_deadline(800.0)
        decision = gate.should_allow(priority=50, metadata={"tier_id": "standard"})
        assert decision.allowed is False

        # Phase 2: RTT 회복 (50ms) → 다시 허용
        clear_deadline()
        for _ in range(10):
            calc.add_sample(50.0)

        set_deadline(800.0)
        decision_recovered = gate.should_allow(
            priority=50, metadata={"tier_id": "standard"}
        )
        assert decision_recovered.allowed is True

    def test_gradient_positive_increases_safety_margin(self):
        """gradient > 0.1 이면 safety_margin이 증가하여 더 보수적으로 판정한다."""
        calc = get_gradient_calculator("admission_control:standard")

        # 안정 상태에서 estimated 측정
        for _ in range(5):
            calc.add_sample(200.0)
        get_estimated_processing_ms(
            calculator_name="admission_control:standard",
            safety_margin=1.5,
            tier_id="standard",
        )

        # RTT 급격히 증가 (gradient > 0.1)
        reset_gradient_calculators()
        calc2 = get_gradient_calculator("admission_control:standard")
        calc2.add_sample(100.0)
        calc2.add_sample(300.0)  # 200% 증가 → gradient > 0.1

        est_with_gradient = get_estimated_processing_ms(
            calculator_name="admission_control:standard",
            safety_margin=1.5,
            tier_id="standard",
        )

        rtt = calc2.get_current_rtt()
        # gradient 양수이면 effective_margin > 1.5
        # est_with_gradient > rtt * 1.5 or 이미 RTT가 높아서 est가 큼
        assert est_with_gradient > 0
        _, gradient = calc2.get_snapshot()
        if gradient > 0.1:
            # margin이 증가했으므로 단순 rtt * 1.5보다 커야 함
            assert est_with_gradient > rtt * 1.5


# =============================================================================
# 4. Cold Start Fast-Fail
# =============================================================================


class TestColdStartFastFail:
    """
    서비스 재시작 직후(Cold Start) → Tier별 기본값으로 Fast-Fail 즉시 작동.

    GradientCalculator에 RTT 데이터가 없어도 기본값으로 판정 가능.
    """

    def test_cold_start_critical_fast_fails_short_deadline(self, mock_rate_controller):
        """Cold Start + critical tier + 짧은 deadline → Fast-Fail."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        # critical 기본값 = 50ms, deadline = 30ms (buffer 50ms 차감 → 실질 0 이하)
        set_deadline(30.0)

        decision = gate.should_allow(priority=0, metadata={"tier_id": "critical"})
        assert decision.allowed is False

    def test_cold_start_standard_fast_fails_short_deadline(self, mock_rate_controller):
        """Cold Start + standard tier + deadline < 200ms → Fast-Fail."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        # standard 기본값 = 200ms, deadline = 150ms (buffer 50 차감 → 실질 100ms)
        set_deadline(150.0)

        decision = gate.should_allow(priority=50, metadata={"tier_id": "standard"})

        # remaining ≈ 100ms < estimated=200ms → Fast-Fail
        assert decision.allowed is False
        assert decision.gate == "DeadlineContext"

    def test_cold_start_non_essential_fast_fails_short_deadline(
        self, mock_rate_controller
    ):
        """Cold Start + non_essential tier + deadline < 500ms → Fast-Fail."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        # non_essential 기본값 = 500ms, deadline = 400ms (buffer 50 → 실질 350ms)
        set_deadline(400.0)

        decision = gate.should_allow(
            priority=100, metadata={"tier_id": "non_essential"}
        )

        # remaining ≈ 350ms < estimated=500ms → Fast-Fail
        assert decision.allowed is False
        assert decision.gate == "DeadlineContext"

    def test_cold_start_long_deadline_allows_through(self, mock_rate_controller):
        """Cold Start라도 deadline이 충분히 길면 허용된다."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        # 10초 deadline — 모든 tier에서 충분
        set_deadline(10000.0)

        for tier, priority in [
            ("critical", 0),
            ("standard", 50),
            ("non_essential", 100),
        ]:
            clear_deadline()
            set_deadline(10000.0)
            decision = gate.should_allow(priority=priority, metadata={"tier_id": tier})
            assert decision.allowed is True, f"{tier} tier should be allowed"

    def test_cold_start_tier_defaults_match_constants(self):
        """Cold Start 기본값이 문서 243의 설계 상수와 일치한다."""
        assert get_tier_default_estimated_ms("critical") == 50.0
        assert get_tier_default_estimated_ms("standard") == 200.0
        assert get_tier_default_estimated_ms("non_essential") == 500.0


# =============================================================================
# 5. Data Pollution Prevention
# =============================================================================


class TestDataPollutionPrevention:
    """
    Fast-Fail 거절/4xx 등 비정상 요청의 RTT가 smoothed_rtt를 오염시키지 않는지 검증.

    3중 필터링: 상태 코드 + 최소 임계치 + 확률 샘플링
    여기서는 GradientCalculator 레벨에서 외부 노이즈 주입 시
    smoothed_rtt 불변을 검증한다.
    """

    def test_smoothed_rtt_unchanged_after_no_new_samples(self):
        """add_sample()을 호출하지 않으면 smoothed_rtt가 변하지 않는다."""
        calc = get_gradient_calculator("admission_control:standard")

        # 초기 RTT 설정
        calc.add_sample(200.0)
        calc.add_sample(200.0)
        rtt_before = calc.get_current_rtt()
        assert rtt_before is not None

        # Fast-Fail 거절 시뮬레이션: add_sample()을 호출하지 않음
        # (AdmissionControlMiddleware는 2xx 응답만 수집하므로)

        rtt_after = calc.get_current_rtt()
        assert rtt_after == rtt_before

    def test_near_zero_sample_pollutes_rtt(self):
        """~0ms 샘플이 RTT에 주입되면 smoothed_rtt가 급락한다 (오염 시연)."""
        calc = GradientCalculator(smoothing_factor=0.5)

        # 안정 상태: 200ms
        for _ in range(5):
            calc.add_sample(200.0)
        rtt_stable = calc.get_current_rtt()
        assert rtt_stable is not None
        assert rtt_stable > 150.0

        # 오염: ~0ms 샘플 주입 (이것이 3중 필터링이 필요한 이유)
        for _ in range(5):
            calc.add_sample(1.0)
        rtt_polluted = calc.get_current_rtt()
        assert rtt_polluted is not None

        # 급락 확인 — 이것이 Data Pollution
        assert rtt_polluted < rtt_stable * 0.5

    def test_filter_prevents_pollution_in_estimated(self):
        """
        3중 필터링이 적용된 RTT 수집 경로에서는 오염이 발생하지 않는다.

        실제 AdmissionControlMiddleware는 Django 의존이므로,
        여기서는 '올바른 샘플만 넣으면 estimated가 안정적'임을 검증한다.
        """
        calc = get_gradient_calculator("admission_control:standard")

        # 정상 2xx 요청의 RTT만 수집 (200ms 안정)
        for _ in range(10):
            calc.add_sample(200.0)

        est = get_estimated_processing_ms(
            calculator_name="admission_control:standard",
            tier_id="standard",
        )

        # estimated ≈ 200 * 1.5 = 300ms 근처 (안정)
        assert est == pytest.approx(300.0, rel=0.2)


# =============================================================================
# 6. Tier별 RTT 격리
# =============================================================================


class TestTierRttIsolation:
    """
    critical/standard/non_essential tier의 GradientCalculator가
    서로 독립적인지 검증.
    """

    def test_critical_rtt_does_not_affect_standard(self):
        """critical tier RTT 변경이 standard tier estimated에 영향 없다."""
        # standard: 안정 RTT 100ms
        calc_std = get_gradient_calculator("admission_control:standard")
        for _ in range(5):
            calc_std.add_sample(100.0)

        est_std_before = get_estimated_processing_ms(
            calculator_name="admission_control:standard",
            tier_id="standard",
        )

        # critical: 높은 RTT 5000ms 주입
        calc_crit = get_gradient_calculator("admission_control:critical")
        for _ in range(5):
            calc_crit.add_sample(5000.0)

        est_std_after = get_estimated_processing_ms(
            calculator_name="admission_control:standard",
            tier_id="standard",
        )

        # standard estimated 불변
        assert est_std_after == pytest.approx(est_std_before, rel=0.01)

    def test_non_essential_rtt_does_not_affect_critical(self):
        """non_essential tier RTT 변경이 critical tier estimated에 영향 없다."""
        # critical: 안정 RTT 20ms
        calc_crit = get_gradient_calculator("admission_control:critical")
        for _ in range(5):
            calc_crit.add_sample(20.0)

        est_crit_before = get_estimated_processing_ms(
            calculator_name="admission_control:critical",
            tier_id="critical",
        )

        # non_essential: 매우 높은 RTT 10000ms
        calc_ne = get_gradient_calculator("admission_control:non_essential")
        for _ in range(5):
            calc_ne.add_sample(10000.0)

        est_crit_after = get_estimated_processing_ms(
            calculator_name="admission_control:critical",
            tier_id="critical",
        )

        # critical estimated 불변
        assert est_crit_after == pytest.approx(est_crit_before, rel=0.01)

    def test_all_three_tiers_independent_in_traffic_gate(self, mock_rate_controller):
        """3개 tier가 동시에 다른 RTT를 가질 때 TrafficGate가 올바르게 판정한다."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        # Tier별 RTT 설정
        # critical: 빠름 (30ms)
        calc_c = get_gradient_calculator("admission_control:critical")
        for _ in range(5):
            calc_c.add_sample(30.0)

        # standard: 보통 (200ms)
        calc_s = get_gradient_calculator("admission_control:standard")
        for _ in range(5):
            calc_s.add_sample(200.0)

        # non_essential: 느림 (2000ms)
        calc_ne = get_gradient_calculator("admission_control:non_essential")
        for _ in range(5):
            calc_ne.add_sample(2000.0)

        # 500ms deadline 설정
        set_deadline(500.0)  # buffer 차감 후 실질 ~450ms

        # critical: estimated ≈ 45ms → 450 > 45 → 허용
        decision_c = gate.should_allow(priority=0, metadata={"tier_id": "critical"})
        assert decision_c.allowed is True

        # standard: estimated ≈ 300ms → 450 > 300 → 허용
        clear_deadline()
        set_deadline(500.0)
        decision_s = gate.should_allow(priority=50, metadata={"tier_id": "standard"})
        assert decision_s.allowed is True

        # non_essential: estimated ≈ 3000ms → 450 < 3000 → Fast-Fail
        clear_deadline()
        set_deadline(500.0)
        decision_ne = gate.should_allow(
            priority=100, metadata={"tier_id": "non_essential"}
        )
        assert decision_ne.allowed is False
        assert decision_ne.gate == "DeadlineContext"

    def test_singleton_registry_returns_same_instance(self):
        """동일한 이름으로 요청하면 같은 GradientCalculator 인스턴스를 반환한다."""
        calc1 = get_gradient_calculator("admission_control:critical")
        calc2 = get_gradient_calculator("admission_control:critical")
        assert calc1 is calc2

    def test_different_names_return_different_instances(self):
        """다른 이름으로 요청하면 다른 인스턴스를 반환한다."""
        calc_c = get_gradient_calculator("admission_control:critical")
        calc_s = get_gradient_calculator("admission_control:standard")
        calc_ne = get_gradient_calculator("admission_control:non_essential")

        assert calc_c is not calc_s
        assert calc_s is not calc_ne
        assert calc_c is not calc_ne

    def test_reset_clears_all_instances(self):
        """reset_gradient_calculators()가 모든 인스턴스를 제거한다."""
        calc1 = get_gradient_calculator("admission_control:critical")
        calc1.add_sample(100.0)

        reset_gradient_calculators()

        # 재생성 후 데이터가 없어야 함
        calc2 = get_gradient_calculator("admission_control:critical")
        assert calc2 is not calc1
        assert calc2.get_current_rtt() is None


# =============================================================================
# 7. End-to-End: Deadline + GradientCalculator + TrafficGate 통합
# =============================================================================


class TestEndToEndIntegration:
    """Deadline Context + GradientCalculator + TrafficGate 전체 파이프라인 통합."""

    def test_no_deadline_skips_all_deadline_checks(self, mock_rate_controller):
        """Deadline 미설정 시 모든 deadline 관련 체크가 스킵된다."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        # GradientCalculator에 높은 RTT가 있어도 deadline 없으면 무관
        calc = get_gradient_calculator("admission_control:standard")
        for _ in range(5):
            calc.add_sample(5000.0)

        decision = gate.should_allow(priority=50, metadata={"tier_id": "standard"})
        # deadline 미설정 → should_fast_fail=False → 허용
        assert decision.allowed is True

    def test_expired_deadline_rejected_before_fast_fail(self, mock_rate_controller):
        """이미 만료된 deadline은 Fast-Fail 판정 이전에 거부된다."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        set_deadline(0.0)  # 즉시 만료

        decision = gate.should_allow(priority=50, metadata={"tier_id": "standard"})
        assert decision.allowed is False
        assert "expired" in decision.reason.lower()

    def test_rtt_data_transitions_from_cold_start_to_measured(
        self, mock_rate_controller
    ):
        """Cold Start → RTT 데이터 축적 → estimated가 기본값에서 측정값으로 전환된다."""
        # Cold Start: standard 기본값 200ms
        est_cold = get_estimated_processing_ms(
            calculator_name="admission_control:standard",
            tier_id="standard",
        )
        assert est_cold == DEFAULT_ESTIMATED_MS_STANDARD

        # RTT 데이터 축적 (80ms 안정)
        calc = get_gradient_calculator("admission_control:standard")
        for _ in range(5):
            calc.add_sample(80.0)

        # 이제 측정값 기반: 약 80 * 1.5 = 120ms
        est_measured = get_estimated_processing_ms(
            calculator_name="admission_control:standard",
            tier_id="standard",
        )
        assert est_measured != DEFAULT_ESTIMATED_MS_STANDARD
        assert est_measured == pytest.approx(80.0 * 1.5, rel=0.2)

    def test_traffic_gate_pipeline_order_deadline_before_rate_limit(
        self, mock_rate_controller
    ):
        """TrafficGate 파이프라인에서 deadline 체크가 rate limit보다 먼저 실행된다."""
        # Rate limit도 거부하도록 설정
        mock_rate_controller.should_process.return_value = False
        gate = TrafficGate(rate_controller=mock_rate_controller)

        # 만료된 deadline 설정
        set_deadline(0.0)

        decision = gate.should_allow(priority=50, metadata={"tier_id": "standard"})

        # Deadline expired가 먼저 반환됨 (RateController까지 가지 않음)
        assert decision.allowed is False
        assert decision.gate == "DeadlineContext"
        # RateController.should_process()가 호출되지 않았음
        mock_rate_controller.should_process.assert_not_called()

    def test_fast_fail_metadata_contains_diagnostic_info(self, mock_rate_controller):
        """Fast-Fail 시 metadata에 진단 정보(estimated_ms, fast_fail)가 포함된다."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        calc = get_gradient_calculator("admission_control:standard")
        calc.add_sample(500.0)
        calc.add_sample(500.0)

        set_deadline(300.0)  # 실질 ~250ms < estimated ~750ms

        decision = gate.should_allow(priority=50, metadata={"tier_id": "standard"})

        assert decision.allowed is False
        assert decision.metadata is not None
        assert "estimated_ms" in decision.metadata
        assert decision.metadata["fast_fail"] is True
        assert isinstance(decision.metadata["estimated_ms"], float)
        assert decision.metadata["estimated_ms"] > 0
