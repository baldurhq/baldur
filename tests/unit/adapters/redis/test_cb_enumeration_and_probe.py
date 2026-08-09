"""The two keyspace-enumeration defects that made an operator's row invisible.

Both failed silently and both looked identical to a genuinely empty store:

- **D12, the scan pattern.** Every write resolves its physical key through the
  backend's key seam, which applies the *dynamic* namespace prefix when
  namespacing is enabled. The three enumeration sites built their pattern from
  the *static* configured prefix instead, so a namespaced deployment scanned a
  prefix nothing had ever been written under.
- **D10, the degraded short-circuit.** The backend is constructed DEGRADED and
  connects lazily per operation. Unlike the per-key paths — whose backend calls
  run that lazy init themselves — the full-state scan had no backend operation
  to trigger it, so a never-yet-connected backend reported an empty keyspace to
  its first consumer. For the layered repository that consumer is the
  construction-time load: the lane that hydrates operator state.

The fake here routes reads and writes through the same ``_get_full_key`` seam
the real backend uses. Without that it could not tell a correct scan pattern
from one that matches nothing — the pre-fix red run is the criterion.
"""

from __future__ import annotations

import fnmatch

import pytest

from baldur.adapters.redis.circuit_breaker import RedisCircuitBreakerStateRepository
from baldur.settings.namespace import get_effective_key_prefix

STATIC_PREFIX = "baldur:"


class FakeBackend:
    """A ResilientStorageBackend stand-in whose keys go through the real seam.

    ``hset`` / ``hgetall`` take a *component* key ("cb:payment-api") and apply
    the prefix internally, exactly as the real backend does; the raw client
    returns *physical* keys, exactly as Redis does. That asymmetry is where the
    defect lived.
    """

    def __init__(self, *, is_degraded: bool = False, connectable: bool = True) -> None:
        self.store: dict[str, dict[str, str]] = {}
        self.is_degraded = is_degraded
        self._connectable = connectable
        self._redis = object()
        self._memory: dict[str, dict[str, str]] = {}
        self.ensure_redis_calls = 0
        self.config = type("Config", (), {"key_prefix": STATIC_PREFIX})()
        self.raw_redis_client = FakeRedisClient(self.store)

    def _get_full_key(self, key: str) -> str:
        return f"{get_effective_key_prefix()}{key}"

    def ensure_redis(self) -> bool:
        self.ensure_redis_calls += 1
        if self._connectable:
            self.is_degraded = False
        return self._connectable

    def hset(self, key: str, mapping: dict) -> bool:
        self.store.setdefault(self._get_full_key(key), {}).update(
            {k: str(v) for k, v in mapping.items()}
        )
        self._memory.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})
        return True

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.store.get(self._get_full_key(key), {}))


class FakeRedisClient:
    """KEYS / SCAN over the physical keyspace."""

    def __init__(self, store: dict[str, dict[str, str]]) -> None:
        self._store = store

    def keys(self, pattern: str) -> list[str]:
        return [k for k in self._store if fnmatch.fnmatch(k, pattern)]

    def scan(self, cursor: int = 0, match: str = "*", count: int = 100):
        return 0, self.keys(match)


@pytest.fixture
def namespaced(monkeypatch):
    """A deployment with namespacing on — the shape that broke.

    The dynamic prefix and the static configured one must actually differ, or
    the pre-fix pattern would coincide with the correct one and the test would
    pass against the defect.
    """
    from baldur.settings.namespace import reset_namespace_settings

    monkeypatch.setenv("BALDUR_NAMESPACE_NAMESPACE_ENABLED", "true")
    monkeypatch.setenv("BALDUR_NAMESPACE_NAMESPACE", "seoul")
    reset_namespace_settings()
    assert get_effective_key_prefix() != STATIC_PREFIX
    yield
    reset_namespace_settings()


@pytest.fixture
def plain(monkeypatch):
    """A deployment with namespacing off — the shape that always worked."""
    from baldur.settings.namespace import reset_namespace_settings

    monkeypatch.setenv("BALDUR_NAMESPACE_NAMESPACE_ENABLED", "false")
    reset_namespace_settings()
    yield
    reset_namespace_settings()


def _seeded_repo(backend: FakeBackend, *service_names: str):
    repo = RedisCircuitBreakerStateRepository(backend=backend)
    for name in service_names:
        repo.get_or_create(name)
    return repo


# =============================================================================
# D12 — enumeration builds its pattern from the seam writes use
# =============================================================================


