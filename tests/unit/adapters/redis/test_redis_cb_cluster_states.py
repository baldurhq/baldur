"""``get_cluster_states()`` answers from Redis, or it raises.

The Redis adapter's ordinary reads are deliberately forgiving: ``hgetall``
switches the backend to degraded and answers from this process's memory,
``get_open_states()`` returns whatever a bounded walk found. Both are correct
for a per-service question and both are wrong for a *fleet-wide* one -- a
substituted or partial view reads as "few circuits are OPEN", which is the
direction that hides a system-wide collapse from the detector watching for it.

So this method has no fallback at all. Each test below drives one substitution
the sibling read path would have made silently and asserts it raises instead,
carrying the machine-readable reason a consumer picks its safe direction from.

Verification techniques applied:
- Exception/edge cases: every failure mode of the walk, each with its reason
- Side effects: the degrade counter sampled either side of the scan, including
  the case where the backend recovers inside the loop
- Negative assertion: no memory-fallback row ever reaches the result, and the
  forgiving sibling read keeps its own semantics
"""

from __future__ import annotations

import fnmatch
from typing import Any

import pytest

from baldur.adapters.redis.circuit_breaker import RedisCircuitBreakerStateRepository
from baldur.services.circuit_breaker.exceptions import (
    CircuitBreakerStateUnavailableError,
)

STATIC_PREFIX = "baldur:"


class FakeRedisClient:
    """SCAN over the physical keyspace, with a scriptable cursor walk."""

    def __init__(self, store: dict[str, dict[str, str]]) -> None:
        self._store = store
        self.pages: list[tuple[int, list[str]]] | None = None
        self.scan_calls = 0
        self.scan_error: Exception | None = None

    def scan(self, cursor: int = 0, match: str = "*", count: int = 100):
        self.scan_calls += 1
        if self.scan_error is not None:
            raise self.scan_error
        if self.pages is not None:
            return self.pages[min(self.scan_calls - 1, len(self.pages) - 1)]
        return 0, [k for k in self._store if fnmatch.fnmatch(k, match)]


class FakeBackend:
    """A ResilientStorageBackend stand-in whose keys go through the real seam.

    ``hgetall`` takes a *component* key and applies the prefix internally,
    exactly as the real backend does; the raw client returns *physical* keys,
    exactly as Redis does.
    """

    def __init__(
        self,
        *,
        is_degraded: bool = False,
        connectable: bool = True,
        has_reached_redis: bool = True,
    ) -> None:
        self.store: dict[str, dict[str, str]] = {}
        self.is_degraded = is_degraded
        self.has_reached_redis = has_reached_redis
        self.degrade_count = 0
        self._connectable = connectable
        self._redis = object()
        self.config = type("Config", (), {"key_prefix": STATIC_PREFIX})()
        self.raw_redis_client = FakeRedisClient(self.store)
        # The process-local rows the forgiving read falls back to.
        self._memory: dict[str, dict[str, str]] = {}
        # Whether the backend's own recovery flips the mode back before the
        # walk ends -- the case a post-walk ``is_degraded`` check would miss.
        self.recovers_in_loop = False

    def _get_full_key(self, key: str) -> str:
        return f"{STATIC_PREFIX}{key}"

    def _probing_unconfigured_default(self) -> bool:
        return not self.has_reached_redis

    def ensure_redis(self) -> bool:
        if self._connectable:
            self.is_degraded = False
        return self._connectable

    def hset(self, key: str, mapping: dict) -> bool:
        self.store.setdefault(self._get_full_key(key), {}).update(
            {k: str(v) for k, v in mapping.items()}
        )
        return True

    def hgetall(self, key: str) -> dict[str, str]:
        """The forgiving per-key read: on a store error it degrades and
        answers from this process's memory, exactly as production does."""
        physical = self._get_full_key(key)
        if physical in self.store:
            return dict(self.store[physical])
        if key in self._memory:
            self.degrade_count += 1
            self.is_degraded = not self.recovers_in_loop
            return dict(self._memory[key])
        return {}


def _repo(backend: FakeBackend, *service_names: str):
    repo = RedisCircuitBreakerStateRepository(backend=backend)
    for name in service_names:
        repo.get_or_create(name)
    return repo


@pytest.fixture(autouse=True)
def _plain_namespace(monkeypatch):
    """Namespacing off, so the physical prefix is the static one."""
    from baldur.settings.namespace import reset_namespace_settings

    monkeypatch.setenv("BALDUR_NAMESPACE_NAMESPACE_ENABLED", "false")
    reset_namespace_settings()
    yield
    reset_namespace_settings()


