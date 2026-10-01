"""FastAPI admission waits for its tier slot without stopping the event loop.

A FastAPI app with ``BaldurMiddleware``, driven by ``httpx.AsyncClient`` over
ASGI on the test's loop: while a request waits for a slot in a saturated
critical tier, a request to another tier on the same loop completes; the
waiting request is admitted when a slot frees (or rejected with 503 at the
tier's bound), and its slot goes back to the tier when it finishes.

The tier registry, the admission settings and the PRO registry slot are
injected at their accessors; the middleware, the gate, the registry and the
compartments are the real ones.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from baldur.adapters.fastapi.middleware import BaldurMiddleware  # noqa: E402
from baldur.api.middleware import admission as adm  # noqa: E402
from baldur.scaling.rate_controller import RateController  # noqa: E402
from baldur.scaling.traffic_gate import TrafficGate  # noqa: E402
from baldur.services.bulkhead.registry import BulkheadRegistry  # noqa: E402
from baldur.settings.admission_control import AdmissionControlSettings  # noqa: E402

_WAIT_S = 5.0
_POLL_S = 0.002
# The critical tier's admission wait (the settings' upper bound is 1.0 s).
_TIER_WAIT_S = 1.0

_TIERS = {"/pay": "critical", "/catalog": "standard"}


class _PathTiers:
    def resolve_tier_with_fallback(self, **request: Any) -> SimpleNamespace:
        return SimpleNamespace(tier_id=_TIERS.get(request["path"], "standard"))


@contextmanager
def _pro_admission(registry: BulkheadRegistry) -> Generator[None, None, None]:
    settings = AdmissionControlSettings(
        enabled=True,
        tier_critical_max_concurrent=1,
        tier_critical_bulkhead_timeout_seconds=_TIER_WAIT_S,
    )
    gate = TrafficGate(rate_controller=RateController())
    with (
        patch.object(adm, "_get_admission_settings", return_value=settings),
        patch.object(adm, "_bulkhead_registry", return_value=registry),
        patch(
            "baldur.services.bulkhead.registry.get_bulkhead_registry",
            return_value=registry,
        ),
        patch("baldur.scaling.traffic_gate.get_traffic_gate", return_value=gate),
        patch("baldur.scaling.tiering.get_tier_registry", return_value=_PathTiers()),
        patch("baldur.context.cell_context.get_current_cell_id", return_value=None),
    ):
        yield


def _app() -> Any:
    app = fastapi.FastAPI()

    @app.get("/pay")
    async def pay() -> dict[str, str]:
        return {"route": "pay"}

    @app.get("/catalog")
    async def catalog() -> dict[str, str]:
        return {"route": "catalog"}

    app.add_middleware(BaldurMiddleware)
    return app


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[Any, None]:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _eventually(predicate, timeout: float = _WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(_POLL_S)
    return predicate()


class TestFastapiAdmissionLoopBehavior:
    """A saturated tier's admission wait never blocks other requests on the loop."""

    @pytest.mark.asyncio
    async def test_request_on_same_loop_completes_while_admission_waits(self, client):
        # Given — the critical tier's only slot is held.
        registry = BulkheadRegistry()
        tier = registry.get_or_create("tier:critical", max_concurrent=1)
        assert tier.try_acquire() is True

        with _pro_admission(registry):
            waiting = asyncio.create_task(client.get("/pay"))
            assert await _eventually(lambda: tier.get_state().waiting_count == 1)

            # When — another tier's request arrives while /pay waits.
            probe = await client.get("/catalog")
            pay_waiting_after_probe = not waiting.done()

            # Then — it completed during the wait; /pay is admitted on release.
            tier.release()
            pay = await asyncio.wait_for(waiting, _WAIT_S)

        assert probe.status_code == 200
        assert probe.json() == {"route": "catalog"}
        assert pay_waiting_after_probe is True
        assert pay.status_code == 200
        assert pay.json() == {"route": "pay"}
        assert tier.get_state().active_count == 0
        assert tier.get_state().waiting_count == 0

    @pytest.mark.asyncio
    async def test_saturated_tier_rejects_at_bound_while_loop_keeps_serving(
        self, client
    ):
        registry = BulkheadRegistry()
        tier = registry.get_or_create("tier:critical", max_concurrent=1)
        assert tier.try_acquire() is True

        with _pro_admission(registry):
            waiting = asyncio.create_task(client.get("/pay"))
            assert await _eventually(lambda: tier.get_state().waiting_count == 1)
            probes = [await client.get("/catalog") for _ in range(3)]
            pay_waiting_after_probes = not waiting.done()
            pay = await asyncio.wait_for(waiting, _WAIT_S)

        assert [p.status_code for p in probes] == [200, 200, 200]
        assert pay_waiting_after_probes is True
        assert pay.status_code == 503
        assert tier.get_state().active_count == 1  # the holder's slot only
        tier.release()
