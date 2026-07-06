"""
드리프트 복구 테스트.
"""

from datetime import UTC, datetime, timedelta

import pytest


class TestDriftReconcilerHalfOpenXorBehavior:
    """476 D7: full XOR resolution matrix.

    Pre-476 the priority rule (OPEN > HALF_OPEN > CLOSED) governed
    every drift. Post-476 the L2-Lua atomic OPEN→HALF_OPEN transition
    must survive Worker B's drift reconciliation on L2 recovery, so
    *exactly one side HALF_OPEN* drifts now resolve by timestamp
    instead of priority. This test class is the regression guard for
    the four (l1_state, l2_state) combinations × the two timestamp
    orderings, plus the no-timestamp fallback that prefers HALF_OPEN.
    """

    def setup_method(self):
        from baldur.adapters.memory.circuit_breaker import get_drift_reconciler

        self.reconciler = get_drift_reconciler()
        self.reconciler.clear_history()

    @pytest.mark.parametrize(
        ("l1_state", "l2_state", "l1_newer", "expected_state", "expected_result_attr"),
        [
            # XOR matrix: HALF_OPEN newer (own side wins).
            ("half_open", "open", True, "half_open", "TIMESTAMP_HALF_OPEN_L1"),
            ("half_open", "closed", True, "half_open", "TIMESTAMP_HALF_OPEN_L1"),
            ("open", "half_open", False, "half_open", "TIMESTAMP_HALF_OPEN_L2"),
            ("closed", "half_open", False, "half_open", "TIMESTAMP_HALF_OPEN_L2"),
            # XOR matrix: non-HALF_OPEN side newer (still uses XOR enum, but the
            # newer non-HALF_OPEN state wins by timestamp — Most Restrictive
            # Wins is bypassed for any XOR drift).
            ("half_open", "open", False, "open", "TIMESTAMP_HALF_OPEN_L2"),
            ("half_open", "closed", False, "closed", "TIMESTAMP_HALF_OPEN_L2"),
            ("open", "half_open", True, "open", "TIMESTAMP_HALF_OPEN_L1"),
            ("closed", "half_open", True, "closed", "TIMESTAMP_HALF_OPEN_L1"),
        ],
    )
    def test_xor_matrix_resolves_by_timestamp(
        self, l1_state, l2_state, l1_newer, expected_state, expected_result_attr
    ):
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()
        anchor = datetime.now(UTC)
        l1_time = anchor if l1_newer else anchor - timedelta(seconds=10)
        l2_time = anchor - timedelta(seconds=10) if l1_newer else anchor

        winner_state, result = reconciler.reconcile(
            service_name="svc",
            l1_state=l1_state,
            l2_state=l2_state,
            l1_updated_at=l1_time,
            l2_updated_at=l2_time,
        )

        assert winner_state == expected_state
        assert result == getattr(DriftReconciliationResult, expected_result_attr)

    def test_xor_no_timestamps_prefers_half_open_l2(self):
        """No timestamps + L2 is HALF_OPEN → TIMESTAMP_HALF_OPEN_L2.

        The fallback deliberately favors the HALF_OPEN side so that an
        L2-Lua-driven transition isn't lost when both sides forgot to
        stamp ``updated_at`` (e.g., test fixtures, legacy data).
        """
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        winner_state, result = reconciler.reconcile(
            service_name="svc",
            l1_state="open",
            l2_state="half_open",
        )

        assert winner_state == "half_open"
        assert result == DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L2

    def test_open_vs_closed_still_uses_most_restrictive_wins(self):
        """Non-XOR drifts (no HALF_OPEN involved) keep the priority rule.

        D7 only added an exception for the XOR case. OPEN vs CLOSED is the
        original ``L1_WINS`` / ``L2_WINS`` partition-protection branch and
        must remain unchanged — relaxing it would let a stale L2 less-
        restrictive state win during a partition.
        """
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        winner_state, result = reconciler.reconcile(
            service_name="svc",
            l1_state="closed",
            l2_state="open",
        )

        assert winner_state == "open"
        assert result == DriftReconciliationResult.L2_WINS


