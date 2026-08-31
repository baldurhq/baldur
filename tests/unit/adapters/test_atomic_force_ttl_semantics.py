"""741 D4/D6 — every repository stores a manual override's lifetime alike.

``atomic_force_open`` / ``atomic_force_close`` are the storage side of a manual
override, and the adapters used to disagree about what a request means:

- a non-positive TTL stored no expiry on memory (a permanent pin) but a
  *past* timestamp on Redis (an override that lapsed the instant it was
  written) — the same operator request meaning opposite things per backend;
- ``atomic_force_close`` wrote no expiry column at all on Redis, so a previous
  block's expiry stayed on the row and governed the force-close pin.

Both are now single-sourced in ``resolve_manual_override_expiry`` and every
implementation writes the column explicitly. These tests hold the live
implementations to the same table, which is the only way a per-adapter
regression shows up as a failure rather than as a backend-specific incident.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
)
from baldur.adapters.redis.circuit_breaker import (
    RedisCircuitBreakerStateRepository,
)
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.interfaces.repositories import resolve_manual_override_expiry
from baldur.utils.time import utc_now

SERVICE = "payment-api"

# The TTL values whose meaning used to depend on the backend.
NO_EXPIRY_TTLS = [None, 0, -1, -90]


# =============================================================================
# The shared resolver — the single definition the adapters delegate to
# =============================================================================


class TestResolveManualOverrideExpiryContract:
    """Storage-side TTL semantics, stated once."""

    @pytest.mark.parametrize("ttl", NO_EXPIRY_TTLS)
    def test_non_positive_or_absent_ttl_stores_no_expiry(self, ttl):
        """``None`` and non-positive TTLs all resolve to "no expiry stored"."""
        assert resolve_manual_override_expiry(ttl) is None

    @pytest.mark.parametrize("ttl", [1, 5, 90, 1440])
    def test_positive_ttl_becomes_now_plus_the_lifetime(self, ttl):
        """A positive TTL resolves to ``now + ttl`` within the call window."""
        before = utc_now()

        resolved = resolve_manual_override_expiry(ttl)

        assert (
            before + timedelta(minutes=ttl)
            <= resolved
            <= utc_now() + timedelta(minutes=ttl)
        )


# =============================================================================
# Adapter fixtures
# =============================================================================


@pytest.fixture
def memory_repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def layered_repo() -> LayeredCircuitBreakerStateRepository:
    """L1-only layered repository — the force-* pair routes through L1."""
    return LayeredCircuitBreakerStateRepository()


def _redis_repo() -> tuple[RedisCircuitBreakerStateRepository, MagicMock]:
    """Redis repository whose backend records the hash it was asked to write.

    ``atomic_force_*`` is a single ``hset`` of a field map, so the payload the
    adapter builds is the observable under test.
    """
    backend = MagicMock(spec=ResilientStorageBackend)
    backend.hset.return_value = True
    repo = RedisCircuitBreakerStateRepository(backend=backend)
    return repo, backend


def _redis_written_expiry(backend: MagicMock) -> str:
    """The ``manual_override_expires_at`` field of the most recent write."""
    _key, updates = backend.hset.call_args[0]
    return updates["manual_override_expires_at"]


# =============================================================================
# Equivalence across the live implementations
# =============================================================================


class TestAtomicForceTTLSemantics:
    """The same TTL request means the same thing on every backend."""

    @pytest.mark.parametrize("ttl", NO_EXPIRY_TTLS)
    @pytest.mark.parametrize("operation", ["atomic_force_open", "atomic_force_close"])
    def test_memory_stores_no_expiry_for_a_non_positive_ttl(
        self, memory_repo, ttl, operation
    ):
        """Memory: a non-positive TTL stores no expiry on either force op."""
        getattr(memory_repo, operation)(SERVICE, reason="test", ttl_minutes=ttl)

        row = memory_repo.get_by_service_name(SERVICE)
        assert row.manual_override_expires_at is None

    @pytest.mark.parametrize("ttl", NO_EXPIRY_TTLS)
    @pytest.mark.parametrize("operation", ["atomic_force_open", "atomic_force_close"])
    def test_layered_stores_no_expiry_for_a_non_positive_ttl(
        self, layered_repo, ttl, operation
    ):
        """Layered: a non-positive TTL stores no expiry on either force op."""
        getattr(layered_repo, operation)(SERVICE, reason="test", ttl_minutes=ttl)

        row = layered_repo.get_by_service_name(SERVICE)
        assert row.manual_override_expires_at is None

    @pytest.mark.parametrize("ttl", NO_EXPIRY_TTLS)
    @pytest.mark.parametrize("operation", ["atomic_force_open", "atomic_force_close"])
    def test_redis_stores_no_expiry_for_a_non_positive_ttl(self, ttl, operation):
        """Redis used to write ``now + ttl`` unguarded — a past timestamp."""
        repo, backend = _redis_repo()

        getattr(repo, operation)(SERVICE, reason="test", ttl_minutes=ttl)

        assert _redis_written_expiry(backend) == ""

    @pytest.mark.parametrize("operation", ["atomic_force_open", "atomic_force_close"])
    def test_memory_stores_the_requested_lifetime(self, memory_repo, operation):
        """Memory: a positive TTL is stored as ``now + ttl``."""
        before = utc_now()

        getattr(memory_repo, operation)(SERVICE, reason="test", ttl_minutes=30)

        stored = memory_repo.get_by_service_name(SERVICE).manual_override_expires_at
        assert (
            before + timedelta(minutes=30)
            <= stored
            <= utc_now() + timedelta(minutes=30)
        )

    @pytest.mark.parametrize("operation", ["atomic_force_open", "atomic_force_close"])
    def test_redis_stores_the_requested_lifetime(self, operation):
        """Redis: a positive TTL lands in the written hash as ``now + ttl``."""
        repo, backend = _redis_repo()
        before = utc_now()

        getattr(repo, operation)(SERVICE, reason="test", ttl_minutes=30)

        written = _redis_written_expiry(backend)
        assert written != ""
        stored = datetime.fromisoformat(written)
        assert (
            before + timedelta(minutes=30)
            <= stored
            <= utc_now() + timedelta(minutes=30)
        )


class TestForceCloseClearsAPreviousBlocksExpiry:
    """A force-close pin never inherits the expiry an earlier block wrote."""

    def test_memory_force_close_overwrites_the_stored_expiry(self, memory_repo):
        """Memory: a TTL-less force-close erases the block's stored expiry."""
        memory_repo.atomic_force_open(SERVICE, reason="block", ttl_minutes=90)

        memory_repo.atomic_force_close(SERVICE, reason="allow", ttl_minutes=None)

        row = memory_repo.get_by_service_name(SERVICE)
        assert row.manual_override_expires_at is None

    def test_redis_force_close_writes_the_expiry_field_explicitly(self):
        """The Redis latent bug: the field was left out of the update map.

        A prior block's expiry then survived the force-close, and the sweep
        would clear the pin at a timestamp that belonged to another decision.
        """
        repo, backend = _redis_repo()

        repo.atomic_force_close(SERVICE, reason="allow", ttl_minutes=None)

        _key, updates = backend.hset.call_args[0]
        assert "manual_override_expires_at" in updates
        assert updates["manual_override_expires_at"] == ""
