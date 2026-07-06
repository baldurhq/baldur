"""
479 D2: process-wide L2 warmup hook tests.

Pins the ``LayeredRepositoryBase._ensure_l2_warmup_once`` contract:

- First redis-l2 LayeredRepository construction submits N parallel
  ``try_acquire_half_open_slot`` calls (N = ``executor_max_workers``) plus
  one ``delete_state`` cleanup.
- Subsequent constructions short-circuit on the ``_warmup_done`` ClassVar.
- Redis-down at construction time does NOT raise (fail-open).
- Non-redis adapter types skip warmup entirely.
- ``_reset_warmup_state()`` re-arms the gate for tests.

This file overrides the package-level autouse ``_suppress_background_drift_reconciliation``
fixture locally — the override keeps drift suppression but skips the warmup
patch so the warmup code path is actually exercised. Each test starts with a
clean ``_warmup_done`` slate.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

# Small executor pool keeps the test fast (each thread spawn is ~50-200ms in
# CPython under GIL contention; default 16 workers × multiple tests would be
# slow). 4 still exercises the Barrier + multi-thread path meaningfully.
# Pre-installed on _executor directly so settings/env state from prior tests
# in the same xdist worker cannot leak into the executor sizing.
_TEST_MAX_WORKERS = 4


@pytest.fixture(autouse=True)
def _suppress_background_drift_reconciliation():
    """Local override of the package-level autouse fixture (479 D4).

    Differs from the package-level fixture:
    - Skips the ``_ensure_l2_warmup_once`` patch so warmup actually runs.
    - Calls ``LayeredRepositoryBase._reset_warmup_state()`` so each test
      starts with a clean once-per-process gate.
    - Pre-installs a small ThreadPoolExecutor on
      ``LayeredRepositoryBase._executor``. ``_get_executor()`` returns this
      pre-built pool via the unlocked fast-path (``cls._executor is not None``)
      so the test doesn't depend on env-var propagation through the cached
      ``L2StorageSettings`` singleton — settings state can leak between
      xdist files within a worker, but ClassVar pre-install is robust.

    Drift suppression is still applied — drift reconciliation must remain
    neutralized for test isolation.
    """
    from baldur.adapters.memory.layered_repository.base import LayeredRepositoryBase

    _shutdown_executor()
    LayeredRepositoryBase._reset_warmup_state()
    LayeredRepositoryBase._executor = ThreadPoolExecutor(
        max_workers=_TEST_MAX_WORKERS, thread_name_prefix="prewarm_test"
    )

    with patch(
        "baldur.adapters.memory.layered_repository.drift_operations."
        "DriftOperationsMixin._schedule_drift_reconciliation",
        return_value=None,
    ):
        yield

    _shutdown_executor()
    LayeredRepositoryBase._reset_warmup_state()


def _shutdown_executor():
    from baldur.adapters.memory.layered_repository.base import LayeredRepositoryBase

    executor = LayeredRepositoryBase._executor
    if executor is not None:
        executor.shutdown(wait=True)
        LayeredRepositoryBase._executor = None


def _make_l2_mock():
    """Build a CircuitBreakerStateRepository mock suitable for warmup."""
    from baldur.interfaces.repositories import CircuitBreakerStateRepository

    mock = MagicMock(spec=CircuitBreakerStateRepository)
    mock.get_all_states.return_value = []
    mock.try_acquire_half_open_slot.return_value = (False, "closed", "closed")
    mock.delete_state.return_value = True
    return mock


class TestPrewarmBehavior:
    """Pin the warmup contract."""

    def test_first_redis_construction_warms_executor_pool_and_cleans_up(self):
        """First redis-l2 ctor → N×try_acquire + 1×delete_state on sentinel."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.adapters.memory.layered_repository.base import (
            _WARMUP_SENTINEL_SERVICE_NAME,
            LayeredRepositoryBase,
        )

        # Force the executor pool size at the test boundary — the fixture's
        # pre-install can lose to autouse fixture-ordering races in some xdist
        # configurations. Reading _max_workers at this exact moment pins the
        # contract: "warmup submits exactly N tasks where N is the pool size
        # observable from the test boundary".
        _shutdown_executor()
        LayeredRepositoryBase._reset_warmup_state()
        LayeredRepositoryBase._executor = ThreadPoolExecutor(
            max_workers=_TEST_MAX_WORKERS, thread_name_prefix="prewarm_test_inline"
        )
        expected_n = _TEST_MAX_WORKERS

        l2_mock = _make_l2_mock()

        LayeredCircuitBreakerStateRepository(l2_repo=l2_mock, adapter_type="redis")

        try_acquire_calls = l2_mock.try_acquire_half_open_slot.call_args_list
        assert len(try_acquire_calls) == expected_n
        for call in try_acquire_calls:
            assert call.args == (_WARMUP_SENTINEL_SERVICE_NAME, 1, 1)

        # Defense-in-depth cleanup once.
        l2_mock.delete_state.assert_called_once_with(_WARMUP_SENTINEL_SERVICE_NAME)

    def test_subsequent_constructions_short_circuit(self):
        """Once _warmup_done=True, second redis-l2 ctor adds no warmup calls."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.adapters.memory.layered_repository.base import (
            LayeredRepositoryBase,
        )

        _shutdown_executor()
        LayeredRepositoryBase._reset_warmup_state()
        LayeredRepositoryBase._executor = ThreadPoolExecutor(
            max_workers=_TEST_MAX_WORKERS, thread_name_prefix="prewarm_test_inline"
        )
        expected_n = _TEST_MAX_WORKERS

        first_l2 = _make_l2_mock()
        LayeredCircuitBreakerStateRepository(l2_repo=first_l2, adapter_type="redis")
        assert first_l2.try_acquire_half_open_slot.call_count == expected_n

        # Second construction: fresh mock, should record ZERO warmup calls.
        second_l2 = _make_l2_mock()
        LayeredCircuitBreakerStateRepository(l2_repo=second_l2, adapter_type="redis")

        assert second_l2.try_acquire_half_open_slot.call_count == 0
        assert second_l2.delete_state.call_count == 0

    def test_redis_down_at_construction_does_not_raise(self):
        """try_acquire raising must not propagate out of __init__."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        l2_mock = _make_l2_mock()
        l2_mock.try_acquire_half_open_slot.side_effect = ConnectionError("redis down")

        # Must not raise: warmup wraps catastrophic errors and the constructor
        # path remains fail-open per CROSS_SERVICE_STANDARDS.
        repo = LayeredCircuitBreakerStateRepository(
            l2_repo=l2_mock, adapter_type="redis"
        )
        assert repo is not None

        # Even on failure, the once-per-process gate flips True so we don't
        # retry from the constructor.
        from baldur.adapters.memory.layered_repository.base import (
            LayeredRepositoryBase,
        )

        assert LayeredRepositoryBase._warmup_done is True

    @pytest.mark.parametrize("adapter_type", ["database", "django", "unknown", ""])
    def test_non_redis_adapter_skips_warmup(self, adapter_type):
        """Cat 6.4 gates only redis; non-redis adapters skip warmup."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        l2_mock = _make_l2_mock()
        LayeredCircuitBreakerStateRepository(l2_repo=l2_mock, adapter_type=adapter_type)

        assert l2_mock.try_acquire_half_open_slot.call_count == 0
        assert l2_mock.delete_state.call_count == 0

    def test_reset_warmup_state_re_arms_gate(self):
        """_reset_warmup_state() lets a subsequent ctor re-trigger warmup."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.adapters.memory.layered_repository.base import (
            LayeredRepositoryBase,
        )

        _shutdown_executor()
        LayeredRepositoryBase._reset_warmup_state()
        LayeredRepositoryBase._executor = ThreadPoolExecutor(
            max_workers=_TEST_MAX_WORKERS, thread_name_prefix="prewarm_test_inline"
        )
        expected_n = _TEST_MAX_WORKERS

        first_l2 = _make_l2_mock()
        LayeredCircuitBreakerStateRepository(l2_repo=first_l2, adapter_type="redis")
        assert LayeredRepositoryBase._warmup_done is True
        assert first_l2.try_acquire_half_open_slot.call_count == expected_n

        LayeredRepositoryBase._reset_warmup_state()
        assert LayeredRepositoryBase._warmup_done is False

        second_l2 = _make_l2_mock()
        LayeredCircuitBreakerStateRepository(l2_repo=second_l2, adapter_type="redis")
        assert second_l2.try_acquire_half_open_slot.call_count == expected_n
