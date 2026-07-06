"""
LayeredRepository 기본 동작 테스트.
"""

import time
from unittest.mock import MagicMock


class TestLayeredRepositoryBasic:
    """LayeredRepository 기본 동작 테스트."""

    def test_l1_always_returns_immediately(self):
        """L1 get_or_create returns immediately (no blocking L2 round-trip).

        The first call in a process resolves lazy imports (settings module for
        the 656 D4 flag read) + constructs the settings singleton inside the
        call, so a warm-up call is issued first to exclude one-time
        import/construction cost; the timed measurement then reflects the
        steady-state L1-return latency this test asserts on.
        """
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        repo = LayeredCircuitBreakerStateRepository(l2_repo=None)

        # Warm-up: pay one-time lazy-import + settings construction here, not in
        # the timed region (deterministic > wall-clock-margin, per §6.5.6).
        repo.get_or_create("warmup-service")

        start = time.time()
        repo.get_or_create("test-service")
        elapsed = time.time() - start

        assert elapsed < 0.01, f"L1 lookup too slow: {elapsed * 1000:.2f}ms"

    def test_l2_sync_is_async(self):
        """L2 동기화는 비동기."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = []

        def slow_update(*args, **kwargs):
            time.sleep(0.5)
            return True

        mock_l2.update_state.side_effect = slow_update
        mock_l2.get_or_create.side_effect = slow_update

        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)

        start = time.time()
        repo.record_failure("test-service")
        elapsed = time.time() - start

        # L1 업데이트는 빠르게 완료되어야 함
        assert elapsed < 0.2, f"L1 업데이트가 너무 느림: {elapsed * 1000:.2f}ms"

    def test_get_storage_info(self):
        """저장소 정보 조회."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        repo = LayeredCircuitBreakerStateRepository(l2_repo=None)
        info = repo.get_storage_info()

        assert "l1_type" in info
        assert "l2_enabled" in info
        assert info["l1_type"] is not None

    def test_get_storage_info_with_l2(self):
        """L2가 있을 때 저장소 정보 조회."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = []

        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)
        info = repo.get_storage_info()

        assert info["l2_enabled"] is True
        assert "l2_healthy" in info
        assert "metrics" in info

    def test_get_or_create_does_not_sync_to_l2(self):
        """478 D2: get_or_create는 L2로 sync하지 않음 (Lua-atomic state clobber 방지).

        Pre-D2: get_or_create scheduled _sync_to_l2_async(name, l1_state).
        Under burst load this clobbered the L2 state (e.g. OPEN→HALF_OPEN
        Lua transition) with the pre-transition L1 snapshot. D2 drops the
        sync — state mirroring belongs to the explicit write callers.
        Falsifiability mirror of pre-G15 clobber evidence in
        scenario-results/6.4-multi-worker-cb-half-open-consistency.md.
        """
        import time

        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = []

        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)

        # Reset call counts established during __init__ hydration so the
        # assertion only covers the get_or_create call below.
        mock_l2.reset_mock()
        mock_l2.get_all_states.return_value = []

        repo.get_or_create("svc")

        # Allow a brief window for any (incorrectly-scheduled) async sync to
        # land before asserting non-occurrence.
        time.sleep(0.1)

        mock_l2.get_or_create.assert_not_called()
        mock_l2.update_state.assert_not_called()
