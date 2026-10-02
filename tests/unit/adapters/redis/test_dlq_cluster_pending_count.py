"""``get_cluster_pending_count_by_domain()`` answers from Redis, or it raises.

The ordinary pending count is forgiving on purpose: when the backend is not on
Redis it answers from this process's memory, and on a cold composite it falls
back to a bounded walk of the legacy by-domain index. Both are fine for a
console number and wrong for the question this read answers — "is anything
parked under this name?" — because a substituted count reads as "nothing is
parked", the direction that silences the signal for work that is.

So every read here goes through the raw client, and every fault raises
``DLQError`` instead. The backend double below keeps a process-local view that
disagrees with Redis, so any read that reached it would return a wrong number
rather than raise.

Verification techniques applied:
- Exception/edge cases: each refusal (backend not on Redis, no raw client,
  composite cannot warm, ZCARD failed), each with its machine-readable reason
- Negative assertion: no refusal falls back to the backend's own reads
- State: a backend constructed degraded is connected by the read itself; a
  backend whose mode leaves Redis right after admission still answers Redis
"""

from __future__ import annotations

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.core.exceptions import DLQError

PHYSICAL_PREFIX = "baldur:"
DOMAIN = "payment_api"
OTHER_DOMAIN = "catalog_api"


class FakeRawRedis:
    """Sorted sets keyed by physical key, with scriptable command failures."""

    def __init__(self) -> None:
        self.zsets: dict[str, set[str]] = {}
        self.fail: set[str] = set()
        self.calls: list[str] = []

    def _maybe_fail(self, command: str) -> None:
        self.calls.append(command)
        if command in self.fail:
            raise ConnectionError(f"{command} lost the connection")

    def execute_command(self, command: str, *args):
        self._maybe_fail(command)
        if command == "EXISTS":
            return 1 if self.zsets.get(args[0]) else 0
        if command == "ZINTERSTORE":
            destination, numkeys, *rest = args
            sources = [self.zsets.get(key, set()) for key in rest[:numkeys]]
            members = set.intersection(*sources) if sources else set()
            # Redis deletes the destination when the intersection is empty.
            if members:
                self.zsets[destination] = members
            else:
                self.zsets.pop(destination, None)
            return len(members)
        raise AssertionError(f"unexpected command {command}")

    def zcard(self, key: str) -> int:
        self._maybe_fail("ZCARD")
        return len(self.zsets.get(key, set()))


class FakeBackend:
    """A ResilientStorageBackend stand-in with a process-local view beside Redis.

    Its own read methods answer from ``memory`` the way the production backend
    does whenever it is off Redis; ``substituted_reads`` records every call to
    them, which the strict count must never make.
    """

    def __init__(
        self,
        raw: FakeRawRedis | None,
        *,
        connects: bool = True,
        degraded: bool = False,
    ) -> None:
        self.raw_redis_client = raw
        self.is_degraded = degraded
        self._connects = connects
        self.ensure_calls = 0
        # Whether the mode leaves Redis right after admission (another
        # thread's degradation, or a second recovery caller writing RECOVERING).
        self.leaves_redis_after_admission = False
        self.memory: dict[str, list[str]] = {}
        self.substituted_reads: list[tuple[str, str]] = []

    def _get_full_key(self, key: str) -> str:
        return f"{PHYSICAL_PREFIX}{key}"

    def ensure_redis(self) -> bool:
        self.ensure_calls += 1
        if not self._connects:
            return False
        self.is_degraded = self.leaves_redis_after_admission
        return True

    def zcard(self, key: str) -> int:
        self.substituted_reads.append(("zcard", key))
        return len(self.memory.get(key, []))

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        self.substituted_reads.append(("zrange", key))
        return list(self.memory.get(key, []))


def _repo(backend: FakeBackend) -> RedisDLQRepository:
    return RedisDLQRepository(backend=backend, pod_id="pod-a", pid=1, run_nonce="n0")


def _park(repo: RedisDLQRepository, raw: FakeRawRedis, domain: str, *ids: str) -> None:
    """File pending entries in the per-status and by-domain indexes, as a
    create does, leaving the composite cold so the read must warm it."""
    backend = repo._backend
    pending = backend._get_full_key(repo.PENDING_KEY)
    by_domain = backend._get_full_key(f"{repo.BY_DOMAIN_PREFIX}{domain}")
    raw.zsets.setdefault(pending, set()).update(ids)
    raw.zsets.setdefault(by_domain, set()).update(ids)


def _resolve(repo: RedisDLQRepository, raw: FakeRawRedis, domain: str, *ids: str):
    """File entries that left pending: still under the domain, not pending."""
    backend = repo._backend
    by_domain = backend._get_full_key(f"{repo.BY_DOMAIN_PREFIX}{domain}")
    raw.zsets.setdefault(by_domain, set()).update(ids)


