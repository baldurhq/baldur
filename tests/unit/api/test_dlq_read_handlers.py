"""Unit tests for the OSS DLQ handler resolution chain + route gating (708).

Covers the un-gating seam that makes the DLQ read + single-entry surface
OSS-available:

- ``_get_read_service()`` — registry-first (PRO under an ACTIVE entitlement),
  OSS ``DLQReadService`` fallback, and — unlike ``_get_service()`` — no
  ``RuntimeError`` exit (E1/E2, the un-gating).
- The OSS-set handlers (list / detail / facets / cleanup stats / retry /
  resolve / force-redrive) resolve through ``_get_read_service()`` and never
  raise the ``baldur_pro``-required ``RuntimeError`` with the slot empty; the
  four management handlers keep ``_get_service()`` and still raise it.
- Route gating (D8): the four PRO-only routes register only when the
  ``baldur_pro`` package is present (``find_spec``), so a pure-OSS deployment
  returns 404 (route absent) rather than the handler-layer 500.

"PRO absent" is simulated by clearing ``ProviderRegistry.dlq_service`` (the
registry-first pattern — deterministic whether or not ``baldur_pro`` is
installed), and PRO-installed by patching the ``find_spec`` probe.
"""

from __future__ import annotations

import importlib.util
from unittest.mock import patch

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.api.admin.registry import AdminRegistry
from baldur.api.admin.routes.dlq import _register_dlq_routes
from baldur.api.handlers.dlq import (
    _get_read_service,
    _get_service,
    dlq_cleanup_archive,
    dlq_cleanup_purge,
    dlq_cleanup_stats,
    dlq_detail,
    dlq_facets,
    dlq_force_redrive,
    dlq_list,
    dlq_replay,
    dlq_resolve,
    dlq_retry,
    dlq_test_create,
)
from baldur.factory.registry import ProviderRegistry
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel, RequestContext
from baldur.models.dlq import DLQConfig
from baldur.services.dlq_read import DLQReadService

REQUIRES_REVIEW = FailedOperationStatus.REQUIRES_REVIEW.value
RESOLVED = FailedOperationStatus.RESOLVED.value


# =============================================================================
# Helpers — request context, in-memory-backed read service
# =============================================================================


def _ctx(method: HttpMethod, path: str, *, pk=None, query=None, body=None):
    return RequestContext(
        method=method,
        path=path,
        query_params=query or {},
        path_params={"pk": pk} if pk is not None else {},
        json_body=body,
        user=None,
    )


@pytest.fixture
def read_service():
    """A real ``DLQReadService`` over an in-memory repository (audit stubbed)."""
    repo = InMemoryFailedOperationRepository()
    service = DLQReadService(
        config=DLQConfig(enabled=True, max_replay_attempts=2), repository=repo
    )
    service._log_dlq_audit = lambda **kwargs: None  # type: ignore[method-assign]
    return service, repo


@pytest.fixture
def slot_empty(monkeypatch):
    """Simulate PRO-absent: the ``dlq_service`` slot resolves to None."""
    monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)


# =============================================================================
# TestGetReadServiceResolver — E1/E2 exit inventory
# =============================================================================


class TestGetReadServiceResolver:
    """``_get_read_service()`` always resolves; ``_get_service()`` gates."""

    def test_e1_slot_registered_returns_pro_service(self, monkeypatch):
        """E1: a registered slot wins the chain (PRO entitled)."""
        sentinel = object()
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: sentinel)

        assert _get_read_service() is sentinel

    def test_e2_slot_empty_returns_oss_read_backing(self, slot_empty):
        """E2: an empty slot falls back to the OSS ``DLQReadService`` singleton."""
        from baldur.services.dlq_read import get_dlq_read_service

        resolved = _get_read_service()

        assert isinstance(resolved, DLQReadService)
        assert resolved is get_dlq_read_service()

    def test_read_resolver_never_raises_runtime_error_when_slot_empty(self, slot_empty):
        """The un-gating: no ``RuntimeError`` exit, unlike ``_get_service()``."""
        # Must not raise.
        assert _get_read_service() is not None

    def test_management_resolver_raises_runtime_error_when_slot_empty(self, slot_empty):
        """``_get_service()`` still gates the management surface PRO-only."""
        with pytest.raises(RuntimeError, match="baldur_pro"):
            _get_service()


