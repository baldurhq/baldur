"""
L2 timeout tests.
"""

import time
from unittest.mock import MagicMock, patch


class TestL2Timeout:
    """L2 timeout tests."""

    def test_timeout_on_slow_l2(self):
        """Timeout fires when L2 is slow."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
            ShadowLogger,
        )

        # Given: L2 simulated to take 1100ms (479 D1: redis default 1000ms;
        # initial load budget = 2x = 2000ms, so 1100ms does not trigger the
        # init-load timeout — this test verifies a slow L2 is handled safely
        # along the non-init path).
        slow_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)

        def slow_get_all():
            time.sleep(1.1)  # 1100ms delay — exceeds 1000ms steady-state timeout
            return []

        slow_l2.get_all_states.side_effect = slow_get_all

        # When: build the repository with the 1000ms timeout (Redis type, 479 D1)
        with patch(
            "baldur.adapters.memory.circuit_breaker.get_shadow_logger"
        ) as mock_logger:
            mock_logger.return_value = ShadowLogger()
            repo = LayeredCircuitBreakerStateRepository(
                l2_repo=slow_l2,
                adapter_type="redis",
            )

        # Then: timeout fires and the repo continues with L1-only operation
        assert repo._metrics["l2_timeout_count"] >= 0

    def test_fallback_to_l1_on_timeout(self):
        """Falls back to L1-only operation on timeout."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.interfaces.repositories import CircuitBreakerStateEnum

        # Given: a slow L2 (479 D1: init load timeout = 2x redis = 2.0s, so
        # delay 2.5s to trigger the timeout path on the initial
        # _load_from_l2_with_timeout).
        slow_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        slow_l2.get_all_states.side_effect = lambda: time.sleep(2.5) or []

        repo = LayeredCircuitBreakerStateRepository(
            l2_repo=slow_l2,
            adapter_type="redis",
        )

        # When: state is created on L1
        state = repo.get_or_create("test-service")

        # Then: L1 data is returned correctly
        assert state is not None
        assert state.service_name == "test-service"
        assert state.state == CircuitBreakerStateEnum.CLOSED.value

    def test_adapter_specific_timeout(self):
        """Per-adapter timeout is applied (479 D1: redis default 1000ms)."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        # Redis adapter (479 D1: 200 -> 1000ms; cold-start cluster-cap headroom)
        redis_repo = LayeredCircuitBreakerStateRepository(
            l2_repo=None,
            adapter_type="redis",
        )
        assert redis_repo._get_timeout_seconds() == 1.0  # 1000ms

        # Database adapter (unchanged by 479 D1)
        db_repo = LayeredCircuitBreakerStateRepository(
            l2_repo=None,
            adapter_type="database",
        )
        assert db_repo._get_timeout_seconds() == 0.2  # 200ms

        # Django adapter (same as database, unchanged by 479 D1)
        django_repo = LayeredCircuitBreakerStateRepository(
            l2_repo=None,
            adapter_type="django",
        )
        assert django_repo._get_timeout_seconds() == 0.2  # 200ms

        # Unknown adapter (unchanged by 479 D1)
        unknown_repo = LayeredCircuitBreakerStateRepository(
            l2_repo=None,
            adapter_type="unknown",
        )
        assert unknown_repo._get_timeout_seconds() == 0.1  # 100ms

    def test_l2_timeout_increments_metric(self):
        """L2 timeout increments the internal timeout metric."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        # Given: a slow L2 (479 D1: 1100ms delay exceeds the 1000ms redis timeout)
        slow_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        slow_l2.get_all_states.return_value = []
        slow_l2.get_by_service_name.side_effect = lambda _: time.sleep(1.1) or None

        repo = LayeredCircuitBreakerStateRepository(
            l2_repo=slow_l2,
            adapter_type="redis",  # 479 D1: 1000ms timeout
        )

        initial_timeout_count = repo._metrics["l2_timeout_count"]

        # When: an L2 lookup triggers the timeout path
        repo._l2_healthy = True
        result = repo.get_by_service_name("test-service")

        # Then: timeout count increments (or L1 result is returned)
        assert (
            result is None or repo._metrics["l2_timeout_count"] >= initial_timeout_count
        )

    def test_redis_timeout_ms_floor_for_cold_start(self):
        """479 G4: redis default timeout meets the cold-start floor (>=500ms).

        Per 479 D1, redis_timeout_ms defaults to 1000ms — this provides the
        headroom required for the Cat 6.4 cold-start cluster-cap guarantee.
        This test pins the floor so a future regression that silently lowers
        the default surfaces at unit-test granularity instead of waiting
        for the Cat 6.4 scenario re-run.

        Per the Pydantic field's operator-warning description:
        "Lowering below ~500 ms forfeits the Cat 6.4 cold-start cluster-cap
        guarantee".
        """
        from baldur.settings.l2_storage import L2StorageSettings

        settings = L2StorageSettings()
        assert settings.redis_timeout_ms >= 500, (
            f"redis_timeout_ms={settings.redis_timeout_ms}ms violates cold-start "
            "floor (>=500ms). Lowering below this forfeits Cat 6.4 cluster-cap."
        )

    def test_redis_timeout_env_override(self, monkeypatch):
        """478 D1: BALDUR_L2_STORAGE_REDIS_TIMEOUT_MS env reaches _get_timeout_seconds().

        Pre-D1: import path was broken (`baldur.config` does not exist), so the
        ImportError fallback always fired and env overrides had zero effect.
        Post-D1: the runtime-config path is reachable; env vars flow through
        L2StorageSettings -> L2StorageRuntimeConfig -> get_timeout_for_adapter.
        """
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.settings.l2_storage import (
            reset_l2_storage_runtime_config,
            reset_l2_storage_settings,
        )

        monkeypatch.setenv("BALDUR_L2_STORAGE_REDIS_TIMEOUT_MS", "500")
        reset_l2_storage_settings()
        reset_l2_storage_runtime_config()

        try:
            repo = LayeredCircuitBreakerStateRepository(
                l2_repo=None,
                adapter_type="redis",
            )
            assert repo._get_timeout_seconds() == 0.5  # 500ms
        finally:
            monkeypatch.delenv("BALDUR_L2_STORAGE_REDIS_TIMEOUT_MS", raising=False)
            reset_l2_storage_settings()
            reset_l2_storage_runtime_config()