class TestRedisClusterStatesBehavior:
    """The cluster-verdict read, one refusal at a time."""

    def test_cluster_states_returns_every_row_the_repository_wrote(self):
        """The happy path: a full walk answers with the whole keyspace."""
        backend = FakeBackend()
        repo = _repo(backend, "payment-api", "catalog-api")

        states = repo.get_cluster_states()

        assert {s.service_name for s in states} == {"payment-api", "catalog-api"}

    def test_cluster_states_declines_a_store_nobody_named(self):
        """A never-reached, never-configured backend is not dialed at all."""
        backend = FakeBackend(has_reached_redis=False)
        repo = _repo(backend)

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            repo.get_cluster_states()

        assert excinfo.value.reason == "unreached_default_store"
        assert backend.raw_redis_client.scan_calls == 0

    def test_cluster_states_raises_when_the_backend_is_degraded_and_unreachable(self):
        """A degraded backend that cannot reconnect has no cluster view."""
        backend = FakeBackend(is_degraded=True, connectable=False)
        repo = _repo(backend)

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            repo.get_cluster_states()

        assert excinfo.value.reason == "backend_degraded"
        assert backend.raw_redis_client.scan_calls == 0

    def test_cluster_states_proceeds_when_a_degraded_backend_reconnects(self):
        """Degraded is not terminal: a successful reconnect answers normally."""
        backend = FakeBackend(is_degraded=True, connectable=True)
        repo = _repo(backend, "payment-api")

        states = repo.get_cluster_states()

        assert [s.service_name for s in states] == ["payment-api"]

    def test_cluster_states_raises_when_the_scan_itself_fails(self):
        """A SCAN that raises is reported as such, not as an empty fleet."""
        backend = FakeBackend()
        repo = _repo(backend, "payment-api")
        backend.raw_redis_client.scan_error = RuntimeError("connection reset")

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            repo.get_cluster_states()

        assert excinfo.value.reason.startswith("scan_failed:")

    def test_cluster_states_raises_when_the_walk_hits_its_iteration_guard(self):
        """A keyspace that never finishes enumerating is not a cluster answer.

        The sibling ``get_open_states()`` returns what it found here; a
        fleet-wide verdict computed from a partial walk under-counts OPEN
        circuits by exactly the rows the walk never reached.
        """
        backend = FakeBackend()
        repo = _repo(backend, "payment-api")
        # A cursor that never returns to 0 -- the shape of a keyspace larger
        # than the bounded walk.
        backend.raw_redis_client.pages = [(7, [])]

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            repo.get_cluster_states()

        assert excinfo.value.reason == "scan_incomplete"

    def test_cluster_states_walks_more_than_one_page_before_answering(self):
        """A multi-page keyspace is enumerated to its end, not to its first page."""
        backend = FakeBackend()
        repo = _repo(backend, "payment-api", "catalog-api")
        physical = sorted(backend.store)
        backend.raw_redis_client.pages = [(4, [physical[0]]), (0, [physical[1]])]

        states = repo.get_cluster_states()

        assert {s.service_name for s in states} == {"payment-api", "catalog-api"}
        assert backend.raw_redis_client.scan_calls == 2

    def test_cluster_states_raises_when_the_backend_degrades_during_the_scan(self):
        """A mid-walk blip mixes store rows with memory rows -- so it raises.

        The per-key read falls back silently, so without this sample the
        result would be a list nobody could tell apart from a clean one.
        """
        backend = FakeBackend()
        repo = _repo(backend, "payment-api")
        physical = next(iter(backend.store))
        # The row leaves the store mid-walk and is answered from memory,
        # which is what bumps the degrade counter.
        backend._memory["cb:payment-api"] = dict(backend.store.pop(physical))
        backend.raw_redis_client.pages = [(0, [physical])]

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            repo.get_cluster_states()

        assert excinfo.value.reason == "degraded_during_scan"

    def test_cluster_states_raises_even_when_the_backend_recovers_inside_the_walk(self):
        """The counter, not the mode flag, is the discriminator.

        A ``is_degraded`` check taken after the walk answers False whenever
        the backend's own recovery flips the mode back inside the loop -- and
        the mixed list would pass as the shared view.
        """
        backend = FakeBackend()
        repo = _repo(backend, "payment-api")
        physical = next(iter(backend.store))
        backend._memory["cb:payment-api"] = dict(backend.store.pop(physical))
        backend.raw_redis_client.pages = [(0, [physical])]
        backend.recovers_in_loop = True

        with pytest.raises(CircuitBreakerStateUnavailableError):
            repo.get_cluster_states()

        assert backend.is_degraded is False
        assert backend.degrade_count == 1

    def test_cluster_states_never_answers_from_the_memory_view(self):
        """No path returns the process-local rows the forgiving read would."""
        backend = FakeBackend(is_degraded=True, connectable=False)
        repo = _repo(backend)
        backend._memory["cb:payment-api"] = {
            "service_name": "payment-api",
            "state": "closed",
        }

        with pytest.raises(CircuitBreakerStateUnavailableError):
            repo.get_cluster_states()

    def test_get_all_states_keeps_its_forgiving_semantics(self):
        """The existing callers' read is untouched by the cluster contract."""
        backend = FakeBackend(is_degraded=True, connectable=False)
        repo = _repo(backend)

        assert repo.get_all_states() == []

    def test_cluster_state_error_names_the_operation_it_could_not_serve(self):
        """Every raise from this method names the same operation."""
        backend = FakeBackend(has_reached_redis=False)
        repo = _repo(backend)

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            repo.get_cluster_states()

        assert excinfo.value.operation == "get_cluster_states"


class TestRedisClusterScanBoundsContract:
    """The walk's bounds are the sibling read's, with the opposite verdict."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("_CLUSTER_SCAN_PAGE_SIZE", 100),
            ("_CLUSTER_SCAN_MAX_ITERATIONS", 1000),
            ("_CLUSTER_SCAN_DEADLINE_SECONDS", 2.0),
        ],
    )
    def test_scan_bounds_are_named_constants(self, name: str, expected: Any):
        """Operational values resolve through a module constant, not a literal."""
        from baldur.adapters.redis import circuit_breaker as module

        assert getattr(module, name) == expected