class TestEnumerationPrefixBehavior:
    """The three scan sites, against the keyspace the repository itself wrote.

    Pre-fix red run: with ``pattern`` built from
    ``backend.config.key_prefix + KEY_PREFIX``, every test in this class
    returns 0 rows under the ``namespaced`` fixture.
    """

    @pytest.mark.parametrize("fixture_name", ["plain", "namespaced"])
    def test_enumeration_prefix_finds_a_row_written_through_the_repository(
        self, request, fixture_name
    ):
        """Round-trip: what the repository wrote, the repository can enumerate."""
        request.getfixturevalue(fixture_name)
        backend = FakeBackend()
        repo = _seeded_repo(backend, "payment-api", "catalog-api")

        states = repo.get_all_states()

        assert {s.service_name for s in states} == {"payment-api", "catalog-api"}

    def test_enumeration_prefix_is_dynamic_not_the_configured_one(self, namespaced):
        """Named assertion on the pattern itself, so a failure says why.

        The row-count tests above would also fail on the pre-fix code, but they
        would report "0 states" and leave the reader to work out that the
        pattern, not the store, was empty.
        """
        backend = FakeBackend()
        repo = RedisCircuitBreakerStateRepository(backend=backend)

        prefix = repo._scan_prefix()

        assert prefix == f"{get_effective_key_prefix()}cb:"
        assert not prefix.startswith(f"{STATIC_PREFIX}cb:")

    def test_enumeration_prefix_stripping_preserves_the_service_name(self, namespaced):
        """The stripped name must be the name, not a namespace remnant.

        Stripping a shorter prefix than the one matched would leave
        ``seoul:cb:payment-api`` as the "service name" — a row that no
        per-key read could ever find again.
        """
        backend = FakeBackend()
        repo = _seeded_repo(backend, "payment-api")

        (state,) = repo.get_all_states()

        assert state.service_name == "payment-api"
        assert repo.get_state(state.service_name) is not None

    def test_enumeration_prefix_is_shared_by_the_scan_based_site(self, namespaced):
        """The SCAN-based sibling site, which had the same static pattern."""
        backend = FakeBackend()
        repo = _seeded_repo(backend, "payment-api")
        repo.update_state("payment-api", state="open")

        open_states = repo.get_open_states()

        assert [s.service_name for s in open_states] == ["payment-api"]

    def test_enumeration_prefix_is_shared_by_the_cleanup_site(self, namespaced):
        """The third site. A namespaced deployment retained keys forever."""
        backend = FakeBackend()
        repo = _seeded_repo(backend, "payment-api")
        backend.delete = lambda key: bool(
            backend.store.pop(backend._get_full_key(key), None)
        )
        # Age the row past any plausible retention window.
        physical = backend._get_full_key("cb:payment-api")
        backend.store[physical]["updated_at"] = "2020-01-01T00:00:00+00:00"

        deleted = repo.cleanup_stale_keys(retention_days=30)

        assert deleted == 1


# =============================================================================
# D10 — probe the backend before trusting its degraded verdict
# =============================================================================


class TestInitialLoadProbeBehavior:
    """``get_all_states`` on a never-yet-connected backend."""

    def test_initial_load_probe_runs_before_the_degraded_fallback(self, plain):
        """The lane that hydrates operator state gets the real keyspace.

        Pre-fix red run: ``is_degraded`` is True at entry, so the method
        returned the memory fallback — empty, and indistinguishable from a
        store with nothing in it.
        """
        # Given: a backend that holds rows but has never been probed. The rows
        # are seeded while it reports healthy, then it is put back to the
        # constructed-DEGRADED state a fresh process would see.
        backend = FakeBackend()
        repo = _seeded_repo(backend, "payment-api")
        backend.is_degraded = True
        backend._memory.clear()

        states = repo.get_all_states()

        assert backend.ensure_redis_calls == 1
        assert [s.service_name for s in states] == ["payment-api"]

    def test_initial_load_probe_failure_still_falls_back_to_memory(self, plain):
        """The probe is a probe, not an assumption that Redis is up.

        Positive control for the fallback: without it the fix could have
        deleted the degraded branch outright and this suite would not notice.
        """
        backend = FakeBackend(is_degraded=True, connectable=False)
        repo = RedisCircuitBreakerStateRepository(backend=backend)
        backend._memory["cb:payment-api"] = {
            "state": "open",
            "failure_count": "3",
            "success_count": "0",
            "manually_controlled": "True",
            "control_reason": "incident",
        }

        states = repo.get_all_states()

        assert backend.ensure_redis_calls == 1
        assert [s.service_name for s in states] == ["payment-api"]
        assert states[0].manually_controlled is True

    def test_initial_load_probe_is_skipped_on_a_healthy_backend(self, plain):
        """No probe on the path that never needed one — the common case."""
        backend = FakeBackend()
        repo = _seeded_repo(backend, "payment-api")

        repo.get_all_states()

        assert backend.ensure_redis_calls == 0
