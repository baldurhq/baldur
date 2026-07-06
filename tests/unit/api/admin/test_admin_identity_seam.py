"""Admin identity seam + client-IP dispatch tests — 537 G1/G6/D1/D5.

Verification targets (OSS side of the OSS<->PRO boundary):
- ``AdminPrincipal`` — the minimal frozen principal exposing ``.username``
  that ``resolve_actor`` reads.
- ``_apply_admin_identity(ctx, *, trusted)`` — the dispatch seam that pulls a
  resolver from ``ProviderRegistry.admin_identity_resolver`` and sets
  ``ctx.user``. No-op for OSS (empty slot); fail-open on resolver error.
- ``_dispatch`` end-to-end client-IP + actor resolution behind the
  ``trust_proxy`` gate, via a live ``ThreadingHTTPServer`` (test_server.py
  pattern: 127.0.0.1:0 ephemeral bind).

Verification techniques (UNIT_TEST_GUIDELINES §8):
- §8.6 immutability: ``AdminPrincipal`` is frozen.
- §8.8 state transition: ``ctx.user`` None -> principal / stays None.
- §8.2 + fail-open: a raising resolver degrades to ``"anonymous"`` without
  propagating (side-effect fail-open, CROSS_SERVICE_STANDARDS).
- §8.5 interaction: resolver invoked with the dispatch-computed ``trusted`` arg.

Resolver injection uses ``register()``/``set_default()`` (the ``resolver_slot``
fixture), NOT ``override()``/``snapshot()``. The registry's ``_instances`` cache
is a ``ContextVar``-backed property (``factory/base.py``): ``override`` binds it
in the calling Context, but a live ``ThreadingHTTPServer`` worker is a plain
thread that does not inherit that Context (it reads the process-shared default),
so an ``override`` would be invisible to the server thread. ``register`` mutates
the cross-thread-shared ``_providers``/``_default`` instead.

OSS tests use a local fake resolver (no ``baldur_pro`` import) — the real PRO
resolver is exercised in ``tests/pro``.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator

import pytest

from baldur.api.admin.registry import AdminRegistry, AdminRoute, reset_admin_registry
from baldur.api.admin.server import AdminServer, _apply_admin_identity
from baldur.api.handlers._common import resolve_actor
from baldur.factory.registry import ProviderRegistry
from baldur.interfaces.admin_identity import AdminPrincipal
from baldur.interfaces.web_framework import (
    HttpMethod,
    PermissionLevel,
    RequestContext,
    ResponseContext,
)
from baldur.settings.admin import AdminServerSettings

# =============================================================================
# Test doubles (OSS-local — no baldur_pro import)
# =============================================================================


class _FixedResolver:
    """Resolver that returns a fixed principal and records the ``trusted`` arg."""

    def __init__(self, principal: AdminPrincipal | None) -> None:
        self._principal = principal
        self.last_trusted: bool | None = None

    def resolve(self, ctx: RequestContext, *, trusted: bool) -> AdminPrincipal | None:
        self.last_trusted = trusted
        return self._principal


class _RaisingResolver:
    """Resolver that always raises — exercises the seam's fail-open path."""

    def resolve(self, ctx: RequestContext, *, trusted: bool) -> AdminPrincipal | None:
        raise RuntimeError("resolver boom")


def _ctx(path: str = "/emergency/trigger") -> RequestContext:
    return RequestContext(method=HttpMethod.POST, path=path, headers={})


@pytest.fixture
def resolver_slot() -> Iterator[Callable[[object], None]]:
    """Reset the admin_identity_resolver slot and yield a register helper.

    The slot starts empty (OSS posture). Calling the yielded helper registers a
    resolver under a unique name and makes it the default — visible to both the
    test (MainThread) and a live server's worker thread, because
    ``register``/``set_default`` touch the cross-thread-shared ``_providers`` /
    ``_default`` (see module docstring on why ``override`` would not work here).
    A unique provider name avoids stale process-shared instance-cache hits
    across tests.
    """
    slot = ProviderRegistry.admin_identity_resolver
    slot.reset()

    def _register(resolver: object) -> None:
        name = f"test_{id(resolver)}"
        slot.register(name, lambda: resolver)
        slot.set_default(name)

    yield _register

    slot.reset()


# =============================================================================
# AdminPrincipal — Contract
# =============================================================================


class TestAdminPrincipalContract:
    """AdminPrincipal shape contract (537 D4)."""

    def test_principal_exposes_username_for_resolve_actor(self):
        """resolve_actor reads ``.username`` off the principal set on ctx.user."""
        ctx = _ctx()
        ctx.user = AdminPrincipal(username="alice@corp.example")
        assert resolve_actor(ctx) == "alice@corp.example"

    def test_principal_is_frozen(self):
        """A principal is an immutable per-request fact (frozen dataclass)."""
        principal = AdminPrincipal(username="bob@corp.example")
        with pytest.raises(dataclasses.FrozenInstanceError):
            principal.username = "mallory@corp.example"  # type: ignore[misc]


# =============================================================================
# _apply_admin_identity — Behavior (pure, no live socket)
# =============================================================================


