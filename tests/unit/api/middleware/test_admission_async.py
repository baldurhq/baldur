"""Async admission — a tier slot is awaited, never waited for on the loop.

``TrafficGate.should_allow_async`` runs ``should_allow``'s pipeline with one
difference: the bulkhead step awaits ``try_acquire_async``. ``check_admission_async``
shares every admission step with ``check_admission`` except the gate call. A
decision carries the compartment its seat came from, so a release — the
admission closure, or the gate's own release when a later step rejects —
returns the seat there even if the tier's name was registered again.

Verification techniques applied:
- Parity: every pipeline exit gives the same decision from both entry points.
- Loop liveness: a ticker coroutine keeps running while admission waits for a
  saturated tier; a seat freed during the wait admits the request.
- Identity: a release after a re-``register()`` reaches the original
  compartment; the release closure is idempotent.

The tier registry, the admission settings and the PRO registry slot are
injected at their accessors; the gate, the registry and the compartments are
the real ones.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.middleware import admission as adm
from baldur.api.middleware.admission import (
    AdmissionDecision,
    check_admission,
    check_admission_async,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.scaling.config import BackpressureLevel
from baldur.scaling.rate_controller import RateController
from baldur.scaling.traffic_gate import TrafficDecision, TrafficGate
from baldur.services.bulkhead.registry import BulkheadRegistry
from baldur.services.bulkhead.semaphore import SemaphoreBulkhead
from baldur.settings.admission_control import AdmissionControlSettings

# Upper bound on any wait the test expects to end.
_WAIT_S = 5.0
_TICK_S = 0.01
# The critical tier's admission wait (the settings' upper bound is 1.0 s).
_TIER_WAIT_S = 0.3

_TIER_NAME = "tier:critical"


def _rate_controller(*, process: bool = True) -> MagicMock:
    controller = MagicMock(spec=RateController)
    controller.get_state.return_value = SimpleNamespace(level=BackpressureLevel.NONE)
    controller.should_process.return_value = process
    return controller


class _Shedding:
    """A load-shedding stage that accepts or rejects, optionally running a
    side action first."""

    def __init__(self, accept: bool, before: Any = None) -> None:
        self._accept = accept
        self._before = before

    def should_accept(self, priority: int = 0, **_: Any) -> dict[str, bool]:
        if self._before is not None:
            self._before()
        return {"accepted": self._accept}


@contextmanager
def _registry_resolved(registry: BulkheadRegistry) -> Generator[None, None, None]:
    with patch(
        "baldur.services.bulkhead.registry.get_bulkhead_registry",
        return_value=registry,
    ):
        yield


async def _eventually(predicate, timeout: float = _WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(_TICK_S / 5)
    return predicate()


def _decision_fields(decision: TrafficDecision) -> tuple[Any, ...]:
    return (
        decision.allowed,
        decision.gate,
        decision.reason,
        decision.bulkhead_acquired,
        decision.bulkhead_name,
    )


# =============================================================================
# TrafficGate.should_allow_async
# =============================================================================


def _gate_for(step: str, registry: BulkheadRegistry) -> TrafficGate:
    if step == "load_shedding":
        return TrafficGate(
            rate_controller=_rate_controller(), load_shedding=_Shedding(False)
        )
    if step == "rate":
        return TrafficGate(rate_controller=_rate_controller(process=False))
    return TrafficGate(rate_controller=_rate_controller())


class TestTrafficGateAsyncBehavior:
    """The async pipeline is the sync pipeline with an awaited bulkhead step."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("step", "expected_gate", "taken_after"),
        [
            ("deadline", "DeadlineContext", 0),
            ("bulkhead_full", "Bulkhead", 1),
            ("load_shedding", "CascadeLoadShedding", 0),
            ("rate", "RateController", 0),
            ("allow", "TrafficGate", 1),
        ],
        ids=["deadline", "bulkhead_full", "load_shedding", "rate", "allow"],
    )
    async def test_every_pipeline_exit_matches_sync_entry_point(
        self, step, expected_gate, taken_after
    ):
        """Same decision, same seat accounting, from both entry points."""
        outcomes = []
        for entry in ("sync", "async"):
            # Given — a fresh compartment and gate for each entry point.
            registry = BulkheadRegistry()
            compartment = registry.get_or_create(_TIER_NAME, max_concurrent=1)
            if step == "bulkhead_full":
                assert compartment.try_acquire() is True
            gate = _gate_for(step, registry)
            kwargs = {"priority": 0, "bulkhead_name": _TIER_NAME}

            # When
            with (
                _registry_resolved(registry),
                patch(
                    "baldur.scaling.deadline_context.is_expired",
                    return_value=step == "deadline",
                ),
            ):
                if entry == "sync":
                    decision = gate.should_allow(**kwargs)
                else:
                    decision = await gate.should_allow_async(**kwargs)

            # Then — record what each entry point decided and left taken.
            outcomes.append(
                (_decision_fields(decision), compartment.get_state().active_count)
            )
            assert decision.gate == expected_gate
            assert compartment.get_state().active_count == taken_after
            assert (decision.bulkhead is compartment) is (step == "allow")
        assert outcomes[0] == outcomes[1]

    @pytest.mark.asyncio
    async def test_bulkhead_wait_is_awaited_and_admits_seat_freed_meanwhile(self):
        # Given — the tier full; the gate waits up to its bound.
        registry = BulkheadRegistry()
        compartment = registry.get_or_create(_TIER_NAME, max_concurrent=1)
        assert compartment.try_acquire() is True
        gate = TrafficGate(rate_controller=_rate_controller())

        with _registry_resolved(registry):
            waiting = asyncio.create_task(
                gate.should_allow_async(
                    bulkhead_name=_TIER_NAME, bulkhead_timeout=_WAIT_S
                )
            )
            assert await _eventually(lambda: compartment.get_state().waiting_count == 1)

            # When — the holder releases while the request waits on this loop.
            compartment.release()
            decision = await asyncio.wait_for(waiting, _WAIT_S)

        # Then
        assert decision.allowed is True
        assert decision.bulkhead is compartment
        gate.release_acquired(decision)
        assert compartment.get_state().active_count == 0

    @pytest.mark.asyncio
    async def test_release_acquired_reaches_acquired_compartment_after_reregister(self):
        # Given — a seat taken, then the tier's name registered again.
        registry = BulkheadRegistry()
        original = registry.get_or_create(_TIER_NAME, max_concurrent=1)
        gate = TrafficGate(rate_controller=_rate_controller())
        with _registry_resolved(registry):
            decision = await gate.should_allow_async(bulkhead_name=_TIER_NAME)
        replacement = SemaphoreBulkhead(_TIER_NAME, max_concurrent=1)
        assert replacement.try_acquire() is True
        registry.register(replacement)

        # When
        with _registry_resolved(registry):
            gate.release_acquired(decision)

        # Then
        assert original.get_state().active_count == 0
        assert replacement.get_state().active_count == 1

    @pytest.mark.asyncio
    async def test_later_reject_releases_seat_to_acquired_compartment(self):
        """Load shedding rejects after the name was re-registered: the seat goes home."""
        # Given — shedding swaps the compartment under the name, then rejects.
        registry = BulkheadRegistry()
        original = registry.get_or_create(_TIER_NAME, max_concurrent=1)
        replacement = SemaphoreBulkhead(_TIER_NAME, max_concurrent=1)
        assert replacement.try_acquire() is True
        gate = TrafficGate(
            rate_controller=_rate_controller(),
            load_shedding=_Shedding(
                False, before=lambda: registry.register(replacement)
            ),
        )

        # When
        with _registry_resolved(registry):
            decision = await gate.should_allow_async(bulkhead_name=_TIER_NAME)

        # Then
        assert decision.allowed is False
        assert decision.gate == "CascadeLoadShedding"
        assert original.get_state().active_count == 0
        assert replacement.get_state().active_count == 1

    def test_release_acquired_without_seat_is_noop(self):
        registry = BulkheadRegistry()
        compartment = registry.get_or_create(_TIER_NAME, max_concurrent=1)
        assert compartment.try_acquire() is True
        gate = TrafficGate(rate_controller=_rate_controller())
        decision = TrafficDecision(
            allowed=True,
            reason="Allowed",
            level=BackpressureLevel.NONE,
            gate="TrafficGate",
            bulkhead_acquired=False,
            bulkhead_name=None,
        )

        with _registry_resolved(registry):
            gate.release_acquired(decision)

        assert compartment.get_state().active_count == 1

    def test_release_acquired_with_name_only_releases_registered_compartment(self):
        """A decision built without the compartment falls back to the name."""
        registry = BulkheadRegistry()
        compartment = registry.get_or_create(_TIER_NAME, max_concurrent=1)
        assert compartment.try_acquire() is True
        gate = TrafficGate(rate_controller=_rate_controller())
        decision = TrafficDecision(
            allowed=True,
            reason="Allowed",
            level=BackpressureLevel.NONE,
            gate="TrafficGate",
            bulkhead_acquired=True,
            bulkhead_name=_TIER_NAME,
        )

        with _registry_resolved(registry):
            gate.release_acquired(decision)

        assert compartment.get_state().active_count == 0


