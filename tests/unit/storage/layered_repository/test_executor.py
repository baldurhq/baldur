"""478 D3 — LayeredRepositoryBase ThreadPoolExecutor sizing.

Pre-D3: hardcoded `max_workers=4`. Under N-instance × M-thread burst the
class-level pool saturated and `try_acquire_half_open_slot` futures timed
out, breaking the cluster-wide HALF_OPEN cap.

Post-D3: pool size driven by `BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS` env
(default 16, ge=1, le=64). Startup-only — `ThreadPoolExecutor` is fixed-
size at construction. Tests use env-var fixtures + reset_l2_storage_settings
+ reset_layered_repository_executor to recreate the pool.
"""

from __future__ import annotations


class TestExecutorMaxWorkers:
    """LayeredRepositoryBase._executor pool size — env-driven (478 D3)."""

    def test_default_max_workers_is_16(self, monkeypatch):
        """Env unset → default 16."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.adapters.memory.layered_repository import (
            reset_layered_repository_executor,
        )
        from baldur.adapters.memory.layered_repository.base import (
            LayeredRepositoryBase,
        )
        from baldur.settings.l2_storage import reset_l2_storage_settings

        monkeypatch.delenv("BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS", raising=False)
        reset_l2_storage_settings()
        reset_layered_repository_executor()

        try:
            LayeredCircuitBreakerStateRepository(l2_repo=None)
            executor = LayeredRepositoryBase._get_executor()
            assert executor._max_workers == 16
        finally:
            reset_layered_repository_executor()
            reset_l2_storage_settings()

    def test_env_override_max_workers(self, monkeypatch):
        """Env set → override applied."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.adapters.memory.layered_repository import (
            reset_layered_repository_executor,
        )
        from baldur.adapters.memory.layered_repository.base import (
            LayeredRepositoryBase,
        )
        from baldur.settings.l2_storage import reset_l2_storage_settings

        monkeypatch.setenv("BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS", "32")
        reset_l2_storage_settings()
        reset_layered_repository_executor()

        try:
            LayeredCircuitBreakerStateRepository(l2_repo=None)
            executor = LayeredRepositoryBase._get_executor()
            assert executor._max_workers == 32
        finally:
            monkeypatch.delenv("BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS", raising=False)
            reset_layered_repository_executor()
            reset_l2_storage_settings()
