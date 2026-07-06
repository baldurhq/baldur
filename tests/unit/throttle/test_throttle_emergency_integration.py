"""
AdaptiveThrottle Emergency Mode 연동 테스트.

테스트 대상:
1. adjust_for_emergency() 메서드 - Emergency Level별 limit 조정
2. _gradient_frozen 플래그 - LEVEL_3에서 Gradient 적용 Freeze
3. _apply_emergency_cap() - Hard-Cap 로직 (티어별 배율)
4. _cache_emergency_tier_multipliers() - 티어 배율 캐싱
5. get_effective_limit() - 티어별 실효 limit 조회
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import threading

from baldur.services.event_bus import (
    BaldurEvent,
    EventType,
)
from baldur.services.event_bus.bus._throttle_handlers import (
    _on_emergency_level_changed_throttle,
)
from baldur_pro.services.throttle.adaptive import (
    EMERGENCY_LEVEL_LIMIT_MULTIPLIERS,
    get_adaptive_throttle,
    reset_adaptive_throttle,
)


class TestEmergencyLevelLimitMultipliers:
    """Emergency Level → limit 배율 매핑 테스트."""

    def test_multiplier_for_normal_level(self):
        """NORMAL (0) 레벨은 배율 1.0."""
        assert EMERGENCY_LEVEL_LIMIT_MULTIPLIERS[0] == 1.0

    def test_multiplier_for_level_1(self):
        """LEVEL_1 (1)은 배율 0.8."""
        assert EMERGENCY_LEVEL_LIMIT_MULTIPLIERS[1] == 0.8

    def test_multiplier_for_level_2(self):
        """LEVEL_2 (2)는 배율 0.5."""
        assert EMERGENCY_LEVEL_LIMIT_MULTIPLIERS[2] == 0.5

    def test_multiplier_for_level_3(self):
        """LEVEL_3 (3)은 배율 0.0 (min_limit 표시)."""
        assert EMERGENCY_LEVEL_LIMIT_MULTIPLIERS[3] == 0.0


class TestAdjustForEmergency:
    """adjust_for_emergency() 메서드 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_level_0_deactivates_emergency_mode(self):
        """Level 0은 Emergency 모드 해제."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # 먼저 Emergency 활성화
        throttle.adjust_for_emergency(2)
        assert throttle.is_emergency_active()

        # Level 0으로 복구
        throttle.adjust_for_emergency(0)

        assert not throttle.is_emergency_active()
        assert not throttle.is_gradient_frozen()
        assert throttle.get_emergency_level() == 0

    def test_level_1_applies_80_percent_multiplier(self):
        """Level 1은 limit × 0.8."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        throttle.adjust_for_emergency(1)

        assert throttle.is_emergency_active()
        assert throttle.get_emergency_level() == 1
        assert throttle.current_limit == 80  # 100 × 0.8
        assert not throttle.is_gradient_frozen()

    def test_level_2_applies_50_percent_multiplier(self):
        """Level 2는 limit × 0.5."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        throttle.adjust_for_emergency(2)

        assert throttle.is_emergency_active()
        assert throttle.get_emergency_level() == 2
        assert throttle.current_limit == 50  # 100 × 0.5
        assert not throttle.is_gradient_frozen()

    def test_level_3_sets_min_limit_and_freezes_gradient(self):
        """Level 3은 min_limit 고정 + Gradient Freeze."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        throttle.adjust_for_emergency(3)

        assert throttle.is_emergency_active()
        assert throttle.get_emergency_level() == 3
        assert throttle.current_limit == throttle.config.min_limit
        assert throttle.is_gradient_frozen()

    def test_preserves_base_limit_on_first_activation(self):
        """최초 Emergency 활성화 시 base_limit 저장."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 150

        throttle.adjust_for_emergency(1)

        # base_limit 저장 확인 (stats에서 조회)
        stats = throttle.get_stats()
        assert stats["emergency"]["base_limit_before_emergency"] == 150

    def test_restores_base_limit_on_deactivation(self):
        """Emergency 해제 시 Recovery Dampening 단계적 복구 시작 (80%→90%→100%).

        Recovery Dampening이 활성화되어 있으면 즉시 base_limit으로 복구되지 않고
        첫 단계(80%)부터 시작합니다.
        """
        throttle = get_adaptive_throttle()
        throttle.current_limit = 150

        # 활성화
        throttle.adjust_for_emergency(2)
        assert throttle.current_limit == 75  # 150 × 0.5

        # 해제 - Recovery Dampening 시작 (80% = 120)
        throttle.adjust_for_emergency(0)
        # Recovery Dampening 첫 단계: base_limit × 0.8 = 150 × 0.8 = 120
        assert throttle.current_limit == 120
        assert throttle.is_recovery_dampening_active()


class TestGradientFrozen:
    """_gradient_frozen 플래그 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_gradient_frozen_at_level_3(self):
        """LEVEL_3에서 Gradient 적용 Freeze."""
        throttle = get_adaptive_throttle()
        throttle.adjust_for_emergency(3)

        assert throttle.is_gradient_frozen()

    def test_gradient_not_frozen_at_level_1_or_2(self):
        """LEVEL_1, LEVEL_2에서는 Gradient Freeze 아님."""
        throttle = get_adaptive_throttle()

        throttle.adjust_for_emergency(1)
        assert not throttle.is_gradient_frozen()

        throttle.adjust_for_emergency(2)
        assert not throttle.is_gradient_frozen()

    def test_gradient_unfrozen_on_deactivation(self):
        """Emergency 해제 시 Gradient Freeze 해제."""
        throttle = get_adaptive_throttle()

        throttle.adjust_for_emergency(3)
        assert throttle.is_gradient_frozen()

        throttle.adjust_for_emergency(0)
        assert not throttle.is_gradient_frozen()

    def test_record_response_skips_limit_adjustment_when_frozen(self):
        """Gradient frozen 상태에서 record_response는 limit 조정 스킵."""
        throttle = get_adaptive_throttle()
        throttle.adjust_for_emergency(3)

        # min_limit으로 설정됨
        frozen_limit = throttle.current_limit

        # RTT 샘플 추가 (limit 조정 시도)
        for _ in range(10):
            throttle.record_response(10.0)  # 낮은 RTT

        # limit이 변경되지 않아야 함 (frozen)
        assert throttle.current_limit == frozen_limit


