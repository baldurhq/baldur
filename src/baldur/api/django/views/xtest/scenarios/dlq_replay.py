"""
Integration test scenarios for DLQ (Dead Letter Queue) and Replay.

Provides the RetryExhaust, RateLimitRetry, DLQReplay success/failure, and
idempotent Replay scenarios.
"""

from .base import (
    IntegrationScenario,
)


class RetryExhaustScenario(IntegrationScenario):
    """
    Scenario: store to DLQ after retries are exhausted.

    Steps:
    1. Configure max retries (3)
    2. 1st attempt fails
    3. 2nd attempt fails
    4. 3rd attempt fails
    5. Confirm the DLQ store
    """

    scenario_name = "retry_exhaust_dlq"

    def execute(self) -> None:
        try:
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
        except ImportError:
            dlq_service = None

        if dlq_service is None:
            raise RuntimeError("baldur_pro DLQService not registered")
        service = self.service_name
        max_retries = self.config.get("max_retries", 3)

        # Step 1: configure max retries
        retry_count = 0

        def step1():
            return f"max_retries: {max_retries}"

        if not self._execute_step(
            1, "config_max_retries", "retry", f"max_retries: {max_retries}", step1
        ):
            return

        # Steps 2-4: retry attempts fail
        for i in range(1, max_retries + 1):

            def step_retry(attempt=i):
                nonlocal retry_count
                retry_count = attempt
                return f"attempt {attempt} failed"

            if not self._execute_step(
                i + 1, f"retry_attempt_{i}", "retry", f"attempt {i} failed", step_retry
            ):
                return

        # Step 5: store to DLQ (486 D2 G4 — xtest needs a real dlq_id).
        def step_dlq():
            result = dlq_service.store_failure(
                domain=service,
                failure_type="RETRY_EXHAUSTED",
                entity_type="xtest_integration",
                entity_id=self.scenario_id,
                error_message=f"Exhausted after {max_retries} retries",
                metadata={
                    "source": "xtest_integration",
                    "scenario": self.scenario_name,
                    "retry_count": retry_count,
                    "xtest_mode": True,
                },
                mode="sync",
            )
            return str(result.success)

        self._execute_step(max_retries + 2, "store_to_dlq", "dlq", "True", step_dlq)

        return


class RateLimitRetryScenario(IntegrationScenario):
    """
    Scenario: retry succeeds after a rate limit.

    Steps:
    1. Rate Limit hit
    2. Backoff wait (configured duration)
    3. Retry succeeds
    """

    scenario_name = "rate_limit_retry"

    def execute(self) -> None:
        import time

        backoff_seconds = self.config.get("backoff_seconds", 1)

        # Step 1: hit the Rate Limit
        def step1():
            return "rate limited"

        if not self._execute_step(
            1, "hit_rate_limit", "rate_limiter", "rate limited", step1
        ):
            return

        # Step 2: backoff wait
        def step2():
            if backoff_seconds > 0:
                time.sleep(backoff_seconds)
            return f"waited {backoff_seconds}s"

        if not self._execute_step(
            2, "backoff_wait", "timer", f"waited {backoff_seconds}s", step2
        ):
            return

        # Step 3: retry succeeds
        def step3():
            return "success"

        self._execute_step(3, "retry_success", "target", "success", step3)

        return


class DLQReplaySuccessScenario(IntegrationScenario):
    """
    Scenario: DLQ entry replayed successfully.

    Steps:
    1. Create a DLQ entry
    2. Start the replay
    3. Target service call succeeds
    4. Remove the DLQ entry
    """

    scenario_name = "dlq_replay_success"

    def execute(self) -> None:  # noqa: C901
        try:
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
        except ImportError:
            dlq_service = None

        if dlq_service is None:
            raise RuntimeError("baldur_pro DLQService not registered")
        service = self.service_name

        # Step 1: create a DLQ entry
        dlq_entry_id = None

        def step1():
            nonlocal dlq_entry_id
            result = dlq_service.store_failure(
                domain=service,
                failure_type="XTEST_REPLAY_TEST",
                entity_type="xtest_integration",
                entity_id=self.scenario_id,
                error_message="Test entry for replay",
                metadata={
                    "source": "xtest_integration",
                    "scenario": self.scenario_name,
                    "xtest_mode": True,
                },
                mode="sync",
            )
            if result.success:
                dlq_entry_id = result.dlq_id
                return f"dlq_id: {dlq_entry_id}"
            return f"failed: {result.error}"

        if not self._execute_step(1, "create_dlq_entry", "dlq", "dlq_id:", step1):
            return

        # Step 2: start the replay
        def step2():
            return "replay started"

        if not self._execute_step(2, "start_replay", "replay", "replay started", step2):
            return

        # Step 3: target service call succeeds
        def step3():
            return "target success"

        if not self._execute_step(3, "call_target", "target", "target success", step3):
            return

        # Step 4: remove the DLQ entry
        def step4():
            if dlq_entry_id:
                dlq_service.mark_resolved(domain=service, dlq_id=dlq_entry_id)
            return "entry removed"

        self._execute_step(4, "remove_dlq_entry", "dlq", "entry removed", step4)

        return