class TestApplyAdminIdentityBehavior:
    """Dispatch seam behavior — 537 G1/D1/D5."""

    def test_no_resolver_registered_leaves_user_none(self, resolver_slot):
        """OSS (empty slot): seam is a no-op, ctx.user stays None -> anonymous."""
        ctx = _ctx()

        _apply_admin_identity(ctx, trusted=True)

        assert ctx.user is None
        assert resolve_actor(ctx) == "anonymous"

    def test_resolver_returning_principal_sets_user(self, resolver_slot):
        """A resolved principal is assigned to ctx.user (state transition)."""
        ctx = _ctx()
        principal = AdminPrincipal(username="alice@corp.example")
        resolver_slot(_FixedResolver(principal))

        _apply_admin_identity(ctx, trusted=True)

        assert ctx.user is principal
        assert resolve_actor(ctx) == "alice@corp.example"

    def test_resolver_returning_none_leaves_user_none(self, resolver_slot):
        """A resolver that declines (None) leaves attribution at anonymous."""
        ctx = _ctx()
        resolver_slot(_FixedResolver(None))

        _apply_admin_identity(ctx, trusted=False)

        assert ctx.user is None
        assert resolve_actor(ctx) == "anonymous"

    def test_resolver_exception_is_fail_open_and_does_not_propagate(
        self, resolver_slot
    ):
        """A raising resolver degrades to anonymous without blocking the action.

        Side-effect fail-open (537 D5 / CROSS_SERVICE_STANDARDS): the request
        was already authorized; attribution must not raise.
        """
        ctx = _ctx()
        resolver_slot(_RaisingResolver())

        # Must not raise.
        _apply_admin_identity(ctx, trusted=True)

        assert ctx.user is None
        assert resolve_actor(ctx) == "anonymous"

    @pytest.mark.parametrize("trusted", [True, False], ids=["trusted", "untrusted"])
    def test_resolver_invoked_with_dispatch_trusted_flag(self, resolver_slot, trusted):
        """The seam forwards the dispatch-computed ``trusted`` flag to resolve()."""
        ctx = _ctx()
        resolver = _FixedResolver(None)
        resolver_slot(resolver)

        _apply_admin_identity(ctx, trusted=trusted)

        assert resolver.last_trusted is trusted


# =============================================================================
# Live-server dispatch — seam default + client_ip trust gate (G1/G6)
# =============================================================================


def _identity_echo_handler(ctx: RequestContext) -> ResponseContext:
    """Echo the resolved actor and client IP so the seam is observable over HTTP."""
    return ResponseContext.json(
        {"actor": resolve_actor(ctx), "client_ip": ctx.client_ip}
    )


def _get_json(
    server: AdminServer, path: str, headers: dict | None = None
) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{server.bound_port}{path}"
    req = urllib.request.Request(url)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.status, json.loads(exc.read())


@pytest.fixture
def whoami_registry() -> AdminRegistry:
    reg = AdminRegistry()
    reg.register(
        AdminRoute(
            HttpMethod.GET, "/whoami", _identity_echo_handler, PermissionLevel.PUBLIC
        )
    )
    return reg


@pytest.fixture
def make_server(whoami_registry):
    """Factory yielding started AdminServers; all are stopped on teardown."""
    servers: list[AdminServer] = []

    def _make(*, trust_proxy: bool = False) -> AdminServer:
        settings = AdminServerSettings(
            bind="127.0.0.1", port=0, trust_proxy=trust_proxy
        )
        server = AdminServer(settings=settings, registry=whoami_registry)
        server.start()
        servers.append(server)
        return server

    yield _make

    for server in servers:
        server.stop(timeout=2.0)
    reset_admin_registry()


class TestAdminIdentitySeamDispatchBehavior:
    """End-to-end identity seam over the live stdlib server (537 G1)."""

    def test_admin_identity_seam_without_resolver_records_anonymous(
        self, make_server, resolver_slot
    ):
        """OSS with no resolver registered: actor stays "anonymous"."""
        server = make_server(trust_proxy=True)

        status, payload = _get_json(
            server, "/whoami", headers={"X-Forwarded-Email": "alice@corp.example"}
        )

        assert status == 200
        assert payload["actor"] == "anonymous"

    def test_admin_identity_seam_with_resolver_records_operator(
        self, make_server, resolver_slot
    ):
        """A registered resolver populates ctx.user -> real operator in audit."""
        resolver = _FixedResolver(AdminPrincipal(username="alice@corp.example"))
        resolver_slot(resolver)

        server = make_server(trust_proxy=True)
        status, payload = _get_json(
            server, "/whoami", headers={"X-Forwarded-Email": "alice@corp.example"}
        )

        assert status == 200
        assert payload["actor"] == "alice@corp.example"
        assert resolver.last_trusted is True


class TestAdminClientIpDispatchBehavior:
    """client_ip resolution behind the trust_proxy gate (537 G6)."""

    def test_admin_client_ip_trusted_uses_forwarded_header(
        self, make_server, resolver_slot
    ):
        """trust_proxy=True: ctx.client_ip is the forwarded client, not the peer."""
        server = make_server(trust_proxy=True)

        status, payload = _get_json(
            server, "/whoami", headers={"X-Forwarded-For": "203.0.113.5"}
        )

        assert status == 200
        assert payload["client_ip"] == "203.0.113.5"

    def test_admin_client_ip_untrusted_uses_tcp_peer(self, make_server, resolver_slot):
        """trust_proxy=False (default): a forwarded header is ignored for IP."""
        server = make_server(trust_proxy=False)

        status, payload = _get_json(
            server, "/whoami", headers={"X-Forwarded-For": "203.0.113.5"}
        )

        assert status == 200
        assert payload["client_ip"] == "127.0.0.1"
