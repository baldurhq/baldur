"""BulkheadDetailView tests — the real resolution chain on a bare install.

The registry-absent 404 branch is gone (the chain always resolves); only
the unknown-name 404 contract remains. The provider slot is forced empty
via the documented mock point so the chain exercises its base-singleton
fallback leg — no registry mocking.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import pytest

from baldur.services.bulkhead.base import Bulkhead, BulkheadState, BulkheadType

pytest.importorskip("django")

import django

django.setup()


@pytest.fixture(autouse=True)
def _bare_install_chain(monkeypatch):
    """Force the chain's fallback leg and a fresh base singleton."""
    from baldur.factory.registry import ProviderRegistry
    from baldur.services.bulkhead.registry import reset_bulkhead_registry

    monkeypatch.setattr(
        ProviderRegistry.bulkhead_registry, "safe_get", lambda name=None: None
    )
    reset_bulkhead_registry()
    yield
    reset_bulkhead_registry()


def _make_get(path="/api/baldur/bulkhead/"):
    """Create a Django GET request."""
    from django.test import RequestFactory

    return RequestFactory().get(path)


class TestBulkheadDetailViewBareInstallBehavior:
    """BulkheadDetailView through the un-mocked chain (slot empty)."""

    def test_builtin_name_returns_200_with_state_fields(self):
        """A built-in compartment resolves to 200 with its state payload."""
        from baldur.api.django.views.bulkhead import BulkheadDetailView

        response = BulkheadDetailView.as_view()(_make_get(), name="database")

        assert response.status_code == 200
        assert response.data["name"] == "database"
        # The base tier builds semaphore compartments (slot forced empty,
        # so this holds on any install).
        assert response.data["type"] == "semaphore"
        assert response.data["active_count"] == 0
        assert response.data["rejected_count"] == 0

    def test_unknown_name_returns_404_with_available_bulkheads(self):
        """The unknown-name 404 branch stays; payload lists registered names."""
        from baldur.api.django.views.bulkhead import BulkheadDetailView
        from baldur.core.connection_health import ConnectionType

        response = BulkheadDetailView.as_view()(_make_get(), name="no_such_domain")

        assert response.status_code == 404
        available = response.data["available_bulkheads"]
        assert {ct.value for ct in ConnectionType} <= set(available)


class TestBulkheadDetailViewQueueSizeContract:
    """The detail payload carries the compartment's ``queue_size`` (805 D5)."""

    def test_detail_payload_carries_compartment_queue_size(self):
        from baldur.api.django.views.bulkhead import BulkheadDetailView
        from baldur.services.bulkhead.registry import get_bulkhead_registry

        get_bulkhead_registry().register(_QueuedCompartment())

        response = BulkheadDetailView.as_view()(_make_get(), name="queued")

        assert response.status_code == 200
        assert response.data["queue_size"] == 7
        assert response.data["max_concurrent"] == 12

    def test_semaphore_detail_payload_reports_zero_queue(self):
        from baldur.api.django.views.bulkhead import BulkheadDetailView

        response = BulkheadDetailView.as_view()(_make_get(), name="database")

        assert response.data["queue_size"] == 0


class _QueuedCompartment(Bulkhead):
    """A compartment whose state reports a waiting queue."""

    @property
    def name(self) -> str:
        return "queued"

    @contextmanager
    def acquire(self, timeout: float | None = None) -> Generator[None, None, None]:
        yield

    def try_acquire(self, timeout: float | None = None) -> bool:
        return True

    def release(self) -> None:
        return None

    def get_state(self) -> BulkheadState:
        return BulkheadState(
            name="queued",
            bulkhead_type=BulkheadType.THREAD_POOL,
            max_concurrent=12,
            active_count=0,
            waiting_count=0,
            rejected_count=0,
            queue_size=7,
        )
