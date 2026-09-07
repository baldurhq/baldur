"""The layered repository's cluster-verdict read: L2, or nothing.

L1 is this worker's cache. It is hydrated wholesale once and then per service
on first touch, so rows another worker changed drift -- and a fleet-wide
verdict answered from one worker's drifted rows is exactly the defect
``get_cluster_states()`` exists to close. So L1 is deliberately *not* a
fallback here, unlike every other read on this repository.

The second deliberate omission is the L2 health bookkeeping. Quarantine
counters belong to the request path; a periodic probe failing on its own
cadence must not push this process's admission decisions onto the L1-only
lane. ``_l2_healthy`` is still read, so the probe never dials a store this
process has already given up on.

Verification techniques applied:
- Exception/edge cases: the four ways L2 cannot answer, each with its reason
- Negative assertion: no L1 row is ever substituted, and the health counters
  are unchanged after a raising probe
- Dependency interaction: the delegate is called exactly once, through the
  shared executor
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.repositories import CircuitBreakerStateData
from baldur.services.circuit_breaker.exceptions import (
    CircuitBreakerStateUnavailableError,
)

OPERATION = "get_cluster_states"


class _InlineExecutor:
    """Executor stub that runs submitted callables synchronously inline.

    The production path submits to the shared pool and blocks on the future,
    so an inline runner is behaviour-preserving for everything except the
    timeout -- which the timeout test drives through the future itself.
    """

    def __init__(self):
        self.submit_count = 0

    def submit(self, fn, *args, **kwargs):
        self.submit_count += 1
        future = MagicMock(spec=Future)
        try:
            value = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised from result()
            future.result.side_effect = exc
        else:
            future.result.return_value = value
        return future


def _l2_rows() -> list[CircuitBreakerStateData]:
    return [
        CircuitBreakerStateData(service_name="payment-api", state="open"),
        CircuitBreakerStateData(service_name="catalog-api", state="closed"),
    ]


@pytest.fixture
def repo(mock_l2_repo):
    """A layered repository over a mock L2, with its counters zeroed."""
    from baldur.adapters.memory.circuit_breaker import (
        LayeredCircuitBreakerStateRepository,
    )

    r = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2_repo, adapter_type="redis")
    mock_l2_repo.reset_mock()
    mock_l2_repo.get_cluster_states.return_value = _l2_rows()
    r._l2_healthy = True
    r._l2_consecutive_failures = 0
    return r


class TestLayeredClusterStates:
    """Every answer comes from L2, or the caller is told it could not."""

    def test_cluster_states_answers_from_l2(self, repo, mock_l2_repo):
        """The delegate's rows come back untouched, through the shared pool."""
        inline = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=inline):
            states = repo.get_cluster_states()

        assert {s.service_name for s in states} == {"payment-api", "catalog-api"}
        mock_l2_repo.get_cluster_states.assert_called_once_with()
        assert inline.submit_count == 1

    def test_cluster_states_raises_when_no_l2_is_configured(self):
        """An L1-only deployment has no cluster to answer for."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        repo = LayeredCircuitBreakerStateRepository(l2_repo=None)

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            repo.get_cluster_states()

        assert excinfo.value.reason == "l2_absent"
        assert excinfo.value.operation == OPERATION

    def test_cluster_states_raises_while_l2_is_quarantined(self, repo, mock_l2_repo):
        """A store this process has given up on is not dialed again here."""
        repo._l2_healthy = False

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            repo.get_cluster_states()

        assert excinfo.value.reason == "l2_quarantined"
        mock_l2_repo.get_cluster_states.assert_not_called()

    def test_cluster_states_raises_on_the_l2_timeout(self, repo):
        """A slow L2 is reported as a timeout, not as an empty fleet."""
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        future = MagicMock(spec=Future)
        future.result.side_effect = FuturesTimeoutError()
        executor = MagicMock(spec=ThreadPoolExecutor)
        executor.submit.return_value = future

        with patch.object(repo, "_get_executor", return_value=executor):
            with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
                repo.get_cluster_states()

        assert excinfo.value.reason == "l2_timeout"

    def test_cluster_states_wraps_a_raising_delegate(self, repo, mock_l2_repo):
        """Any other L2 failure keeps its message inside the cluster error."""
        mock_l2_repo.get_cluster_states.side_effect = RuntimeError("connection reset")
        inline = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=inline):
            with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
                repo.get_cluster_states()

        assert excinfo.value.reason.startswith("l2_error:")
        assert "connection reset" in excinfo.value.reason

    def test_cluster_states_propagates_the_delegates_own_cluster_error(
        self, repo, mock_l2_repo
    ):
        """A delegate that already answered in this vocabulary keeps its reason.

        Re-wrapping would bury the Redis adapter's ``degraded_during_scan``
        under a generic ``l2_error``, and that reason is what a consumer reads.
        """
        mock_l2_repo.get_cluster_states.side_effect = (
            CircuitBreakerStateUnavailableError(OPERATION, "degraded_during_scan")
        )
        inline = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=inline):
            with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
                repo.get_cluster_states()

        assert excinfo.value.reason == "degraded_during_scan"

    def test_cluster_states_never_substitutes_an_l1_row(self, repo, mock_l2_repo):
        """L1 holds a row; the failing read still refuses to answer from it."""
        repo._l1.get_or_create("payment-api")
        mock_l2_repo.get_cluster_states.side_effect = RuntimeError("connection reset")
        inline = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=inline):
            with pytest.raises(CircuitBreakerStateUnavailableError):
                repo.get_cluster_states()

        # L1 could have answered -- the point is that it was not asked to.
        assert [row.service_name for row in repo._l1.get_all_states()] == [
            "payment-api"
        ]

    def test_a_failing_probe_leaves_the_l2_health_bookkeeping_alone(
        self, repo, mock_l2_repo
    ):
        """The periodic probe must not quarantine the request path.

        Its cadence is its own; counting its failures toward the quarantine
        threshold would push this process's admission decisions onto the
        L1-only lane for a store the request path is still reaching.
        """
        mock_l2_repo.get_cluster_states.side_effect = RuntimeError("connection reset")
        inline = _InlineExecutor()

        with patch.object(repo, "_get_executor", return_value=inline):
            for _ in range(5):
                with pytest.raises(CircuitBreakerStateUnavailableError):
                    repo.get_cluster_states()

        assert repo._l2_healthy is True
        assert repo._l2_consecutive_failures == 0
