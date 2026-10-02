"""Real-Redis integration tests for ``get_cluster_pending_count_by_domain`` (809 D3).

What this tests that the unit tests cannot: the strict count's contract against
a real server — the composite ``(pending, domain)`` index warmed by a real
``ZINTERSTORE`` from the per-status and by-domain indexes a real create wrote,
a raw ``ZCARD`` over it, and a backend that starts degraded and is connected
by the read itself. The unit suite proves the refusals against a fake; this
file proves the answer is the one the replay selection would act on, and that
a backend off Redis refuses while its forgiving sibling answers from memory.

Coverage axes:
- Parity: the strict count equals the domain-scoped replay selection's
  population and the ordinary count, per domain, with non-pending entries
  and another domain's entries in the store
- Cold start: a freshly constructed repository's first read connects and
  answers from Redis
- Refusal: Redis unreachable from the start, or lost after first use, raises
  ``DLQError`` while the ordinary count answers from process memory

Backends run with ``auto_recovery=False`` and a per-test WAL directory, so a
degraded backend never replays its writes into a later test's keys.

Auto-skips when Redis is unavailable via the ``requires_redis`` autoskip hook.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.requires_redis

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.core.exceptions import DLQError
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.settings.resilient_storage import ResilientStorageSettings

KEY_PREFIX = "test:baldur:"
# Nothing listens on port 1, so the backend can never leave degraded mode.
UNREACHABLE_URL = "redis://localhost:1/0"


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    """Reset the runtime-scoped Redis negative cache so a backend can init."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


@pytest.fixture
def make_repository(tmp_path):
    """Build Redis DLQ repositories on fresh backends sharing one key space."""
    built: list[ResilientStorageBackend] = []

    def _make(redis_url: str, name: str = "a") -> RedisDLQRepository:
        settings = ResilientStorageSettings(
            redis_url=redis_url,
            key_prefix=KEY_PREFIX,
            use_dynamic_prefix=False,
            allow_memory_only=True,
            auto_recovery=False,
            wal_dir=str(tmp_path / f"wal-{name}"),
        )
        backend = ResilientStorageBackend(settings=settings)
        built.append(backend)
        return RedisDLQRepository(backend=backend)

    yield _make
    for backend in built:
        try:
            backend.close()
        except Exception:
            pass


def _seed(repo: RedisDLQRepository) -> None:
    """Pending entries in two domains, plus one that left pending."""
    for _ in range(3):
        repo.create(domain="payment_api", failure_type="TIMEOUT")
    resolved = repo.create(domain="payment_api", failure_type="TIMEOUT")
    repo.update_status(resolved.id, FailedOperationStatus.RESOLVED.value)
    for _ in range(2):
        repo.create(domain="catalog_api", failure_type="TIMEOUT")


class TestClusterPendingCountRedis:
    """The strict count against a real server."""

    @pytest.mark.parametrize("domain", ["payment_api", "catalog_api", "absent_api"])
    def test_cluster_pending_count_equals_the_domain_scoped_selection(
        self, make_repository, redis_url, domain
    ):
        """
        Purpose:
            A count of 0 must mean the recovery's selection finds nothing,
            so the two read the same population.
        Expected:
            - the strict count equals the replay page's entry count
            - and equals the ordinary count on a healthy backend
        """
        repo = make_repository(redis_url)
        _seed(repo)

        strict = repo.get_cluster_pending_count_by_domain(domain)

        page = repo.find_replayable_page(max_retries=10, domain=domain, limit=100)
        assert strict == len(page.entries)
        assert strict == repo.get_pending_count_by_domain(domain)

    def test_fresh_repository_first_read_connects_and_answers(
        self, make_repository, redis_url
    ):
        """
        Purpose:
            A backend is constructed degraded with empty memory; the first
            strict read must connect it, not answer from that memory.
        Expected:
            - a second process's view (a new backend) counts the 3 entries
              the first one wrote
        """
        writer = make_repository(redis_url, "writer")
        _seed(writer)
        reader = make_repository(redis_url, "reader")
        assert reader._backend.is_degraded is True

        assert reader.get_cluster_pending_count_by_domain("payment_api") == 3

    def test_unreachable_redis_raises_while_the_ordinary_count_answers_from_memory(
        self, make_repository
    ):
        """
        Purpose:
            With Redis unreachable from the start, writes land in process
            memory; a count read from there would decide "nothing parked"
            for a store this process cannot see.
        Expected:
            - the ordinary count answers 2 from memory
            - the strict count raises DLQError
        """
        repo = make_repository(UNREACHABLE_URL)
        for _ in range(2):
            repo.create(domain="payment_api", failure_type="TIMEOUT")

        assert repo.get_pending_count_by_domain("payment_api") == 2
        with pytest.raises(DLQError):
            repo.get_cluster_pending_count_by_domain("payment_api")

    def test_redis_lost_after_first_use_raises_instead_of_answering_from_memory(
        self, make_repository, redis_url
    ):
        """
        Purpose:
            A backend that was on Redis and then degraded answers its own
            reads from memory; the strict count refuses until recovery.
        Expected:
            - the first strict read answers from Redis
            - after degradation, a write lands in memory and the strict count
              raises DLQError
        """
        repo = make_repository(redis_url)
        _seed(repo)
        assert repo.get_cluster_pending_count_by_domain("payment_api") == 3

        repo._backend._switch_to_degraded()
        repo.create(domain="payment_api", failure_type="TIMEOUT")

        with pytest.raises(DLQError):
            repo.get_cluster_pending_count_by_domain("payment_api")