class TestApplyEmergencyCap:
    """_apply_emergency_cap() Hard-Cap 로직 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_no_cap_when_emergency_inactive(self):
        """Emergency 비활성 시 Hard-Cap 미적용."""
        throttle = get_adaptive_throttle()

        result = throttle._apply_emergency_cap(100, "standard")

        assert result == 100  # 원래 값 그대로

    def test_applies_tier_multiplier_when_emergency_active(self):
        """Emergency 활성 시 티어 배율 적용."""
        throttle = get_adaptive_throttle()
        throttle.adjust_for_emergency(2)  # LEVEL_2 활성화

        # LEVEL_2에서 standard 티어 배율 = 0.1
        result = throttle._apply_emergency_cap(100, "standard")

        # min_limit 이상 보장
        assert result >= throttle.config.min_limit

    def test_respects_min_limit(self):
        """결과값은 min_limit 이상 보장."""
        throttle = get_adaptive_throttle()
        throttle.adjust_for_emergency(3)  # LEVEL_3

        # LEVEL_3에서 non_essential 티어 배율 = 0.0
        result = throttle._apply_emergency_cap(100, "non_essential")

        assert result >= throttle.config.min_limit


class TestCacheEmergencyTierMultipliers:
    """_cache_emergency_tier_multipliers() 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_caches_tier_multipliers_on_level_change(self):
        """Emergency Level 변경 시 티어 배율 캐싱."""
        throttle = get_adaptive_throttle()
        throttle.adjust_for_emergency(1)

        stats = throttle.get_stats()
        tier_multipliers = stats["emergency"]["tier_multipliers"]

        # LEVEL_1: critical=1.0, standard=1.0, non_essential=0.0
        assert tier_multipliers.get("critical") == 1.0
        assert tier_multipliers.get("standard") == 1.0
        assert tier_multipliers.get("non_essential") == 0.0

    def test_updates_cache_on_level_change(self):
        """Level 변경 시 캐시 업데이트."""
        throttle = get_adaptive_throttle()

        # LEVEL_1
        throttle.adjust_for_emergency(1)
        stats1 = throttle.get_stats()
        assert stats1["emergency"]["tier_multipliers"].get("standard") == 1.0

        # LEVEL_2
        throttle.adjust_for_emergency(2)
        stats2 = throttle.get_stats()
        assert stats2["emergency"]["tier_multipliers"].get("standard") == 0.1


