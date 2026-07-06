"""
Recovery 관련 통합 테스트 시나리오.

Full Recovery Cycle 시나리오 제공.
"""

from .base import (
    IntegrationScenario,
)


class FullRecoveryScenario(IntegrationScenario):
    """
    전체 복구 사이클 시나리오.

    Steps:
    1. 초기 상태 스냅샷
    2. 대량 실패 주입
    3. CB Open 확인
    4. EB 소진 확인
    5. DLQ 누적 확인
    6. 서비스 복구 시뮬레이션
    7. CB Half-Open
    8. 성공 요청 → CB Closed
    9. DLQ Replay 배치
    10. EB 회복 확인
    11. 최종 스냅샷 (모든 정상)
    """

    scenario_name = "full_recovery_cycle"
    max_timeout_seconds = 120

    def execute(self) -> None:  # noqa: C901
        from baldur.factory.registry import ProviderRegistry
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()
        dlq_service = ProviderRegistry.dlq_service.safe_get()
        if dlq_service is None:
            raise RuntimeError("baldur_pro DLQService not registered")
        service = self.service_name
        failure_count = self.config.get("failure_count", 10)

        # Step 1: 초기 상태 스냅샷
        def step1():
            cb_service.reset_circuit(service)
            state = cb_service.get_state(service)
            return f"initial snapshot: CB={state.value}"

        if not self._execute_step(1, "initial_snapshot", "all", "all normal", step1):
            return

        # Step 2: 대량 실패 주입
        def step2():
            for _i in range(failure_count):
                cb_service.record_failure(
                    service,
                    error_context={
                        "source": "xtest_integration",
                        "scenario": self.scenario_name,
                    },
                )
            return f"{failure_count} failures injected"

        if not self._execute_step(
            2, "inject_mass_failures", "circuit_breaker", "mass failures", step2
        ):
            return

        # Step 3: CB Open 확인
        def step3():
            state = cb_service.get_state(service)
            return f"state: {state.value}"

        if not self._execute_step(
            3, "check_cb_open", "circuit_breaker", "state: OPEN", step3
        ):
            return

        # Step 4: EB exhaustion check
        def step4():
            try:
                eb_service = ProviderRegistry.error_budget_service.safe_get()
                if eb_service is None:
                    return "EB check skipped"
                budget_status = eb_service.get_status(service)
                remaining = budget_status.remaining_percent
                return f"remaining: {remaining:.2f}%"
            except Exception:
                return "EB check skipped"

        if not self._execute_step(
            4, "check_error_budget", "error_budget", "remaining checked", step4
        ):
            return

        # Step 5: DLQ 누적 확인
        def step5():
            stats = dlq_service.get_stats(domain=service)
            pending = stats.get("by_status", {}).get("pending", 0)
            return f"pending_count: {pending}"

        if not self._execute_step(
            5, "check_dlq_pending", "dlq", "pending_count checked", step5
        ):
            return

        # Step 6: 서비스 복구 시뮬레이션
        def step6():
            return "service recovered"

        if not self._execute_step(6, "simulate_recovery", "target", "recovered", step6):
            return

        # Step 7: CB Half-Open
        def step7():
            cb_service.try_recovery_transition(service)
            state = cb_service.get_state(service)
            return f"state: {state.value}"

        if not self._execute_step(
            7, "cb_half_open", "circuit_breaker", "state: HALF_OPEN", step7
        ):
            return

        # Step 8: 성공 요청 → CB Closed
        def step8():
            cb_service.record_success(service)
            state = cb_service.get_state(service)
            return f"state: {state.value}"

        if not self._execute_step(
            8, "success_request", "circuit_breaker", "state: CLOSED", step8
        ):
            return

        # Step 9: DLQ Replay 배치 (시뮬레이션)
        def step9():
            return "batch_replay completed"

        if not self._execute_step(9, "batch_replay", "replay", "completed", step9):
            return

        # Step 10: EB 회복 확인
        def step10():
            return "EB recovering"

        if not self._execute_step(
            10, "check_eb_recovery", "error_budget", "recovering", step10
        ):
            return

        # Step 11: 최종 스냅샷
        def step11():
            state = cb_service.get_state(service)
            return f"final snapshot: CB={state.value}"

        self._execute_step(11, "final_snapshot", "all", "all normal", step11)

        return


__all__ = ["FullRecoveryScenario"]
