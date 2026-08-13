"""The constructor's initial L2 load skips a Redis nobody named.

The load runs in an executor with a doubled timeout, so on a host whose
loopback refuses slowly it absorbed up to two seconds of the first protected
call and then announced its own timeout at WARNING against an address nobody
configured. Hot-path record operations sync to L2 fire-and-forget, so
declining the load costs the caller thread nothing.

The gate is on configuration *intent*, never on ``is_degraded``: a resilient
storage backend constructs DEGRADED and stays that way until its first probe,
so an ``is_degraded`` gate would skip the load on every healthy configured
process too.

It guards absence in both directions, and the ``is True`` pin is the half that
is easy to lose. A ``MagicMock`` answers the predicate with a truthy mock, so
under plain truthiness every spec'd test double in the tree would silently
stop hydrating — a change no production posture would reveal.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
    LayeredCircuitBreakerStateRepository,
)
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.interfaces.repositories import CircuitBreakerStateData
from baldur.settings.resilient_storage import ResilientStorageSettings

SKIP_EVENT = "layered_repo.initial_load_skipped"


def _l2_with_backend(backend):
    """A spec-bounded L2 double carrying the given ``_backend`` object."""
    l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
    l2.get_all_states.return_value = [
        CircuitBreakerStateData(
            service_name="payment-service",
            state="open",
            failure_count=7,
            success_count=2,
        )
    ]
    l2._backend = backend
    return l2


def _redis_layered_repo(l2, **kwargs):
    """The construction the skip gates — an L2 the caller called Redis-backed.

    ``adapter_type`` defaults to ``"unknown"`` on this class, which the gate
    declines to skip on, so every case naming the Redis posture has to say so.
    """
    kwargs.setdefault("adapter_type", "redis")
    return LayeredCircuitBreakerStateRepository(l2_repo=l2, **kwargs)


@pytest.fixture
def unconfigured_l2(no_redis_posture):
    """An L2 whose backend reports it would be dialing an unnamed address.

    The real backend answers the predicate off the same settings the posture
    fixture patched, so nothing here restates the verdict the production
    predicate reaches.
    """
    backend = ResilientStorageBackend()
    assert backend._probing_unconfigured_default() is True
    return _l2_with_backend(backend)


class TestLayeredInitialLoadSkipBehavior:
    """Construction in the posture where nobody named a Redis."""

    def test_unconfigured_redis_l2_is_not_read_at_construction(self, unconfigured_l2):
        """The executor submit that cost the first protected call never happens."""
        _redis_layered_repo(unconfigured_l2)

        unconfigured_l2.get_all_states.assert_not_called()

    def test_skipped_load_leaves_the_sync_timestamp_unset(self, unconfigured_l2):
        """Nothing was hydrated, so nothing may claim a sync happened."""
        repo = _redis_layered_repo(unconfigured_l2)

        assert repo._last_sync_time is None

    def test_skipped_load_announces_at_debug_with_its_reason(self, unconfigured_l2):
        """A zero-config first run is the expected posture, not an incident."""
        with capture_logs() as logs:
            _redis_layered_repo(unconfigured_l2)

        skips = [entry for entry in logs if entry["event"] == SKIP_EVENT]
        assert [
            (entry["log_level"], entry["reason"], entry["adapter_type"])
            for entry in skips
        ] == [("debug", "redis_not_configured", "redis")]

    def test_skipped_load_emits_no_warning_or_above(self, unconfigured_l2):
        """The WARNING this posture used to produce is what the skip removes."""
        with capture_logs() as logs:
            _redis_layered_repo(unconfigured_l2)

        assert [
            entry
            for entry in logs
            if entry["log_level"] in ("warning", "error", "critical")
        ] == []

    def test_admin_force_resync_still_reads_l2_in_the_skipped_posture(
        self, unconfigured_l2
    ):
        """The operator's recourse is the point of leaving the state unhydrated.

        The return value is deliberately not asserted: the method answers True
        whenever the load did not raise, and the load swallows both of its own
        failure branches — a pre-existing shape this skip neither creates nor
        relies on.
        """
        repo = _redis_layered_repo(unconfigured_l2)

        repo.force_sync_from_l2()

        unconfigured_l2.get_all_states.assert_called_once()
        assert [s.service_name for s in repo._l1.get_all_states()] == [
            "payment-service"
        ]


class TestLayeredInitialLoadSkipGateBehavior:
    """Which shapes the gate declines to skip on."""

    def test_backend_reporting_configured_intent_still_loads(self, no_redis_posture):
        """A named Redis is loaded from, unreachable or not."""
        backend = ResilientStorageBackend(
            settings=ResilientStorageSettings(redis_url=no_redis_posture)
        )
        assert backend._probing_unconfigured_default() is False
        l2 = _l2_with_backend(backend)

        _redis_layered_repo(l2)

        l2.get_all_states.assert_called_once()

    def test_l2_without_a_backend_attribute_still_loads(self, no_redis_posture):
        """An explicitly injected custom L2 exposes no backend to ask."""
        l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        l2.get_all_states.return_value = []

        _redis_layered_repo(l2)

        l2.get_all_states.assert_called_once()

    def test_backend_without_the_predicate_still_loads(self, no_redis_posture):
        """Several unrelated adapters carry a ``_backend`` attribute name."""
        l2 = _l2_with_backend(object())

        _redis_layered_repo(l2)

        l2.get_all_states.assert_called_once()

    def test_spec_bounded_backend_double_still_loads(self, no_redis_posture):
        """The ``is True`` pin, asserted directly rather than left to callers.

        A ``MagicMock(spec=...)`` answers the predicate with a truthy mock. If
        the gate accepted truthiness, every existing redis-typed construction
        built on a spec'd double would stop hydrating — and each of them would
        have to discover it separately.
        """
        backend = MagicMock(spec=ResilientStorageBackend)
        assert backend._probing_unconfigured_default() is not True
        l2 = _l2_with_backend(backend)

        _redis_layered_repo(l2)

        l2.get_all_states.assert_called_once()

    @pytest.mark.parametrize("adapter_type", ["django", "unknown"])
    def test_non_redis_adapter_type_still_loads(
        self, adapter_type, unconfigured_l2, no_redis_posture
    ):
        """The Redis posture says nothing about a Django-backed L2."""
        _redis_layered_repo(unconfigured_l2, adapter_type=adapter_type)

        unconfigured_l2.get_all_states.assert_called_once()