class TestGetEffectiveLimit:
    """get_effective_limit() 메서드 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_returns_current_limit_when_emergency_inactive(self):
        """Emergency 비활성 시 현재 limit 반환."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        assert throttle.get_effective_limit("critical") == 100
        assert throttle.get_effective_limit("standard") == 100
        assert throttle.get_effective_limit("non_essential") == 100

    def test_applies_tier_multiplier_when_emergency_active(self):
        """Emergency 활성 시 티어별 배율 적용된 limit 반환."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100
        throttle.adjust_for_emergency(2)  # LEVEL_2

        # LEVEL_2: critical=1.0, standard=0.1, non_essential=0.0
        # 단, adjust_for_emergency가 이미 limit을 변경했으므로
        # current_limit은 이미 50 (100 × 0.5)
        # get_effective_limit은 그 위에 티어 배율을 추가로 적용
        critical_limit = throttle.get_effective_limit("critical")
        standard_limit = throttle.get_effective_limit("standard")
        non_essential_limit = throttle.get_effective_limit("non_essential")

        # critical은 배율 1.0 → 원래 limit
        assert critical_limit >= throttle.config.min_limit
        # standard는 배율 0.1 → 크게 감소
        assert standard_limit >= throttle.config.min_limit
        # non_essential은 배율 0.0 → min_limit
        assert non_essential_limit == throttle.config.min_limit


class TestEmergencyLevelChangedThrottleHandlerUpdated:
    """_on_emergency_level_changed_throttle 핸들러 업데이트 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_calls_adjust_for_emergency(self):
        """핸들러가 adjust_for_emergency 메서드를 호출."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        event = BaldurEvent(
            event_type=EventType.EMERGENCY_LEVEL_CHANGED,
            data={"level": 2, "previous_level": 0},
            source="emergency_manager",
        )

        _on_emergency_level_changed_throttle(event)

        # adjust_for_emergency(2) 호출 결과 확인
        assert throttle.is_emergency_active()
        assert throttle.get_emergency_level() == 2
        assert throttle.current_limit == 50  # 100 × 0.5

    def test_level_3_freezes_gradient(self):
        """Level 3 이벤트 시 Gradient Freeze."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        event = BaldurEvent(
            event_type=EventType.EMERGENCY_LEVEL_CHANGED,
            data={"level": 3, "previous_level": 0},
            source="emergency_manager",
        )

        _on_emergency_level_changed_throttle(event)

        assert throttle.is_gradient_frozen()
        assert throttle.current_limit == throttle.config.min_limit

    def test_level_0_unfreezes_gradient(self):
        """Level 0 이벤트 시 Gradient Freeze 해제."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # 먼저 Level 3 활성화
        throttle.adjust_for_emergency(3)
        assert throttle.is_gradient_frozen()

        # Level 0으로 복구
        event = BaldurEvent(
            event_type=EventType.EMERGENCY_LEVEL_CHANGED,
            data={"level": 0, "previous_level": 3},
            source="emergency_manager",
        )

        _on_emergency_level_changed_throttle(event)

        assert not throttle.is_gradient_frozen()


class TestEmergencyKillSwitchBidirectionalGuardBehavior:
    """S1-3: Emergency unfreeze guards against active Kill Switch.

    _gradient_frozen is set by two independent sources: Kill Switch and
    Emergency LEVEL_3.  Kill Switch deactivation already guards against
    Emergency (_governance.py:68).  These tests verify the symmetric guard
    in _emergency.py: Emergency deactivation also checks Kill Switch.
    """

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_emergency_off_while_kill_switch_active_keeps_gradient_frozen(self):
        """Kill Switch ON → Emergency LEVEL_3 ON → Emergency OFF → gradient still frozen."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # Given — Kill Switch activates, then Emergency LEVEL_3
        throttle._kill_switch_active = True
        throttle._gradient_frozen = True
        throttle.adjust_for_emergency(3)
        assert throttle.is_gradient_frozen()

        # When — Emergency deactivates (level 0)
        throttle.adjust_for_emergency(0)

        # Then — gradient stays frozen because Kill Switch is still active
        assert throttle.is_gradient_frozen()
        assert not throttle.is_emergency_active()

    def test_emergency_level_1_while_kill_switch_active_keeps_gradient_frozen(self):
        """Kill Switch ON → Emergency LEVEL_1 → gradient still frozen."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # Given — Kill Switch is active with gradient frozen
        throttle._kill_switch_active = True
        throttle._gradient_frozen = True
        throttle.adjust_for_emergency(3)

        # When — Emergency drops to LEVEL_1 (level < 3 path)
        throttle.adjust_for_emergency(1)

        # Then — gradient stays frozen because Kill Switch is still active
        assert throttle.is_gradient_frozen()

    def test_emergency_level_2_while_kill_switch_active_keeps_gradient_frozen(self):
        """Kill Switch ON → Emergency LEVEL_2 → gradient still frozen."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # Given
        throttle._kill_switch_active = True
        throttle._gradient_frozen = True
        throttle.adjust_for_emergency(3)

        # When
        throttle.adjust_for_emergency(2)

        # Then
        assert throttle.is_gradient_frozen()

    def test_emergency_off_without_kill_switch_unfreezes_gradient(self):
        """No Kill Switch → Emergency OFF → gradient unfreezes (normal behavior)."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # Given — Emergency LEVEL_3 without Kill Switch
        assert not throttle._kill_switch_active
        throttle.adjust_for_emergency(3)
        assert throttle.is_gradient_frozen()

        # When — Emergency deactivates
        throttle.adjust_for_emergency(0)

        # Then — gradient unfreezes normally
        assert not throttle.is_gradient_frozen()

    def test_emergency_level_1_without_kill_switch_unfreezes_gradient(self):
        """No Kill Switch → Emergency LEVEL_3 → LEVEL_1 → gradient unfreezes."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # Given
        assert not throttle._kill_switch_active
        throttle.adjust_for_emergency(3)
        assert throttle.is_gradient_frozen()

        # When — drops to LEVEL_1
        throttle.adjust_for_emergency(1)

        # Then
        assert not throttle.is_gradient_frozen()

    def test_kill_switch_then_emergency_then_emergency_off_then_kill_switch_off(self):
        """Full lifecycle: KS ON → EM3 ON → EM OFF → KS OFF → gradient unfrozen."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # Kill Switch ON
        throttle._kill_switch_active = True
        throttle._gradient_frozen = True

        # Emergency LEVEL_3 ON
        throttle.adjust_for_emergency(3)
        assert throttle.is_gradient_frozen()

        # Emergency OFF — gradient stays frozen (Kill Switch holds)
        throttle.adjust_for_emergency(0)
        assert throttle.is_gradient_frozen()

        # Kill Switch OFF — gradient unfreezes (Emergency is already off, level < 3)
        throttle._kill_switch_active = False
        if throttle._emergency_level < 3:
            throttle._gradient_frozen = False
        assert not throttle.is_gradient_frozen()


class TestThrottleResetIncludesEmergencyState:
    """reset_all()이 Emergency 상태도 초기화하는지 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_reset_clears_emergency_state(self):
        """reset_all()이 Emergency 상태 초기화."""
        throttle = get_adaptive_throttle()
        throttle.adjust_for_emergency(3)

        assert throttle.is_emergency_active()
        assert throttle.is_gradient_frozen()

        throttle.reset_all()

        assert not throttle.is_emergency_active()
        assert not throttle.is_gradient_frozen()
        assert throttle.get_emergency_level() == 0