# =============================================================================
# check_admission_async
# =============================================================================


class _PathTiers:
    """Classifies a request into a tier by its path."""

    def __init__(self, tiers: dict[str, str]) -> None:
        self._tiers = tiers

    def resolve_tier_with_fallback(self, **request: Any) -> SimpleNamespace:
        return SimpleNamespace(tier_id=self._tiers.get(request["path"], "standard"))


@contextmanager
def _pro_admission(
    registry: BulkheadRegistry | None,
    gate: Any,
    settings: AdmissionControlSettings | None = None,
) -> Generator[None, None, None]:
    """Run the PRO admission path over a real gate and registry."""
    admission_settings = settings or AdmissionControlSettings(
        enabled=True,
        tier_critical_max_concurrent=1,
        tier_critical_bulkhead_timeout_seconds=_TIER_WAIT_S,
    )
    with (
        patch.object(adm, "_get_admission_settings", return_value=admission_settings),
        patch.object(adm, "_bulkhead_registry", return_value=registry),
        patch(
            "baldur.services.bulkhead.registry.get_bulkhead_registry",
            return_value=registry,
        ),
        patch("baldur.scaling.traffic_gate.get_traffic_gate", return_value=gate),
        patch(
            "baldur.scaling.tiering.get_tier_registry",
            return_value=_PathTiers({"/pay/": "critical"}),
        ),
        patch("baldur.context.cell_context.get_current_cell_id", return_value=None),
    ):
        yield


