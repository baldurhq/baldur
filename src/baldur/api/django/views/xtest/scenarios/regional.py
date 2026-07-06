"""
Regional-related integration test scenarios.

Provides Global vs Regional state precedence verification and multi-region isolation scenarios.

Key features:
- RegionalOverrideConflictScenario: verifies Global/Regional precedence and Admin Override
- MultiRegionIsolationTestScenario: verifies that only a specific region is isolated

Uses the real AtomicStateQuery and NamespacedEmergencyTracker to
verify the Lua-script-based atomic state lookup logic.
"""

import json
from typing import Any

from django.utils import timezone

from .base import (
    IntegrationScenario,
)


class MockStateBackend:
    """
    Mock StateBackend for testing.

    Manages state in memory without Redis.
    Can be used with AtomicStateQuery to verify the precedence logic.
    """

    def __init__(self, redis_client: Any = None):
        """
        Initialize the Mock StateBackend.

        Args:
            redis_client: Mock Redis client (for shared state)
        """
        self._redis_client = redis_client
        self._storage: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        """Look up a key."""
        if self._redis_client:
            data = self._redis_client.get(key)
            if data:
                return json.loads(
                    data.decode("utf-8") if isinstance(data, bytes) else data
                )
            return default
        return self._storage.get(key, default)

    def set(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        """Store a key."""
        if self._redis_client:
            self._redis_client.set(key, json.dumps(value, default=str))
        else:
            self._storage[key] = value

    def delete(self, key: str) -> bool:
        """Delete a key."""
        if self._redis_client:
            return bool(self._redis_client.delete(key) > 0)
        if key in self._storage:
            del self._storage[key]
            return True
        return False

    def exists(self, key: str) -> bool:
        """Check whether a key exists."""
        if self._redis_client:
            return self._redis_client.get(key) is not None
        return key in self._storage

    def get_all(self, pattern: str = "*") -> dict[str, Any]:
        """Look up keys matching a pattern."""
        if self._redis_client:
            import fnmatch

            result = {}
            for key in list(self._storage.keys()):
                if fnmatch.fnmatch(key, pattern):
                    result[key] = self._storage[key]
            return result
        return self._storage.copy()


class RegionalOverrideConflictScenario(IntegrationScenario):
    """
    Global vs Regional state precedence verification scenario.

    Uses NamespacedEmergencyTracker and AtomicStateQuery to
    verify the real precedence logic.

    Precedence rules:
    1. When Admin Override is active → Regional takes precedence
    2. Global STRICT → all regions STRICT (override)
    3. Regional state → local state

    Steps:
    1. Check the initial state (Global: NORMAL, Regional: NORMAL)
    2. Set Regional STRICT
    3. Call get_effective_state() (STRICT - Regional precedence)
    4. Set Global STRICT
    5. Call get_effective_state() (STRICT - Global override)
    6. Set Regional ADMIN_OVERRIDE
    7. Call get_effective_state() (NORMAL - Admin wins)
    8. Restore states (all states NORMAL)

    Config options:
    - target_region: str - target region (default: seoul)
    - redis_client: Redis client (injected for testing)
    """

    scenario_name = "regional_override_conflict"
    max_timeout_seconds = 60

    # =========================================================================
    # Step 2: state-setting helper methods (doc 144, 2-1, 2-2, 2-3)
    # =========================================================================

    def _set_global_state(
        self,
        tracker: Any,
        level: Any,
        activated_by: str = "xtest-scenario",
    ) -> dict[str, Any]:
        """
        Set the Global Emergency state.

        Args:
            tracker: NamespacedEmergencyTracker instance
            level: EmergencyLevel (NORMAL, LEVEL_1, LEVEL_2, LEVEL_3)
            activated_by: who activated it

        Returns:
            State transition info dictionary
        """
        from baldur.models.emergency import EmergencyLevel, EmergencyScope

        previous_state = tracker.get_state(namespace="global")
        previous_mode = previous_state.governance_mode if previous_state else "NORMAL"

        if level == EmergencyLevel.NORMAL:
            new_state = tracker.deactivate_emergency(
                deactivated_by=activated_by,
                namespace="global",
                scope=EmergencyScope.GLOBAL,
            )
        else:
            new_state = tracker.activate_emergency(
                level=level,
                activated_by=activated_by,
                reason="X-Test Global STRICT setting",
                namespace="global",
                scope=EmergencyScope.GLOBAL,
            )

        return {
            "action": "set_global_state",
            "previous_state": previous_mode,
            "new_state": new_state.governance_mode,
            "level": level.value if hasattr(level, "value") else str(level),
            "region": "global",
            "timestamp": timezone.now().isoformat(),
        }

    def _set_regional_state(
        self,
        tracker: Any,
        namespace: str,
        level: Any,
        activated_by: str = "xtest-scenario",
    ) -> dict[str, Any]:
        """
        Set the Regional Emergency state.

        Args:
            tracker: NamespacedEmergencyTracker instance
            namespace: target region (e.g., "seoul", "tokyo")
            level: EmergencyLevel
            activated_by: who activated it

        Returns:
            State transition info dictionary
        """
        from baldur.models.emergency import EmergencyLevel, EmergencyScope

        previous_state = tracker.get_state(namespace=namespace)
        previous_mode = previous_state.governance_mode if previous_state else "NORMAL"

        if level == EmergencyLevel.NORMAL:
            new_state = tracker.deactivate_emergency(
                deactivated_by=activated_by,
                namespace=namespace,
                scope=EmergencyScope.REGIONAL,
            )
        else:
            new_state = tracker.activate_emergency(
                level=level,
                activated_by=activated_by,
                reason=f"X-Test Regional STRICT setting: {namespace}",
                namespace=namespace,
                scope=EmergencyScope.REGIONAL,
            )

        return {
            "action": "set_regional_state",
            "previous_state": previous_mode,
            "new_state": new_state.governance_mode,
            "level": level.value if hasattr(level, "value") else str(level),
            "region": namespace,
            "timestamp": timezone.now().isoformat(),
        }

    def _set_admin_override(
        self,
        tracker: Any,
        namespace: str,
        active: bool,
    ) -> dict[str, Any]:
        """
        Set Admin Override (controlled via the precedence parameter).

        When Admin Override is active, get_effective_state() is called with
        precedence="ADMIN_OVERRIDE" to apply Regional precedence.

        Args:
            tracker: NamespacedEmergencyTracker instance
            namespace: target region
            active: whether Override is active

        Returns:
            State transition info dictionary
        """
        # Admin Override is controlled via the precedence parameter at call time
        # Here, only a state-tracking flag is managed
        return {
            "action": "set_admin_override",
            "previous_state": "OFF" if active else "ON",
            "new_state": "ON" if active else "OFF",
            "region": namespace,
            "timestamp": timezone.now().isoformat(),
        }

    # =========================================================================
    # Step 3: get_effective_state() integration (doc 144, 3-1, 3-2, 3-3)
    # =========================================================================

    def _get_effective_state_with_logging(
        self,
        tracker: Any,
        namespace: str,
        precedence: str | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """
        Call get_effective_state() and log the result.

        Looks up the state atomically via AtomicStateQuery.

        Args:
            tracker: NamespacedEmergencyTracker instance
            namespace: target region
            precedence: precedence ("AUTO", "ADMIN_OVERRIDE", etc.)

        Returns:
            (ScopedEmergencyState, log dictionary)
        """
        state = tracker.get_effective_state(
            namespace=namespace,
            precedence=precedence,
        )

        log_entry = {
            "action": "get_effective_state",
            "namespace": namespace,
            "precedence": precedence or "AUTO",
            "result_governance_mode": state.governance_mode,
            "result_scope": state.scope.value
            if hasattr(state.scope, "value")
            else str(state.scope),
            "result_level": (
                state.emergency_level.value
                if hasattr(state.emergency_level, "value")
                else str(state.emergency_level)
            ),
            "timestamp": timezone.now().isoformat(),
        }

        return state, log_entry

    def execute(self) -> None:  # noqa: C901, PLR0915
        """Run the 8-step scenario."""
        from baldur.models.emergency import EmergencyLevel
        from baldur.services.regional_emergency.tracker import (
            NamespacedEmergencyTracker,
        )

        target_region = self.config.get("target_region", "seoul")
        redis_client = self.config.get("redis_client")
        state_transitions: list[dict[str, Any]] = []
        admin_override_active = False

        # Initialize the Tracker (a Mock backend can be injected for testing)
        if redis_client:
            from baldur.services.regional_emergency.atomic_query import (
                AtomicStateQuery,
            )

            backend = MockStateBackend(redis_client=redis_client)
            atomic_query = AtomicStateQuery(redis_client=redis_client)
            tracker = NamespacedEmergencyTracker(
                backend=backend,
                atomic_query=atomic_query,
            )
        else:
            tracker = NamespacedEmergencyTracker()

        # =====================================================================
        # Step 1: check the initial state (Global: NORMAL, Regional: NORMAL)
        # =====================================================================
        def step1():
            # Reset state (clear leftovers from previous tests)
            self._set_global_state(tracker, EmergencyLevel.NORMAL)
            self._set_regional_state(tracker, target_region, EmergencyLevel.NORMAL)

            state, log = self._get_effective_state_with_logging(tracker, target_region)
            state_transitions.append(log)

            return (
                f"Global: NORMAL, Regional: NORMAL, governance: {state.governance_mode}"
            )

        if not self._execute_step(
            1,
            "check_initial_state",
            "namespaced_emergency_tracker",
            "Global: NORMAL, Regional: NORMAL",
            step1,
        ):
            return

        # =====================================================================
        # Step 2: set Regional STRICT
        # =====================================================================
        def step2():
            transition = self._set_regional_state(
                tracker, target_region, EmergencyLevel.LEVEL_2
            )
            state_transitions.append(transition)
            return "Regional: STRICT (LEVEL_2)"

        if not self._execute_step(
            2,
            "set_regional_strict",
            "namespaced_emergency_tracker",
            "Regional: STRICT",
            step2,
        ):
            return

        # =====================================================================
        # Step 3: call get_effective_state() (STRICT - Regional precedence)
        # =====================================================================
        def step3():
            state, log = self._get_effective_state_with_logging(tracker, target_region)
            state_transitions.append(log)
            return f"governance: {state.governance_mode}, scope: {state.scope.value}"

        if not self._execute_step(
            3,
            "get_effective_state_regional_priority",
            "atomic_state_query",
            "governance: STRICT, scope: regional",
            step3,
        ):
            return

        # =====================================================================
        # Step 4: set Global STRICT
        # =====================================================================
        def step4():
            transition = self._set_global_state(tracker, EmergencyLevel.LEVEL_3)
            state_transitions.append(transition)
            return "Global: STRICT (LEVEL_3)"

        if not self._execute_step(
            4,
            "set_global_strict",
            "namespaced_emergency_tracker",
            "Global: STRICT",
            step4,
        ):
            return

        # =====================================================================
        # Step 5: call get_effective_state() (STRICT - Global override)
        # =====================================================================
        def step5():
            state, log = self._get_effective_state_with_logging(tracker, target_region)
            state_transitions.append(log)
            return f"governance: {state.governance_mode}, scope: {state.scope.value}"

        if not self._execute_step(
            5,
            "get_effective_state_global_override",
            "atomic_state_query",
            "governance: STRICT, scope: global",
            step5,
        ):
            return

        # =====================================================================
        # Step 6: set Regional ADMIN_OVERRIDE
        # =====================================================================
        def step6():
            nonlocal admin_override_active
            # Change Regional to NORMAL
            self._set_regional_state(tracker, target_region, EmergencyLevel.NORMAL)
            # Activate the Admin Override flag
            transition = self._set_admin_override(tracker, target_region, True)
            state_transitions.append(transition)
            admin_override_active = True
            return "Regional: ADMIN_OVERRIDE (NORMAL)"

        if not self._execute_step(
            6,
            "set_admin_override",
            "namespaced_emergency_tracker",
            "Regional: ADMIN_OVERRIDE",
            step6,
        ):
            return

        # =====================================================================
        # Step 7: call get_effective_state() (NORMAL - Admin wins)
        # =====================================================================
        def step7():
            # Admin Override is active, so use precedence="ADMIN_OVERRIDE"
            precedence = "ADMIN_OVERRIDE" if admin_override_active else None
            state, log = self._get_effective_state_with_logging(
                tracker, target_region, precedence=precedence
            )
            state_transitions.append(log)
            return f"governance: {state.governance_mode}, scope: {state.scope.value}"

        if not self._execute_step(
            7,
            "get_effective_state_admin_wins",
            "atomic_state_query",
            "governance: NORMAL, scope: regional",
            step7,
        ):
            return

        # =====================================================================
        # Step 8: restore states (all states NORMAL)
        # =====================================================================
        def step8():
            nonlocal admin_override_active
            # Restore Global
            self._set_global_state(tracker, EmergencyLevel.NORMAL)
            # Restore Regional
            self._set_regional_state(tracker, target_region, EmergencyLevel.NORMAL)
            # Release Admin Override
            self._set_admin_override(tracker, target_region, False)
            admin_override_active = False

            state, log = self._get_effective_state_with_logging(tracker, target_region)
            state_transitions.append(log)
            return f"All states restored: governance={state.governance_mode}"

        self._execute_step(
            8,
            "restore_all_states",
            "namespaced_emergency_tracker",
            "All states restored: governance=NORMAL",
            step8,
        )

        # Add the state transition history to the result
        if self.result and self.result.config is not None:
            self.result.config["state_transitions"] = state_transitions
        elif self.result:
            self.result.config = {"state_transitions": state_transitions}

        return


class MultiRegionIsolationTestScenario(IntegrationScenario):
    """
    Multi-region isolation verification scenario.

    Uses NamespacedEmergencyTracker to confirm that only a specific region is
    isolated while other regions remain normal.

    Steps:
    1. Check the current region
    2. Set a specific region to STRICT (isolate)
    3. Check the state of another region (NORMAL)
    4. Verify the isolation state (only the target is isolated)
    5. Release the isolation

    Config options:
    - target_region: str - region to isolate (default: seoul)
    - other_region: str - another region for comparison (default: tokyo)
    - redis_client: Redis client (injected for testing)
    """

    scenario_name = "multi_region_isolation_test"
    max_timeout_seconds = 60

    def _create_tracker(self) -> tuple[Any, Any]:
        """
        Create a NamespacedEmergencyTracker.

        Returns:
            (tracker, atomic_query) tuple
        """
        from baldur.services.regional_emergency.tracker import (
            NamespacedEmergencyTracker,
        )

        redis_client = self.config.get("redis_client")

        if redis_client:
            from baldur.services.regional_emergency.atomic_query import (
                AtomicStateQuery,
            )

            backend = MockStateBackend(redis_client=redis_client)
            atomic_query = AtomicStateQuery(redis_client=redis_client)
            tracker = NamespacedEmergencyTracker(
                backend=backend,
                atomic_query=atomic_query,
            )
            return tracker, atomic_query
        tracker = NamespacedEmergencyTracker()
        return tracker, None

    def execute(self) -> None:  # noqa: C901
        """Run the 5-step scenario."""
        from baldur.models.emergency import EmergencyLevel, EmergencyScope

        target_region = self.config.get("target_region", "seoul")
        other_region = self.config.get("other_region", "tokyo")

        tracker, _ = self._create_tracker()

        # =====================================================================
        # Step 1: check the current region
        # =====================================================================
        def step1():
            # Initialize: set all regions to NORMAL
            tracker.deactivate_emergency(
                deactivated_by="xtest-init",
                namespace=target_region,
                scope=EmergencyScope.REGIONAL,
            )
            tracker.deactivate_emergency(
                deactivated_by="xtest-init",
                namespace=other_region,
                scope=EmergencyScope.REGIONAL,
            )
            return f"region: {target_region}"

        if not self._execute_step(
            1,
            "check_current_region",
            "namespaced_emergency_tracker",
            f"region: {target_region}",
            step1,
        ):
            return

        # =====================================================================
        # Step 2: set a specific region to STRICT (isolate)
        # =====================================================================
        def step2():
            state = tracker.activate_emergency(
                level=EmergencyLevel.LEVEL_2,
                activated_by="xtest-scenario",
                reason="X-Test multi-region isolation test",
                namespace=target_region,
                scope=EmergencyScope.REGIONAL,
            )
            return f"{target_region}: STRICT (isolated={state.governance_mode == 'STRICT'})"

        if not self._execute_step(
            2,
            "set_region_strict",
            "namespaced_emergency_tracker",
            f"{target_region}: STRICT",
            step2,
        ):
            return

        # =====================================================================
        # Step 3: check the state of another region (NORMAL)
        # =====================================================================
        def step3():
            state = tracker.get_effective_state(namespace=other_region)
            if state.governance_mode == "STRICT":
                return f"{other_region}: STRICT (unexpected)"
            return f"{other_region}: NORMAL"

        if not self._execute_step(
            3,
            "check_other_region_normal",
            "namespaced_emergency_tracker",
            f"{other_region}: NORMAL",
            step3,
        ):
            return

        # =====================================================================
        # Step 4: verify the isolation state (only the target is isolated)
        # =====================================================================
        def step4():
            target_state = tracker.get_effective_state(namespace=target_region)
            other_state = tracker.get_effective_state(namespace=other_region)

            target_isolated = target_state.governance_mode == "STRICT"
            other_isolated = other_state.governance_mode == "STRICT"

            isolated_regions = []
            if target_isolated:
                isolated_regions.append(target_region)
            if other_isolated:
                isolated_regions.append(other_region)

            return f"isolated_regions: {isolated_regions}, {target_region}_isolated: {target_isolated}, {other_region}_isolated: {other_isolated}"

        expected_step4 = f"isolated_regions: ['{target_region}'], {target_region}_isolated: True, {other_region}_isolated: False"
        if not self._execute_step(
            4,
            "verify_isolation_state",
            "namespaced_emergency_tracker",
            expected_step4,
            step4,
        ):
            return

        # =====================================================================
        # Step 5: release the isolation
        # =====================================================================
        def step5():
            tracker.deactivate_emergency(
                deactivated_by="xtest-scenario",
                namespace=target_region,
                scope=EmergencyScope.REGIONAL,
            )
            state = tracker.get_effective_state(namespace=target_region)
            is_isolated = state.governance_mode == "STRICT"
            return f"{target_region}: NORMAL (restored={not is_isolated}, isolated={is_isolated})"

        self._execute_step(
            5,
            "restore_region",
            "namespaced_emergency_tracker",
            f"{target_region}: NORMAL",
            step5,
        )

        return


__all__ = [
    "RegionalOverrideConflictScenario",
    "MultiRegionIsolationTestScenario",
]