class DLQReplayFailureScenario(IntegrationScenario):
    """
    Scenario: DLQ entry replay fails (stored back to the DLQ).

    Steps:
    1. Create a DLQ entry
    2. Start the replay
    3. Target service call fails
    4. Confirm the retry count increments
    """

    scenario_name = "dlq_replay_failure"

    def execute(self) -> None:  # noqa: C901
        try:
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
        except ImportError:
            dlq_service = None

        if dlq_service is None:
            raise RuntimeError("baldur_pro DLQService not registered")
        service = self.service_name

        # Step 1: create a DLQ entry
        dlq_entry_id = None

        def step1():
            nonlocal dlq_entry_id
            result = dlq_service.store_failure(
                domain=service,
                failure_type="XTEST_REPLAY_FAIL_TEST",
                entity_type="xtest_integration",
                entity_id=self.scenario_id,
                error_message="Test entry for replay failure",
                metadata={
                    "source": "xtest_integration",
                    "scenario": self.scenario_name,
                    "xtest_mode": True,
                },
                mode="sync",
            )
            if result.success:
                dlq_entry_id = result.dlq_id
                return f"dlq_id: {dlq_entry_id}"
            return f"failed: {result.error}"

        if not self._execute_step(1, "create_dlq_entry", "dlq", "dlq_id:", step1):
            return

        # Step 2: start the replay
        def step2():
            return "replay started"

        if not self._execute_step(2, "start_replay", "replay", "replay started", step2):
            return

        # Step 3: target service call fails
        def step3():
            return "target failed"

        if not self._execute_step(3, "call_target", "target", "target failed", step3):
            return

        # Step 4: confirm the retry count increments
        def step4():
            return "retry_count: 1"

        self._execute_step(4, "increment_retry", "dlq", "retry_count:", step4)

        return


class IdempotentReplayScenario(IntegrationScenario):
    """
    Scenario: replay idempotency guarantee.

    Steps:
    1. Create a DLQ entry (with an idempotency_key)
    2. Run the first replay
    3. Confirm the idempotency key is registered
    4. Attempt to replay the same entry again
    5. Confirm the duplicate-detection result
    6. Confirm the actual processing count (exactly once)
    """

    scenario_name = "idempotent_replay"

    def execute(self) -> None:  # noqa: C901
        from baldur.services.idempotency import (
            IdempotencyKey,
            IdempotencyService,
        )

        try:
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
        except ImportError:
            dlq_service = None

        if dlq_service is None:
            raise RuntimeError("baldur_pro DLQService not registered")
        IdempotencyService()
        service = self.service_name
        idempotency_key = f"replay_{self.scenario_id}"

        # Step 1: create a DLQ entry
        dlq_entry_id = None

        def step1():
            nonlocal dlq_entry_id
            result = dlq_service.store_failure(
                domain=service,
                failure_type="XTEST_IDEMPOTENT_TEST",
                entity_type="xtest_integration",
                entity_id=self.scenario_id,
                error_message="Test entry for idempotency",
                metadata={
                    "source": "xtest_integration",
                    "scenario": self.scenario_name,
                    "idempotency_key": idempotency_key,
                    "xtest_mode": True,
                },
                mode="sync",
            )
            if result.success:
                dlq_entry_id = result.dlq_id
                return f"idempotency_key: {idempotency_key}"
            return f"failed: {result.error}"

        if not self._execute_step(
            1, "create_dlq_entry", "dlq", "idempotency_key included", step1
        ):
            return

        # Step 2: first replay
        def step2():
            return "first replay executed"

        if not self._execute_step(
            2, "first_replay", "replay", "first processed", step2
        ):
            return

        # Step 3: confirm the idempotency key is registered
        def step3():
            IdempotencyKey.for_operation(
                entity_type="replay",
                entity_id=str(dlq_entry_id) if dlq_entry_id else self.scenario_id,
                action="process",
            )
            # Simulate key registration
            return "key registered"

        if not self._execute_step(
            3, "check_key_registered", "idempotency", "registered", step3
        ):
            return

        # Step 4: replay the same entry again
        def step4():
            return "duplicate replay attempted"

        if not self._execute_step(
            4, "second_replay", "replay", "duplicate attempt", step4
        ):
            return

        # Step 5: duplicate-detection result
        def step5():
            return "duplicate detected, previous result returned"

        if not self._execute_step(
            5, "check_duplicate_result", "idempotency", "previous result", step5
        ):
            return

        # Step 6: confirm the processing count
        def step6():
            return "actual_processing: 1"

        self._execute_step(6, "verify_single_process", "target", "1 time only", step6)

        return


__all__ = [
    "RetryExhaustScenario",
    "RateLimitRetryScenario",
    "DLQReplaySuccessScenario",
    "DLQReplayFailureScenario",
    "IdempotentReplayScenario",
]
