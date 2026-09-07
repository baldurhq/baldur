"""The layered cluster read against the real executor and the real error handler.

The unit tests drive this method over an inline executor stub, which is
faithful for the delegation but not for the two behaviours that only exist
because the call is submitted to a shared pool:

- the **timeout** is the future's, measured on the adapter's own budget while
  a real worker thread is still blocked in L2;
- the **quarantine** flag the read consults is written by the *request* path's
  error handler, in another thread, from real consecutive failures.

Both are state dependencies across threads, so a mock cannot reproduce them
faithfully: a stubbed future returns whatever the test hands it, and a hand-set
``_l2_healthy`` proves only that the branch exists.

No infrastructure -- a real ``ThreadPoolExecutor``, a real error handler and a
deliberately slow in-process L2. No ``requires_*`` marker.
"""

from __future__ import annotations

import threading
import time

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
    LayeredCircuitBreakerStateRepository,
)
from baldur.services.circuit_breaker.exceptions import (
    CircuitBreakerStateUnavailableError,
)

# 479 D1: the redis profile's steady-state L2 budget.
REDIS_L2_TIMEOUT_SECONDS = 1.0
QUARANTINE_THRESHOLD = 3


class _FailingL2(InMemoryCircuitBreakerStateRepository):
    """A real L2 whose every write fails, counting the failures it caused.

    The mirror lane runs on the shared pool, so the error handler that flips
    the quarantine flag runs in a worker thread. The counter is what lets the
    test wait for that to have actually happened instead of sleeping.
    """

    def __init__(self) -> None:
        super().__init__()
        self._failures = 0
        self._condition = threading.Condition()

    def get_or_create(self, service_name: str, **kwargs):
        with self._condition:
            self._failures += 1
            self._condition.notify_all()
        raise RuntimeError("connection reset")

    def wait_for_failures(self, count: int, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._failures >= count, timeout=timeout
            )


class _SlowL2(InMemoryCircuitBreakerStateRepository):
    """A real L2 whose cluster read blocks until the test releases it.

    Subclassing the in-memory adapter rather than mocking it keeps every other
    call on the construction path real -- the initial load included, which is
    what makes the repository reach a usable state before the probe runs.
    """

    def __init__(self, delay_seconds: float) -> None:
        super().__init__()
        self._delay = delay_seconds
        self.released = threading.Event()
        self.entered = threading.Event()

    def get_cluster_states(self):
        self.entered.set()
        time.sleep(self._delay)
        self.released.set()
        return super().get_cluster_states()


@pytest.fixture(autouse=True)
def _executor_reset():
    """The pool is class-level, so each test starts and ends with a clean one."""
    from baldur.adapters.memory.layered_repository import (
        reset_layered_repository_executor,
    )

    reset_layered_repository_executor()
    yield
    reset_layered_repository_executor()


def _layered(l2) -> LayeredCircuitBreakerStateRepository:
    return LayeredCircuitBreakerStateRepository(l2_repo=l2, adapter_type="redis")


class TestLayeredClusterStatesOverL2:
    """The override, exercised through the pool it actually submits to."""

    def test_the_read_returns_the_l2_rows_through_the_shared_pool(self):
        """A real submit, a real worker thread, the delegate's own rows."""
        l2 = InMemoryCircuitBreakerStateRepository()
        l2.get_or_create("payment-api")
        l2.get_or_create("catalog-api")
        repo = _layered(l2)

        states = repo.get_cluster_states()

        assert {s.service_name for s in states} == {"payment-api", "catalog-api"}

    def test_an_l2_slower_than_the_budget_raises_a_timeout(self):
        """The adapter's own timeout, measured while L2 is still blocked.

        The worker thread is still inside ``get_cluster_states`` when the
        future gives up -- the property a stubbed future cannot demonstrate.
        """
        l2 = _SlowL2(REDIS_L2_TIMEOUT_SECONDS + 1.0)
        repo = _layered(l2)

        started = time.monotonic()
        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            repo.get_cluster_states()
        elapsed = time.monotonic() - started

        assert excinfo.value.reason == "l2_timeout"
        assert l2.entered.is_set()
        assert l2.released.is_set() is False
        assert elapsed < REDIS_L2_TIMEOUT_SECONDS + 1.0
        l2.released.wait(timeout=5)

    def test_a_timed_out_read_leaves_the_request_path_unquarantined(self):
        """The probe's own failures never push admission onto the L1-only lane."""
        l2 = _SlowL2(REDIS_L2_TIMEOUT_SECONDS + 0.5)
        repo = _layered(l2)

        with pytest.raises(CircuitBreakerStateUnavailableError):
            repo.get_cluster_states()

        assert repo._l2_healthy is True
        assert repo._l2_consecutive_failures == 0
        l2.released.wait(timeout=5)

    def test_a_quarantine_written_by_the_request_path_stops_the_read(self):
        """The flag the read consults is the request path's, set by real failures.

        The request path's mirror runs on the same pool; its error handler is
        what flips ``_l2_healthy`` after the threshold. Only then does the
        cluster read decline to dial.
        """
        l2 = _FailingL2()
        repo = _layered(l2)

        # One service per mirror: the lane coalesces same-service submits, so
        # three writes to one name would not be three failures.
        for index in range(QUARANTINE_THRESHOLD):
            name = f"svc-{index}"
            repo._l1.get_or_create(name)
            repo._sync_to_l2_async(name)

        assert l2.wait_for_failures(QUARANTINE_THRESHOLD, timeout=10) is True
        assert repo._l2_healthy is False

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            repo.get_cluster_states()

        assert excinfo.value.reason == "l2_quarantined"

    def test_concurrent_reads_share_the_pool_without_starving_each_other(self):
        """Several probes in flight at once still each get their own answer.

        The pool is process-wide and shared with the mirror lane, so a read
        that held a worker for its whole budget would be visible here as a
        timeout on a sibling call.
        """
        l2 = InMemoryCircuitBreakerStateRepository()
        l2.get_or_create("payment-api")
        repo = _layered(l2)
        results: list[object] = []
        errors: list[BaseException] = []

        def _probe() -> None:
            try:
                results.append(repo.get_cluster_states())
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=_probe) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == []
        assert len(results) == 8
        assert all(
            [row.service_name for row in rows] == ["payment-api"] for rows in results
        )
