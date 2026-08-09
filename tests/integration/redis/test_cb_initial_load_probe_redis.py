"""749 D10 — a never-probed backend still hydrates the operator's state.

``ResilientStorageBackend`` is constructed DEGRADED and connects lazily, per
operation. The per-key paths trigger that lazy init through their own backend
calls; the full-state scan had no backend operation to trigger it, so it read
``is_degraded`` on a backend that had simply never tried yet and returned the
in-memory fallback — empty.

The consumer that pays for it is the layered repository's construction-time
load: the lane that hydrates operator state into a process that did not take
the block. So a worker starting during an incident came up believing nothing
was blocked, and admitted every request the operator had cut off.

The unit test's fake flips ``is_degraded`` on cue, which proves the fake's
ordering. Only a real backend shows that a genuinely fresh one is reachable
after the probe and empty before it.

Requires a running Redis instance (auto-skip via ``requires_redis``).
"""

from __future__ import annotations

import pytest

from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
    reset_layered_repository_executor,
)
from baldur.adapters.redis.circuit_breaker import RedisCircuitBreakerStateRepository
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.settings.resilient_storage import ResilientStorageSettings

pytestmark = pytest.mark.requires_redis

SERVICE = "payment-api"
KEY_PREFIX = "test:baldur:"


@pytest.fixture(autouse=True)
def _quiet_background_lanes(monkeypatch):
    """Keep the layered repo's background lanes out of these assertions.

    Drift reconciliation and the L2 prewarm both submit work on the shared
    executor at construction; either can write to Redis while the test is
    reading it.
    """
    monkeypatch.setattr(
        "baldur.adapters.memory.layered_repository.drift_operations."
        "DriftOperationsMixin._schedule_drift_reconciliation",
        lambda self: None,
    )
    monkeypatch.setattr(
        "baldur.adapters.memory.layered_repository.base."
        "LayeredRepositoryBase._ensure_l2_warmup_once",
        lambda self: None,
    )
    yield
    reset_layered_repository_executor()


def _fresh_backend(redis_url: str) -> ResilientStorageBackend:
    """A backend in the state every process starts in: never yet connected."""
    settings = ResilientStorageSettings(
        redis_url=redis_url,
        key_prefix=KEY_PREFIX,
        use_dynamic_prefix=False,
        allow_memory_only=True,
    )
    backend = ResilientStorageBackend(settings=settings)
    assert backend.is_degraded is True, "premise: a new backend has not connected yet"
    return backend


@pytest.fixture
def blocked_service(redis_client, redis_url):
    """An operator's Block, written by another process, sitting in Redis."""
    writer_backend = _fresh_backend(redis_url)
    writer = RedisCircuitBreakerStateRepository(backend=writer_backend)
    writer.atomic_force_open(SERVICE, reason="incident", ttl_minutes=90)
    assert writer.get_by_service_name(SERVICE).manually_controlled is True

    yield

    for key in redis_client.keys(f"{KEY_PREFIX}*"):
        redis_client.delete(key)


class TestUnprobedBackendInitialLoad:
    """Construction-time hydration on a backend that has never connected."""

    def test_initial_load_probe_precedes_the_degraded_verdict(
        self, blocked_service, redis_url
    ):
        """
        Purpose:
            ``get_all_states`` on a never-probed backend returns the Redis rows.
        Expected:
            - the block written out-of-band comes back
            - the backend is no longer degraded afterwards (the probe ran)
        """
        backend = _fresh_backend(redis_url)
        repo = RedisCircuitBreakerStateRepository(backend=backend)

        states = repo.get_all_states()

        assert [s.service_name for s in states] == [SERVICE]
        assert states[0].manually_controlled is True
        assert backend.is_degraded is False

    def test_initial_load_probe_absence_would_have_returned_nothing(
        self, blocked_service, redis_url
    ):
        """The negative half: what the unprobed branch reads without the probe.

        ``_get_all_from_memory`` is the exact fallback the pre-fix code took on
        a degraded verdict. Asserting it is empty on this same fixture is what
        makes the row above evidence rather than a coincidence.
        """
        backend = _fresh_backend(redis_url)
        repo = RedisCircuitBreakerStateRepository(backend=backend)

        assert repo._get_all_from_memory() == []

    def test_initial_load_probe_lets_a_new_process_reject_blocked_traffic(
        self, blocked_service, redis_url
    ):
        """End to end: the reason the probe matters at all.

        A layered repository built on the fresh backend hydrates the pinned row
        at construction, and admission then refuses the request — the behaviour
        an operator expects from a worker that boots mid-incident.
        """
        backend = _fresh_backend(redis_url)
        layered = LayeredCircuitBreakerStateRepository(
            l2_repo=RedisCircuitBreakerStateRepository(backend=backend),
            adapter_type="redis",
        )
        service = CircuitBreakerService(
            config=CircuitBreakerConfig(enabled=True, recovery_timeout=1),
            repository=layered,
        )

        # Read L1 directly. Going through the layered ``get_by_service_name``
        # would fall through to the per-key L2 read on a miss — a lane whose
        # own backend call triggers the lazy connect — and would therefore pass
        # even with the construction-time scan returning nothing.
        row = layered._l1.get_by_service_name(SERVICE)
        assert row is not None
        assert row.manually_controlled is True
        assert service.should_allow(SERVICE) is False