def _composite(repo: RedisDLQRepository, domain: str) -> str:
    return repo._status_domain_key("pending", domain)


class TestClusterPendingCountRedisBehavior:
    """The strict pending count, one refusal at a time."""

    def test_cluster_pending_count_counts_only_the_domains_pending_entries(self):
        """Entries that left pending, and other domains' entries, are not counted."""
        raw = FakeRawRedis()
        backend = FakeBackend(raw)
        repo = _repo(backend)
        _park(repo, raw, DOMAIN, "e1", "e2")
        _resolve(repo, raw, DOMAIN, "e3")
        _park(repo, raw, OTHER_DOMAIN, "e4")

        assert repo.get_cluster_pending_count_by_domain(DOMAIN) == 2
        assert backend.substituted_reads == []

    def test_cluster_pending_count_empty_domain_answers_zero(self):
        """Zero is an answer from Redis, not a refusal."""
        raw = FakeRawRedis()
        repo = _repo(FakeBackend(raw))
        _park(repo, raw, OTHER_DOMAIN, "e1")

        assert repo.get_cluster_pending_count_by_domain(DOMAIN) == 0

    def test_cluster_pending_count_fresh_backend_is_connected_by_the_read(self):
        """A backend is constructed degraded; the first count connects it
        instead of answering from the empty memory it starts with."""
        raw = FakeRawRedis()
        backend = FakeBackend(raw, degraded=True)
        repo = _repo(backend)
        _park(repo, raw, DOMAIN, "e1", "e2")

        assert repo.get_cluster_pending_count_by_domain(DOMAIN) == 2
        assert backend.ensure_calls == 1
        assert backend.is_degraded is False

    def test_cluster_pending_count_mode_leaving_redis_after_admission_still_reads_redis(
        self,
    ):
        """Between the readiness check and the read the backend can leave
        Redis; its own count would then answer from memory (5 here)."""
        raw = FakeRawRedis()
        backend = FakeBackend(raw)
        backend.leaves_redis_after_admission = True
        repo = _repo(backend)
        _park(repo, raw, DOMAIN, "e1", "e2")
        backend.memory[_composite(repo, DOMAIN)] = ["m1", "m2", "m3", "m4", "m5"]

        assert repo.get_cluster_pending_count_by_domain(DOMAIN) == 2
        assert backend.substituted_reads == []

    @pytest.mark.parametrize(
        ("fault", "reason"),
        [
            pytest.param({"connects": False}, "redis_inactive", id="not_on_redis"),
            pytest.param({"raw": None}, "raw_client_missing", id="no_raw_client"),
            pytest.param(
                {"fail": "EXISTS"}, "composite_unavailable", id="composite_cannot_warm"
            ),
            pytest.param({"fail": "ZCARD"}, "zcard_failed", id="zcard_failed"),
        ],
    )
    def test_cluster_pending_count_fault_raises_instead_of_substituting(
        self, fault, reason
    ):
        """Each fault the forgiving count would paper over raises, and none
        reaches the process-local view (which holds entries here)."""
        # Given
        raw = FakeRawRedis()
        if "fail" in fault:
            raw.fail.add(fault["fail"])
        backend = FakeBackend(
            fault.get("raw", raw), connects=fault.get("connects", True)
        )
        repo = _repo(backend)
        _park(repo, raw, DOMAIN, "e1")
        backend.memory[_composite(repo, DOMAIN)] = ["m1", "m2"]
        backend.memory[f"{repo.BY_DOMAIN_PREFIX}{DOMAIN}"] = ["m1", "m2"]

        # When
        with pytest.raises(DLQError, match=reason):
            repo.get_cluster_pending_count_by_domain(DOMAIN)

        # Then: neither the backend's count nor the legacy by-domain walk ran.
        assert backend.substituted_reads == []

    def test_cluster_pending_count_backend_not_on_redis_sends_no_command(self):
        """An inactive backend is refused before the raw client is touched."""
        raw = FakeRawRedis()
        repo = _repo(FakeBackend(raw, connects=False))

        with pytest.raises(DLQError):
            repo.get_cluster_pending_count_by_domain(DOMAIN)

        assert raw.calls == []

    def test_cluster_pending_count_zcard_failure_is_chained(self):
        """The Redis error stays reachable as the cause of the refusal."""
        raw = FakeRawRedis()
        raw.fail.add("ZCARD")
        repo = _repo(FakeBackend(raw))
        _park(repo, raw, DOMAIN, "e1")

        with pytest.raises(DLQError) as caught:
            repo.get_cluster_pending_count_by_domain(DOMAIN)

        assert isinstance(caught.value.__cause__, ConnectionError)