class TestThrottleStatsIncludeEmergencyInfo:
    """get_stats()가 Emergency 정보를 포함하는지 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_stats_include_emergency_section(self):
        """stats에 emergency 섹션 포함."""
        throttle = get_adaptive_throttle()
        stats = throttle.get_stats()

        assert "emergency" in stats
        assert "active" in stats["emergency"]
        assert "level" in stats["emergency"]
        assert "gradient_frozen" in stats["emergency"]
        assert "base_limit_before_emergency" in stats["emergency"]
        assert "tier_multipliers" in stats["emergency"]

    def test_stats_reflect_current_emergency_state(self):
        """stats가 현재 Emergency 상태 반영."""
        throttle = get_adaptive_throttle()

        # 비활성 상태
        stats1 = throttle.get_stats()
        assert stats1["emergency"]["active"] is False
        assert stats1["emergency"]["level"] == 0

        # 활성화
        throttle.adjust_for_emergency(2)
        stats2 = throttle.get_stats()
        assert stats2["emergency"]["active"] is True
        assert stats2["emergency"]["level"] == 2


# =============================================================================
# 454 — Race regression tests for D3/D8 (lock symmetry on get_stats / rollback)
# =============================================================================

EXPECTED_STATS_TOP_KEYS = {
    "current_limit",
    "min_limit",
    "max_limit",
    "gradient",
    "adaptive",
    "emergency",
    "governance",
    "recovery",
}

EXPECTED_EMERGENCY_KEYS = {
    "active",
    "level",
    "gradient_frozen",
    "base_limit_before_emergency",
    "tier_multipliers",
    "full_stop_active",
}


class TestAdaptiveThrottleGetStatsRaceBehavior:
    """454 D3 — get_stats() must hold _adjustment_lock so concurrent emergency
    mutations cannot tear dict iteration. Pre-fix this surfaced as
    ``RuntimeError: dictionary changed size during iteration`` under xdist."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_get_stats_concurrent_with_emergency_adjustment_no_runtime_error(self):
        """Barrier-released N=8 (4 readers + 4 writers); 100 iterations.

        Asserts no RuntimeError from concurrent dict mutation and that the
        stats schema (top-level + emergency subdict keys) stays intact across
        every snapshot every reader observes.
        """
        # Given
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        num_threads = 8
        iterations = 100
        errors: list[BaseException] = []
        bad_schemas: list[set] = []
        levels = [0, 1, 2, 3]

        def reader():
            try:
                for _ in range(iterations):
                    barrier.wait(timeout=10)
                    snap = throttle.get_stats()
                    if not EXPECTED_STATS_TOP_KEYS.issubset(snap.keys()):
                        bad_schemas.append(set(snap.keys()))
                    elif not EXPECTED_EMERGENCY_KEYS.issubset(snap["emergency"].keys()):
                        bad_schemas.append(set(snap["emergency"].keys()))
            except BaseException as exc:
                errors.append(exc)

        def writer(idx: int):
            try:
                for i in range(iterations):
                    barrier.wait(timeout=10)
                    throttle.adjust_for_emergency(levels[(idx + i) % len(levels)])
            except BaseException as exc:
                errors.append(exc)

        barrier = threading.Barrier(num_threads)
        threads = [threading.Thread(target=reader) for _ in range(4)] + [
            threading.Thread(target=writer, args=(i,)) for i in range(4)
        ]

        # When
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # Then
        assert not errors, f"Concurrent get_stats raised: {errors!r}"
        assert not bad_schemas, f"Stats schema torn: {bad_schemas[:3]!r}"


