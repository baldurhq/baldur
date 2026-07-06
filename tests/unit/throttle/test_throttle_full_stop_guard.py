"""
FullStopGuard 단위 테스트.

테스트 대상: baldur.resilience.policies.guards.full_stop.FullStopGuard
            baldur.resilience.policies.guards.full_stop.create_default_full_stop_guard

검증 범위:
- name 속성 계약값
- 3중 조건 (Emergency L3 + DB CB OPEN + Budget 소진) 모두 충족 시 거부
- 개별 조건 미충족 시 통과
- create_default_full_stop_guard() 팩토리 함수 Fail-Open 동작
"""

from __future__ import annotations

from unittest.mock import patch

from baldur.resilience.policies.guards.full_stop import (
    FullStopGuard,
    create_default_full_stop_guard,
)

# =============================================================================
# name 계약 검증
# =============================================================================


class TestFullStopGuardNameContract:
    """FullStopGuard.name 계약값 검증."""

    def test_name_is_full_stop(self):
        """name은 'full_stop'이어야 한다."""
        guard = FullStopGuard(
            emergency_provider=lambda: 0,
            cb_state_provider=lambda svc: "closed",
            budget_provider=lambda: 100.0,
        )
        assert guard.name == "full_stop"


# =============================================================================
# 3중 조건 동작 검증
# =============================================================================


class TestFullStopGuardTripleConditionBehavior:
    """Full Stop 3중 조건 동작 검증."""

    def test_all_three_conditions_met_rejects(self):
        """Emergency L3 + DB CB OPEN + Budget 소진 시 거부."""
        guard = FullStopGuard(
            emergency_provider=lambda: 3,
            cb_state_provider=lambda svc: "open",
            budget_provider=lambda: 0.0,
        )
        result = guard.check()
        assert result.allowed is False
        assert "full_stop" in result.reason

    def test_emergency_below_3_passes(self):
        """Emergency Level < 3이면 통과."""
        guard = FullStopGuard(
            emergency_provider=lambda: 2,
            cb_state_provider=lambda svc: "open",
            budget_provider=lambda: 0.0,
        )
        result = guard.check()
        assert result.allowed is True

    def test_cb_closed_passes(self):
        """CB가 closed이면 통과."""
        guard = FullStopGuard(
            emergency_provider=lambda: 3,
            cb_state_provider=lambda svc: "closed",
            budget_provider=lambda: 0.0,
        )
        result = guard.check()
        assert result.allowed is True

    def test_budget_remaining_positive_passes(self):
        """Budget 잔여량 > 0이면 통과."""
        guard = FullStopGuard(
            emergency_provider=lambda: 3,
            cb_state_provider=lambda svc: "open",
            budget_provider=lambda: 10.0,
        )
        result = guard.check()
        assert result.allowed is True

    def test_only_emergency_3_not_enough(self):
        """Emergency L3만으로는 거부 불가 (3중 조건)."""
        guard = FullStopGuard(
            emergency_provider=lambda: 3,
            cb_state_provider=lambda svc: "closed",
            budget_provider=lambda: 50.0,
        )
        result = guard.check()
        assert result.allowed is True

    def test_metadata_contains_details_on_reject(self):
        """거부 시 metadata에 세부 정보가 포함되어야 한다."""
        guard = FullStopGuard(
            emergency_provider=lambda: 3,
            cb_state_provider=lambda svc: "open",
            budget_provider=lambda: -5.0,
        )
        result = guard.check()
        assert result.allowed is False
        assert result.metadata["emergency_level"] == 3
        assert result.metadata["db_cb_state"] == "open"
        assert result.metadata["budget_remaining"] == -5.0

    def test_cb_state_provider_receives_database_service_name(self):
        """cb_state_provider는 'database' 서비스명으로 호출되어야 한다."""
        called_with = []

        def cb_provider(service: str) -> str:
            called_with.append(service)
            return "closed"

        guard = FullStopGuard(
            emergency_provider=lambda: 3,
            cb_state_provider=cb_provider,
            budget_provider=lambda: 0.0,
        )
        guard.check()
        assert "database" in called_with

    def test_emergency_level_above_3_also_rejects(self):
        """Emergency Level > 3 (예: 4)도 거부 조건에 해당."""
        guard = FullStopGuard(
            emergency_provider=lambda: 4,
            cb_state_provider=lambda svc: "open",
            budget_provider=lambda: 0.0,
        )
        result = guard.check()
        assert result.allowed is False


# =============================================================================
# create_default_full_stop_guard() 팩토리 검증
# =============================================================================


class TestCreateDefaultFullStopGuardBehavior:
    """create_default_full_stop_guard() 팩토리 함수 동작 검증."""

    def test_returns_full_stop_guard_instance(self):
        """FullStopGuard 인스턴스를 반환해야 한다."""
        guard = create_default_full_stop_guard()
        assert isinstance(guard, FullStopGuard)
        assert guard.name == "full_stop"

    def test_emergency_provider_failopen_returns_0(self):
        """Emergency import 실패 시 provider는 0을 반환해야 한다."""
        guard = create_default_full_stop_guard()
        # Emergency 모듈이 없어도 provider가 0 반환 → L3 조건 미충족 → 통과
        with patch.dict("sys.modules", {"baldur_pro.services.emergency_mode": None}):
            result = guard.check()
            assert result.allowed is True

    def test_cb_provider_failopen_returns_closed(self):
        """CB 모듈 import 실패 시 provider는 'closed'를 반환해야 한다."""
        guard = create_default_full_stop_guard()
        with patch.dict("sys.modules", {"baldur.services.circuit_breaker": None}):
            result = guard.check()
            assert result.allowed is True

    def test_budget_provider_failopen_returns_100(self):
        """Budget 모듈 import 실패 시 provider는 100.0을 반환해야 한다."""
        guard = create_default_full_stop_guard()
        with patch.dict("sys.modules", {"baldur_pro.services.error_budget": None}):
            result = guard.check()
            assert result.allowed is True