def _critical_request(method: HttpMethod = HttpMethod.GET) -> RequestContext:
    return RequestContext(method=method, path="/pay/", client_ip="203.0.113.5")


def _saturated_critical_tier() -> tuple[BulkheadRegistry, Any]:
    registry = BulkheadRegistry()
    compartment = registry.get_or_create(_TIER_NAME, max_concurrent=1)
    assert compartment.try_acquire() is True
    return registry, compartment


class TestAdmissionAsyncBehavior:
    """check_admission_async waits for a tier slot without blocking the loop."""

    @pytest.mark.asyncio
    async def test_saturated_tier_wait_leaves_loop_serving_then_rejects(self):
        # Given — the critical tier full; a ticker shares the loop.
        registry, compartment = _saturated_critical_tier()
        gate = TrafficGate(rate_controller=_rate_controller())
        ticks = {"n": 0}

        async def _ticker() -> None:
            while True:
                ticks["n"] += 1
                await asyncio.sleep(_TICK_S)

        ticker = asyncio.create_task(_ticker())
        await asyncio.sleep(0)
        ticks_before = ticks["n"]

        # When — the request waits out the tier's bound.
        with _pro_admission(registry, gate):
            started = time.monotonic()
            decision = await check_admission_async(_critical_request())
            waited = time.monotonic() - started
        ticker.cancel()

        # Then — rejected after the wait, and the loop kept ticking meanwhile.
        assert decision.active is True
        assert decision.rejection is not None
        assert decision.rejection.status_code == 503
        assert decision.release is None
        assert ticks["n"] - ticks_before >= 5
        assert waited >= _TIER_WAIT_S / 2
        assert compartment.get_state().waiting_count == 0

    @pytest.mark.asyncio
    async def test_seat_freed_during_wait_admits_and_release_returns_it_once(self):
        # Given
        registry, compartment = _saturated_critical_tier()
        gate = TrafficGate(rate_controller=_rate_controller())
        settings = AdmissionControlSettings(
            enabled=True,
            tier_critical_max_concurrent=1,
            tier_critical_bulkhead_timeout_seconds=1.0,
        )

        with _pro_admission(registry, gate, settings):
            waiting = asyncio.create_task(check_admission_async(_critical_request()))
            assert await _eventually(lambda: compartment.get_state().waiting_count == 1)

            # When — the holder finishes while the request waits.
            compartment.release()
            decision = await asyncio.wait_for(waiting, _WAIT_S)

            # Then — admitted on the freed seat; two releases give back one.
            assert decision.active is True
            assert decision.rejection is None
            assert decision.tier_id == "critical"
            assert compartment.get_state().active_count == 1
            decision.release()
            decision.release()
        assert compartment.get_state().active_count == 0

    @pytest.mark.asyncio
    async def test_release_after_reregister_returns_seat_to_original_compartment(self):
        """SC4: the slot goes back to the compartment it was taken from."""
        registry = BulkheadRegistry()
        gate = TrafficGate(rate_controller=_rate_controller())
        with _pro_admission(registry, gate):
            decision = await check_admission_async(_critical_request())
            original = registry.get(_TIER_NAME)
            assert original.get_state().active_count == 1
            replacement = SemaphoreBulkhead(_TIER_NAME, max_concurrent=1)
            assert replacement.try_acquire() is True
            registry.register(replacement)

            decision.release()

        assert original.get_state().active_count == 0
        assert replacement.get_state().active_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "enabled", "pro_present"),
        [
            (HttpMethod.OPTIONS, True, True),
            (HttpMethod.GET, False, True),
            (HttpMethod.GET, True, False),
        ],
        ids=["options_preflight", "disabled", "oss_no_registry"],
    )
    async def test_early_exits_match_sync_entry_point(
        self, method, enabled, pro_present
    ):
        registry = BulkheadRegistry() if pro_present else None
        gate = MagicMock(spec=TrafficGate)
        settings = AdmissionControlSettings(enabled=enabled)

        with _pro_admission(registry, gate, settings):
            sync_decision = check_admission(_critical_request(method))
            async_decision = await check_admission_async(_critical_request(method))

        assert async_decision == sync_decision
        assert async_decision.active is False
        gate.should_allow.assert_not_called()
        gate.should_allow_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate_error_fails_open(self):
        gate = MagicMock(spec=TrafficGate)
        gate.should_allow_async.side_effect = RuntimeError("gate down")

        with _pro_admission(BulkheadRegistry(), gate):
            decision = await check_admission_async(_critical_request())

        assert decision == AdmissionDecision(active=False)

    @pytest.mark.asyncio
    async def test_gate_call_receives_tier_bound_and_metadata(self):
        """The async gate call gets the per-tier name, bound and metadata."""
        gate = MagicMock(spec=TrafficGate)
        gate.should_allow_async.return_value = TrafficDecision(
            allowed=True,
            reason="Allowed",
            level=BackpressureLevel.NONE,
            gate="TrafficGate",
        )

        with _pro_admission(BulkheadRegistry(), gate):
            decision = await check_admission_async(_critical_request())

        gate.should_allow_async.assert_awaited_once_with(
            priority=adm.TIER_PRIORITY_MAP["critical"],
            bulkhead_name=_TIER_NAME,
            bulkhead_timeout=_TIER_WAIT_S,
            metadata={"tier_id": "critical"},
        )
        gate.should_allow.assert_not_called()
        assert decision.release is None