# =============================================================================
# TestDlqReadHandlersSlotEmpty — the seven un-gated, the four still gated
# =============================================================================

# The seven OSS-set handlers + a minimal valid request for each. With the slot
# empty they must resolve via ``_get_read_service()`` (never RuntimeError).
_OSS_HANDLER_CASES = {
    "cleanup_stats": (
        dlq_cleanup_stats,
        lambda: _ctx(HttpMethod.GET, "/dlq/cleanup/stats"),
    ),
    "list": (dlq_list, lambda: _ctx(HttpMethod.GET, "/dlq/list")),
    "facets": (dlq_facets, lambda: _ctx(HttpMethod.GET, "/dlq/facets")),
    "detail": (dlq_detail, lambda: _ctx(HttpMethod.GET, "/dlq/1", pk="1")),
    "retry": (
        dlq_retry,
        lambda: _ctx(HttpMethod.POST, "/dlq/1/retry", pk="1", body={}),
    ),
    "resolve": (
        dlq_resolve,
        lambda: _ctx(HttpMethod.POST, "/dlq/1/resolve", pk="1", body={}),
    ),
    "force_redrive": (
        dlq_force_redrive,
        lambda: _ctx(
            HttpMethod.POST, "/dlq/1/force-redrive", pk="1", body={"reason": "r"}
        ),
    ),
}

# The four PRO-set management handlers + a body that passes validation and
# reaches ``_get_service()`` → RuntimeError with the slot empty.
_PRO_HANDLER_CASES = {
    "replay": (
        dlq_replay,
        lambda: _ctx(HttpMethod.POST, "/dlq/replay", body={"batch_size": 50}),
    ),
    "cleanup_archive": (
        dlq_cleanup_archive,
        lambda: _ctx(
            HttpMethod.POST, "/dlq/cleanup/archive", body={"older_than_days": 30}
        ),
    ),
    "cleanup_purge": (
        dlq_cleanup_purge,
        lambda: _ctx(HttpMethod.POST, "/dlq/cleanup/purge", body={"confirm": True}),
    ),
    "test_create": (
        dlq_test_create,
        lambda: _ctx(
            HttpMethod.POST,
            "/dlq/test/create",
            body={"domain": "d", "failure_type": "x"},
        ),
    ),
}


class TestDlqReadHandlersSlotEmpty:
    """The un-gating boundary, exercised through the real resolution chain."""

    @pytest.mark.parametrize("name", sorted(_OSS_HANDLER_CASES))
    def test_oss_handler_does_not_raise_runtime_error(
        self, name, slot_empty, read_service, monkeypatch
    ):
        """Each OSS-set handler resolves via ``_get_read_service()`` — the real
        chain (slot empty → OSS backing), so no ``RuntimeError`` is raised."""
        service, _ = read_service
        # Inject our in-memory-backed backing as the OSS fallback so the real
        # ``_get_read_service()`` body runs (safe_get → None → this service).
        monkeypatch.setattr(
            "baldur.services.dlq_read.get_dlq_read_service", lambda: service
        )
        handler, build_ctx = _OSS_HANDLER_CASES[name]

        resp = handler(build_ctx())  # must not raise RuntimeError

        # A response object came back (a 200/404/409, never the RuntimeError-500).
        assert hasattr(resp, "status_code")
        assert resp.status_code != 500

    @pytest.mark.parametrize("name", sorted(_PRO_HANDLER_CASES))
    def test_management_handler_raises_runtime_error(self, name, slot_empty):
        """Each PRO-set handler still hits ``_get_service()`` → RuntimeError."""
        handler, build_ctx = _PRO_HANDLER_CASES[name]

        with pytest.raises(RuntimeError, match="baldur_pro"):
            handler(build_ctx())

    def test_retry_state_conflict_maps_to_409(
        self, slot_empty, read_service, monkeypatch
    ):
        """An at-cap retry surfaces ``DLQStateConflictError`` → 409, not a crash."""
        service, repo = read_service
        entry = repo.create(
            domain="d", failure_type="poison", retry_count=2, max_retries=2
        )
        repo.update_status(entry.id, status=REQUIRES_REVIEW)
        monkeypatch.setattr(
            "baldur.services.dlq_read.get_dlq_read_service", lambda: service
        )

        resp = dlq_retry(
            _ctx(HttpMethod.POST, f"/dlq/{entry.id}/retry", pk=entry.id, body={})
        )

        assert resp.status_code == 409
        # State conflict left the entry unchanged.
        assert repo.get_by_id(entry.id).status == REQUIRES_REVIEW

    def test_resolve_already_resolved_maps_to_409(
        self, slot_empty, read_service, monkeypatch
    ):
        service, repo = read_service
        entry = repo.create(domain="d", failure_type="x")
        repo.update_status(entry.id, status=RESOLVED)
        monkeypatch.setattr(
            "baldur.services.dlq_read.get_dlq_read_service", lambda: service
        )

        resp = dlq_resolve(
            _ctx(HttpMethod.POST, f"/dlq/{entry.id}/resolve", pk=entry.id, body={})
        )

        assert resp.status_code == 409

    def test_retry_missing_entry_maps_to_404(
        self, slot_empty, read_service, monkeypatch
    ):
        service, _ = read_service
        monkeypatch.setattr(
            "baldur.services.dlq_read.get_dlq_read_service", lambda: service
        )

        resp = dlq_retry(_ctx(HttpMethod.POST, "/dlq/999/retry", pk="999", body={}))

        assert resp.status_code == 404


