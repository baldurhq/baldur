"""Unit tests for shared handler helpers (``baldur.api.handlers._common``).

The ``resolve_actor`` contract is the single source of truth for the
``actor`` field in audit logs emitted by every framework-agnostic handler.
Divergence would fragment audit grep workflows across endpoints.
"""

from __future__ import annotations

from types import SimpleNamespace

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import HttpMethod, RequestContext


def _ctx(user=None) -> RequestContext:
    return RequestContext(
        method=HttpMethod.GET,
        path="/test/",
        query_params={},
        path_params={},
        json_body=None,
        user=user,
    )


class TestResolveActorContract:
    """resolve_actor() — stable audit-actor string across all handlers."""

    def test_returns_username_when_user_has_username(self):
        assert resolve_actor(_ctx(user=SimpleNamespace(username="alice"))) == "alice"

    def test_returns_anonymous_when_user_is_none(self):
        """Unauthenticated request -> 'anonymous', never 'api' or empty."""
        assert resolve_actor(_ctx(user=None)) == "anonymous"

    def test_returns_anonymous_when_user_has_no_username(self):
        """User object without .username attribute -> 'anonymous'."""
        assert resolve_actor(_ctx(user=SimpleNamespace())) == "anonymous"

    def test_returns_anonymous_when_username_is_empty_string(self):
        """Empty username string -> treated as missing -> 'anonymous'."""
        assert resolve_actor(_ctx(user=SimpleNamespace(username=""))) == "anonymous"

    def test_returns_anonymous_when_username_is_none(self):
        """Explicit None username -> 'anonymous'."""
        assert resolve_actor(_ctx(user=SimpleNamespace(username=None))) == "anonymous"

    def test_never_returns_framework_specific_repr(self):
        """Django AnonymousUser.__str__ returns 'AnonymousUser'; the helper
        must NOT leak that into audit logs."""

        class FakeAnonymousUser:
            def __str__(self) -> str:
                return "AnonymousUser"

        assert resolve_actor(_ctx(user=FakeAnonymousUser())) == "anonymous"
