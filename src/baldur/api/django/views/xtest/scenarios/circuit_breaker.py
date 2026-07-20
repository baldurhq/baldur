"""
Circuit Breaker-related integration test scenarios.

Provides the "DLQ store after CB Open" scenario.
"""

from .base import (
    IntegrationScenario,
)


class CBOpenDLQScenario(IntegrationScenario):
    """
    Scenario: store to DLQ after the Circuit Breaker opens.

    Steps:
    1. Verify CB Closed
    2. Inject failures
    3. Verify CB Open
    4. Send a request -> verify it is blocked
    5. Store to DLQ
    6. Verify the DLQ entry details
    """

    scenario_name = "cb_open_dlq_flow"

    def execute(self) -> None:  # noqa: C901
        from baldur.factory.registry import ProviderRegistry
        from baldur.services.circuit_breaker import (
            CircuitState,
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()
        dlq_service = ProviderRegistry.dlq_service.safe_get()
        if dlq_service is None:
            raise RuntimeError("baldur_pro DLQService not registered")
        service = self.service_name
        failure_count = self.config.get("failure_count", 5)

        # Step 1: verify CB Closed
        def step1():
            state = cb_service.get_state(service)
            if state != CircuitState.CLOSED:
                # Reset for the test
                cb_service.reset_circuit(service)
                state = cb_service.get_state(service)
            return f"state: {state.value}"

        if not self._execute_step(
            1, "check_cb_state", "circuit_breaker", "state: CLOSED", step1
        ):
            return

        # Step 2: inject failures
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
            2, "inject_failures", "circuit_breaker", f"{failure_count} failures", step2
        ):
            return

        # Step 3: verify CB Open
        def step3():
            state = cb_service.get_state(service)
            return f"state: {state.value}"

        if not self._execute_step(
            3, "check_cb_open", "circuit_breaker", "state: OPEN", step3
        ):
            return

        # Step 4: send a request -> verify it is blocked
        def step4():
            result = cb_service.should_allow_request(service)
            if result.allowed:
                return "request allowed (unexpected)"
            return f"CircuitOpenException: {result.reason}"

        if not self._execute_step(
            4, "send_request", "circuit_breaker", "CircuitOpenException", step4
        ):
            return

        # Step 5: store to DLQ
        dlq_entry_id = None

        def step5():
            nonlocal dlq_entry_id
            # impl doc 486 D2 G4 — xtest scenario needs real ``dlq_id``.
            result = dlq_service.store_failure(
                domain=service,
                failure_type="CIRCUIT_OPEN",
                entity_type="xtest_integration",
                entity_id=self.scenario_id,
                error_message="Circuit breaker is open",
                metadata={
                    "source": "xtest_integration",
                    "scenario": self.scenario_name,
                    "xtest_mode": True,
                },
                mode="sync",
            )
            if result.success:
                dlq_entry_id = result.dlq_id
                return f"DLQ entry created: {dlq_entry_id}"
            return f"DLQ store failed: {result.error}"

        if not self._execute_step(5, "store_to_dlq", "dlq", "DLQ entry created", step5):
            return

        # Step 6: verify the DLQ entry details
        def step6():
            if not dlq_entry_id:
                raise ValueError("No DLQ entry ID from previous step")
            entry = dlq_service.get_entry(dlq_entry_id)
            if entry:
                return f"error_type: {entry.get('failure_type', 'unknown')}"
            return "entry not found"

        self._execute_step(
            6, "verify_dlq_entry", "dlq", "error_type: CIRCUIT_OPEN", step6
        )

        return


__all__ = ["CBOpenDLQScenario"]