# =============================================================================
# TestDlqRouteGating — D8: PRO-only routes register only when PRO is installed
# =============================================================================

# (method, path) tuples for the two route tiers (D4/D8).
_OSS_ROUTES = {
    (HttpMethod.GET, "/dlq/cleanup/stats"),
    (HttpMethod.GET, "/dlq/list"),
    (HttpMethod.GET, "/dlq/facets"),
    (HttpMethod.GET, "/dlq/{pk}"),
    (HttpMethod.POST, "/dlq/{pk}/retry"),
    (HttpMethod.POST, "/dlq/{pk}/resolve"),
    (HttpMethod.POST, "/dlq/{pk}/force-redrive"),
}
_PRO_ROUTES = {
    (HttpMethod.POST, "/dlq/replay"),
    (HttpMethod.POST, "/dlq/cleanup/archive"),
    (HttpMethod.POST, "/dlq/cleanup/purge"),
    (HttpMethod.POST, "/dlq/test/create"),
}


def _registered_routes(*, pro_installed: bool) -> set[tuple[HttpMethod, str]]:
    """Register DLQ routes with the ``find_spec`` probe forced to a known state."""
    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name, *args, **kwargs):
        if name == "baldur_pro":
            return object() if pro_installed else None
        return real_find_spec(name, *args, **kwargs)

    registry = AdminRegistry()
    with patch.object(importlib.util, "find_spec", _fake_find_spec):
        _register_dlq_routes(registry)
    return {(r.method, r.path) for r in registry.all_routes()}


class TestDlqRouteGating:
    """The PRO-set four are absent in pure OSS (404), present when PRO installed."""

    def test_pro_absent_registers_only_the_seven_oss_routes(self):
        routes = _registered_routes(pro_installed=False)

        assert _OSS_ROUTES <= routes
        assert routes & _PRO_ROUTES == set()
        assert len(routes) == len(_OSS_ROUTES)

    def test_pro_installed_registers_all_eleven_routes(self):
        routes = _registered_routes(pro_installed=True)

        assert _OSS_ROUTES <= routes
        assert _PRO_ROUTES <= routes
        assert len(routes) == len(_OSS_ROUTES) + len(_PRO_ROUTES)

    def test_force_redrive_route_binds_at_admin(self):
        """Force-redrive is OSS (D7) but stays ADMIN-gated (permission ⟂ tier)."""
        registry = AdminRegistry()
        with patch.object(importlib.util, "find_spec", lambda name, *a, **k: None):
            _register_dlq_routes(registry)

        resolved = registry.resolve("POST", "/dlq/1/force-redrive")
        assert resolved is not None
        route, _params = resolved
        assert route.permission_level == PermissionLevel.ADMIN
