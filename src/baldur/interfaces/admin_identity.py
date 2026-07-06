"""Admin Identity Resolver Interface (537 OSS->PRO boundary).

OSS-side contract for resolving an authenticated operator identity on the
framework-free admin transport (``api/admin/server.py``). OSS ships no
resolver — the slot is empty and ``resolve_actor`` records ``"anonymous"``
(the intentional single-operator OSS posture). PRO ships ``IdentityResolver``
(``baldur_pro.services.admin_identity``) which reads a trusted proxy-forwarded
identity header and returns an :class:`AdminPrincipal`.

Co-locates the boundary ``Protocol`` with its data type, mirroring
:mod:`baldur.interfaces.pool_monitor`. ``interfaces/`` is the established home
for OSS<->PRO boundary contracts and is in the OSS test support set.

The principal exposes only ``.username`` — the single attribute
:func:`baldur.api.handlers._common.resolve_actor` reads. Capability gating
(RBAC) is deferred (537 Out of Scope); the level/groups fields are additive
when the RBAC follow-on lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from baldur.interfaces.web_framework import RequestContext

__all__ = [
    "AdminIdentityResolver",
    "AdminPrincipal",
]


@dataclass(frozen=True)
class AdminPrincipal:
    """Authenticated admin operator identity.

    Minimal principal satisfying the ``resolve_actor`` contract — it exposes
    ``username``, the only attribute ``resolve_actor`` reads
    (``getattr(user, "username", None)``). Set onto ``RequestContext.user`` by
    the admin dispatch seam so audit records attribute control-plane actions
    to a real operator rather than ``"anonymous"``.

    Frozen: a principal is an immutable per-request fact, never mutated after
    resolution.
    """

    username: str


@runtime_checkable
class AdminIdentityResolver(Protocol):
    """Protocol for the PRO admin identity resolver.

    OSS leaves ``ProviderRegistry.admin_identity_resolver`` empty; PRO ships
    the concrete resolver. The admin dispatch consumes any implementation via
    ``ProviderRegistry.admin_identity_resolver.safe_get()``.

    Contract:
        * ``trusted=False`` MUST return ``None`` — the forwarded identity
          header is never trusted from a direct (unproxied) caller
          (fail-closed on identity).
        * An absent / empty / whitespace-only header MUST return ``None``
          (not a blank-username principal) so attribution degrades cleanly to
          ``"anonymous"``.
    """

    def resolve(
        self, ctx: RequestContext, *, trusted: bool
    ) -> AdminPrincipal | None: ...