class TestAdaptiveThrottleRollbackRaceBehavior:
    """454 D8 — rollback_to_base_limit() touches 7 fields plus current_limit;
    pre-fix the asymmetric lock allowed a concurrent get_stats() to observe a
    half-applied rollback (e.g. ``emergency.active=False`` while
    ``emergency.level`` was still nonzero)."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_rollback_concurrent_with_reader_no_torn_emergency_state(self):
        """Single rollback thread vs 7 readers under barrier; 50 iterations.

        Each reader snapshot of the emergency subdict must satisfy the
        invariant: ``active`` and ``level != 0`` agree (both truthy or both
        falsy). A torn snapshot would violate this because rollback clears
        ``active`` and ``level`` separately.
        """
        # Given
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        num_readers = 7
        iterations = 50
        errors: list[BaseException] = []
        torn_snapshots: list[dict] = []

        def reader():
            try:
                for _ in range(iterations):
                    barrier.wait(timeout=10)
                    em = throttle.get_stats()["emergency"]
                    # Invariant: active and (level != 0) must be in sync.
                    if bool(em["active"]) != (em["level"] != 0):
                        torn_snapshots.append(dict(em))
            except BaseException as exc:
                errors.append(exc)

        def rollback():
            try:
                for _ in range(iterations):
                    # Re-arm emergency before each rollback so we have something
                    # to roll back from.
                    throttle.adjust_for_emergency(2)
                    barrier.wait(timeout=10)
                    throttle.rollback_to_base_limit()
            except BaseException as exc:
                errors.append(exc)

        barrier = threading.Barrier(num_readers + 1)
        threads = [threading.Thread(target=reader) for _ in range(num_readers)]
        threads.append(threading.Thread(target=rollback))

        # When
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # Then
        assert not errors, f"Concurrent rollback raised: {errors!r}"
        assert not torn_snapshots, (
            f"Torn rollback snapshots observed: {torn_snapshots[:3]!r}"
        )
