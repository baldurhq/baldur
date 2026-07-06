"""
Layered Repository test package fixtures.

Shared fixtures used by every test in this package.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _suppress_background_drift_reconciliation():
    """Suppress background drift reconciliation + L2 prewarm for test isolation.

    LayeredCircuitBreakerStateRepository._schedule_drift_reconciliation()
    submits _reconcile_all_drift() -> mark_all_as_synced() to a
    ThreadPoolExecutor. The async run pollutes the ShadowLogger singleton
    state and causes intermittent failures in subsequent tests.

    Executor drain alone is insufficient: _reconcile_all_drift() internally
    spawns new tasks via _get_executor().submit(), so a fresh executor can
    appear after shutdown. Patching _schedule_drift_reconciliation itself
    to a no-op blocks the root cause.

    479 D4: _ensure_l2_warmup_once fires a 16-thread Barrier submit on the
    first redis-l2 construction, which adds extra
    try_acquire_half_open_slot / delete_state calls to the mock l2_repo and
    breaks pre-existing patterns like
    mock.assert_called_once_with("svc", ...). Apply the same pattern as
    drift suppression — patch prewarm to a no-op by default. test_prewarm.py
    locally overrides this fixture to opt out.
    """
    _shutdown_executor()
    with (
        patch(
            "baldur.adapters.memory.layered_repository.drift_operations."
            "DriftOperationsMixin._schedule_drift_reconciliation",
            return_value=None,
        ),
        patch(
            "baldur.adapters.memory.layered_repository.base."
            "LayeredRepositoryBase._ensure_l2_warmup_once",
            return_value=None,
        ),
    ):
        yield
    _shutdown_executor()


def _shutdown_executor():
    try:
        from baldur.adapters.memory.layered_repository.base import (
            LayeredRepositoryBase,
        )

        executor = LayeredRepositoryBase._executor
        if executor is not None:
            executor.shutdown(wait=True)
            LayeredRepositoryBase._executor = None
    except ImportError:
        pass


@pytest.fixture
def mock_l2_repo():
    """Mock L2 repository."""
    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )

    mock = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
    mock.get_all_states.return_value = []
    return mock


@pytest.fixture
def shadow_logger():
    """Shadow Logger fixture."""
    from baldur.adapters.memory.circuit_breaker import get_shadow_logger

    logger = get_shadow_logger()
    logger.clear()
    yield logger
    logger.clear()


@pytest.fixture
def drift_reconciler():
    """Drift Reconciler fixture."""
    from baldur.adapters.memory.circuit_breaker import get_drift_reconciler

    reconciler = get_drift_reconciler()
    reconciler.clear_history()
    yield reconciler
    reconciler.clear_history()
