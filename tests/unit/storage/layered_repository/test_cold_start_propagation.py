"""656 D4 — flag-gated cold-start L1-miss L2 read in get_or_create.

With ``cluster_state_propagation_enabled`` on, a freshly booted /
never-hydrated worker's first ``get_or_create`` for a service that is OPEN
in L2 performs a bounded one-shot authoritative L2 read (reusing
``get_by_service_name``'s timeout-bounded executor fallback) so it rejects
traffic the cluster already cut off — closing the #478 hydration-failure
staleness window 479 left open.

With the flag at its default (off) the admission read path is byte-identical
to today: no L2 touch, no executor submit. This gate read is also the OSS
behavioral consumer of the flag (G32 claim-wiring proof).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)

_SETTINGS_FN = "baldur.settings.circuit_breaker.get_circuit_breaker_settings"


def _flag(enabled: bool):
    return SimpleNamespace(cluster_state_propagation_enabled=enabled)


@pytest.fixture
def l2_mock():
    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )

    mock = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
    mock.get_all_states.return_value = []
    return mock


@pytest.fixture
def repo(l2_mock):
    from baldur.adapters.memory.circuit_breaker import (
        LayeredCircuitBreakerStateRepository,
    )

    return LayeredCircuitBreakerStateRepository(
        l2_repo=l2_mock,
        adapter_type="redis",
    )


# =============================================================================
# Behavior — flag x L1 x L2 cold-start matrix (656 D4)
# =============================================================================


class TestColdStartPropagationBehavior:
    """``flag`` x ``L1 (miss/hit)`` x ``L2 (open/absent)`` (656 D4)."""

    def test_flag_off_l1_miss_is_byte_identical_empty_create(self, repo, l2_mock):
        # Given: flag off, empty L1.
        with (
            patch(_SETTINGS_FN, return_value=_flag(False)),
            patch.object(
                repo, "get_by_service_name", wraps=repo.get_by_service_name
            ) as spy,
        ):
            state = repo.get_or_create("svc")

        # Then: no L2 read at all (the layered get_by_service_name is never
        # entered), result is a fresh CLOSED entry.
        spy.assert_not_called()
        l2_mock.get_by_service_name.assert_not_called()
        assert state.state == CircuitBreakerStateEnum.CLOSED.value

    def test_flag_on_l1_miss_l2_open_returns_open(self, repo, l2_mock):
        # Given: flag on, empty L1, L2 reports OPEN.
        l2_mock.get_by_service_name.return_value = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            failure_count=5,
        )

        with patch(_SETTINGS_FN, return_value=_flag(True)):
            state = repo.get_or_create("svc")

        # Then: the cold-start read returns the cluster-authoritative OPEN.
        assert state.state == CircuitBreakerStateEnum.OPEN.value
        l2_mock.get_by_service_name.assert_called_once_with("svc")

    def test_flag_on_l1_miss_l2_absent_creates_empty_closed(self, repo, l2_mock):
        # Given: flag on, empty L1, L2 has no entry.
        l2_mock.get_by_service_name.return_value = None

        with patch(_SETTINGS_FN, return_value=_flag(True)):
            state = repo.get_or_create("svc")

        # Then: falls through to L1 empty-create (CLOSED).
        assert state.state == CircuitBreakerStateEnum.CLOSED.value
        l2_mock.get_by_service_name.assert_called_once_with("svc")

    def test_flag_on_l1_hit_does_not_read_l2(self, repo, l2_mock):
        # Given: flag on, L1 already holds the service (a hit).
        repo._l1.get_or_create("svc")
        repo._l1.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
        )

        with patch(_SETTINGS_FN, return_value=_flag(True)):
            state = repo.get_or_create("svc")

        # Then: get_by_service_name hits L1 first; L2 is never touched.
        assert state.state == CircuitBreakerStateEnum.OPEN.value
        l2_mock.get_by_service_name.assert_not_called()