class TestDriftReconciliation:
    """드리프트 복구 테스트."""

    def setup_method(self):
        """각 테스트 전 DriftReconciler 초기화."""
        from baldur.adapters.memory.circuit_breaker import get_drift_reconciler

        self.reconciler = get_drift_reconciler()
        self.reconciler.clear_history()

    def test_most_restrictive_wins_open_vs_closed(self):
        """OPEN > CLOSED 우선순위."""
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        # When: OPEN vs CLOSED
        winner_state, result = reconciler.reconcile(
            service_name="test-service",
            l1_state="open",
            l2_state="closed",
        )

        # Then: OPEN이 승리 (더 제한적)
        assert winner_state == "open"
        assert result == DriftReconciliationResult.L1_WINS

    def test_half_open_xor_closed_resolves_by_timestamp(self):
        """476 D7: HALF_OPEN-XOR resolves by timestamp, not priority.

        Pre-476 this was Most Restrictive Wins (HALF_OPEN > CLOSED → L1
        wins by priority). Post-476, exactly-one-side-HALF_OPEN drifts
        resolve by timestamp because L2 owns OPEN→HALF_OPEN transitions
        atomically and the priority rule would otherwise reverse them.
        """
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        # When: HALF_OPEN vs CLOSED, no timestamps — fall back prefers
        # the HALF_OPEN side (deliberate permission window).
        winner_state, result = reconciler.reconcile(
            service_name="test-service",
            l1_state="half_open",
            l2_state="closed",
        )

        assert winner_state == "half_open"
        assert result == DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L1

    def test_half_open_xor_open_resolves_by_timestamp(self):
        """476 D7: OPEN vs HALF_OPEN XOR — newer wins, not priority.

        Pre-476 OPEN beat HALF_OPEN via L2_WINS. Post-476, the L2-Lua
        atomic OPEN→HALF_OPEN decision must be preserved when newer.
        """
        from datetime import datetime, timedelta

        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()
        now = datetime.now(UTC)

        # L2 just transitioned to HALF_OPEN via Lua; L1 is still stale OPEN.
        winner_state, result = reconciler.reconcile(
            service_name="test-service",
            l1_state="open",
            l2_state="half_open",
            l1_updated_at=now - timedelta(seconds=10),
            l2_updated_at=now,
        )

        assert winner_state == "half_open"
        assert result == DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L2

    def test_timestamp_tiebreaker_same_state(self):
        """같은 상태면 타임스탬프로 결정."""
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        now = datetime.now(UTC)
        l1_time = now - timedelta(seconds=5)
        l2_time = now  # L2가 더 최신

        # When: 같은 상태, L2가 더 최신
        winner_state, result = reconciler.reconcile(
            service_name="test-service",
            l1_state="open",
            l2_state="open",
            l1_updated_at=l1_time,
            l2_updated_at=l2_time,
        )

        # Then: 같은 상태면 드리프트 없음
        assert result == DriftReconciliationResult.NO_DRIFT

    def test_timestamp_tiebreaker_different_priority_same_level(self):
        """같은 우선순위 레벨에서 타임스탬프로 결정."""
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
            DriftReconciliationResult,
        )

        reconciler = DriftReconciler()

        now = datetime.now(UTC)
        l1_time = now
        l2_time = now - timedelta(seconds=5)

        winner_state, result = reconciler.reconcile(
            service_name="test-service",
            l1_state="closed",
            l2_state="closed",
            l1_updated_at=l1_time,
            l2_updated_at=l2_time,
        )

        # 같은 상태면 NO_DRIFT
        assert result == DriftReconciliationResult.NO_DRIFT

    def test_jitter_applied_to_reconciliation(self):
        """Jitter가 적용되어 지연됨."""
        from baldur.adapters.memory.circuit_breaker import DriftReconciler

        reconciler = DriftReconciler(
            min_jitter_seconds=0.0,
            max_jitter_seconds=0.01,  # 10ms
        )

        executed = []

        def do_reconcile():
            executed.append(True)

        # When: 스케줄 실행
        jitter = reconciler.schedule_reconciliation_sync(
            service_name="test-service",
            do_reconcile=do_reconcile,
        )

        # Then: 실행 완료 및 Jitter 값 반환
        assert len(executed) == 1
        assert 0.0 <= jitter <= 0.01

    def test_jitter_distribution(self):
        """Jitter가 균등 분포."""
        from baldur.adapters.memory.circuit_breaker import DriftReconciler

        reconciler = DriftReconciler(
            min_jitter_seconds=0.0,
            max_jitter_seconds=5.0,
        )

        jitters = [reconciler.get_jitter() for _ in range(1000)]

        # 평균이 약 2.5초 근처
        avg = sum(jitters) / len(jitters)
        assert 2.0 < avg < 3.0, f"Jitter 평균이 예상 범위 벗어남: {avg}"

        # 최소/최대가 범위 내
        assert min(jitters) >= 0.0
        assert max(jitters) <= 5.0

    def test_thundering_herd_prevention(self):
        """Thundering Herd 방지."""
        from baldur.adapters.memory.circuit_breaker import DriftReconciler

        # Given: 100개 Reconciler (각 Pod 시뮬레이션)
        jitters = []
        for _ in range(100):
            reconciler = DriftReconciler(
                min_jitter_seconds=0.0,
                max_jitter_seconds=5.0,
            )
            jitters.append(reconciler.get_jitter())

        # Then: 고유 값들이 많이 생성됨
        unique_jitters = {round(j, 2) for j in jitters}
        assert len(unique_jitters) > 50, (
            f"Jitter 분산 부족: {len(unique_jitters)} unique"
        )

        # 시간대가 분산됨 (통계적 변동성을 고려하여 threshold 완화)
        in_first_second = sum(1 for j in jitters if j < 1.0)
        in_last_second = sum(1 for j in jitters if j >= 4.0)
        # 0-5초 범위에서 균등 분포 시 각 1초당 약 20%인 20개 예상
        # 통계적 변동성 고려하여 5개 이상으로 완화 (기존 10에서 하향)
        assert in_first_second >= 5, (
            f"첫 1초에 충분한 Jitter 분산 없음: {in_first_second}"
        )
        assert in_last_second >= 5, (
            f"마지막 1초에 충분한 Jitter 분산 없음: {in_last_second}"
        )

    def test_reconciler_history_tracking(self):
        """복구 기록 추적."""
        from baldur.adapters.memory.circuit_breaker import DriftReconciler

        reconciler = DriftReconciler()
        reconciler.clear_history()

        # When: 여러 복구 실행
        reconciler.reconcile("svc-1", "open", "closed")
        reconciler.reconcile("svc-2", "closed", "open")
        reconciler.reconcile("svc-3", "half_open", "half_open")

        # Then: 기록 저장됨
        history = reconciler.get_history()
        assert len(history) == 3
        assert history[0].service_name == "svc-1"
        assert history[1].service_name == "svc-2"
        assert history[2].service_name == "svc-3"

    def test_reconciler_stats(self):
        """복구 통계."""
        from baldur.adapters.memory.circuit_breaker import (
            DriftReconciler,
        )

        reconciler = DriftReconciler()
        reconciler.clear_history()

        # When: 여러 복구 실행
        reconciler.reconcile("svc-1", "open", "closed")  # L1 wins
        reconciler.reconcile("svc-2", "closed", "open")  # L2 wins
        reconciler.reconcile("svc-3", "closed", "closed")  # No drift

        # Then: 통계 확인
        stats = reconciler.get_stats()
        assert stats["total_reconciliations"] == 3
        assert stats["by_result"]["l1_wins"] == 1
        assert stats["by_result"]["l2_wins"] == 1
        assert stats["by_result"]["no_drift"] == 1
        assert len(stats["affected_services"]) == 3
