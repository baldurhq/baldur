"""Admin route registry — maps (method, path) to HandlerFunc + permission.

Routes support literal paths (``"/health"``) and single-segment path
parameters (``"/cb/{name}"``) — enough for every admin handler. More
elaborate routing (nested params, regex) is explicitly out of scope for
the admin UI.

Per-domain route registrations live in :mod:`baldur.api.admin.routes` —
each module there defines a single ``_register_<domain>_routes`` function.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from urllib.parse import unquote

import structlog

from baldur.interfaces.web_framework import (
    HandlerFunc,
    HttpMethod,
    PermissionLevel,
)
from baldur.utils.singleton import make_singleton_factory

logger = structlog.get_logger()

__all__ = [
    "AdminRoute",
    "AdminRegistry",
    "configure_admin_registry",
    "get_admin_registry",
    "register_admin_route",
    "reset_admin_registry",
]


_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _compile_path(path: str) -> tuple[re.Pattern[str], tuple[str, ...]]:
    """Compile ``/cb/{name}`` style path into a regex + ordered param names.

    Path parameters match a single URL segment (no ``/`` inside). Literal
    slashes separate segments.
    """
    param_names: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        param_names.append(match.group(1))
        return r"(?P<" + match.group(1) + r">[^/]+)"

    pattern = "^" + _PARAM_RE.sub(_sub, path) + "$"
    return re.compile(pattern), tuple(param_names)


@dataclass(frozen=True)
class AdminRoute:
    """A single admin-server route.

    Attributes:
        method: HTTP method.
        path: Path template with optional ``{param}`` segments.
        handler: Framework-agnostic handler function.
        permission_level: Required permission to invoke the handler.
    """

    method: HttpMethod
    path: str
    handler: HandlerFunc
    permission_level: PermissionLevel = PermissionLevel.VIEWER
    _regex: re.Pattern[str] = field(init=False, repr=False, compare=False)
    _param_names: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        regex, params = _compile_path(self.path)
        object.__setattr__(self, "_regex", regex)
        object.__setattr__(self, "_param_names", params)

    def match(self, method: str, path: str) -> dict[str, str] | None:
        """Return path params dict if route matches, else None.

        Captured segments are percent-decoded: the client encodes a path
        param with ``encodeURIComponent`` (the console JS does so for DLQ
        ids), so a Redis-adapter composite id like ``host:pid:hash:seq``
        arrives as ``host%3Apid%3A...`` and must be decoded back before it
        reaches ``get_by_id`` — otherwise every per-id lookup 404s.
        """
        if method != self.method.value:
            return None
        m = self._regex.match(path)
        if m is None:
            return None
        return {name: unquote(m.group(name)) for name in self._param_names}


class AdminRegistry:
    """Thread-safe registry of admin routes.

    All built-in domain routes are pre-registered on first singleton
    construction via :mod:`baldur.api.admin.routes`. Additional routes can
    be wired at runtime via :func:`register_admin_route`.
    """

    def __init__(self) -> None:
        self._routes: list[AdminRoute] = []
        self._lock = threading.RLock()

    def register(self, route: AdminRoute) -> None:
        """Register a route. Later registrations for the same (method, path)
        replace earlier ones — handler-module reloads stay idempotent."""
        with self._lock:
            for i, existing in enumerate(self._routes):
                if existing.method == route.method and existing.path == route.path:
                    self._routes[i] = route
                    return
            self._routes.append(route)

    def resolve(
        self, method: str, path: str
    ) -> tuple[AdminRoute, dict[str, str]] | None:
        """Find first matching route + extract path params."""
        with self._lock:
            for route in self._routes:
                params = route.match(method, path)
                if params is not None:
                    return route, params
        return None

    def all_routes(self) -> list[AdminRoute]:
        """Snapshot of registered routes — for diagnostics."""
        with self._lock:
            return list(self._routes)

    def clear(self) -> None:
        """Remove all registered routes — test isolation only."""
        with self._lock:
            self._routes.clear()


def _create_admin_registry() -> AdminRegistry:
    """Create an :class:`AdminRegistry` with every domain's routes pre-wired."""
    # Lazy import: routes/__init__.py imports AdminRegistry from this module,
    # so the import lives inside the factory to break the cycle.
    from baldur.api.admin.routes import register_all_routes

    reg = AdminRegistry()
    register_all_routes(reg)
    return reg


get_admin_registry, configure_admin_registry, reset_admin_registry = (
    make_singleton_factory("admin_registry", _create_admin_registry)
)


def register_admin_route(
    method: HttpMethod | str,
    path: str,
    handler: HandlerFunc,
    permission_level: PermissionLevel = PermissionLevel.VIEWER,
) -> None:
    """Convenience wrapper: ``register_admin_route("GET", "/health", health_check)``."""
    method_enum = method if isinstance(method, HttpMethod) else HttpMethod(method)
    get_admin_registry().register(
        AdminRoute(
            method=method_enum,
            path=path,
            handler=handler,
            permission_level=permission_level,
        )
    )
