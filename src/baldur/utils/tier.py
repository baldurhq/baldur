"""
Tier Utilities.

Canonical PRO-wheel presence probe for the baldur system. Every surface that
must compose differently on an OSS-only install (beat lane entries, admin route
registration, default scheduler jobs) resolves through ``is_pro_installed``.

Presence vs entitlement:
    ``is_pro_installed()`` answers *"is the PRO distribution importable?"* — a
    static packaging fact, resolved without importing any PRO symbol and
    therefore independent of import ordering and registry state. It is **not**
    an entitlement or registration check: a wheel that is installed but whose
    services never register still probes ``True``. Callers that need the
    resolved backing tier (which implementation actually answers a request)
    want the DLQ capture backing's tier resolver instead.
"""

from __future__ import annotations

import importlib.util


def is_pro_installed() -> bool:
    """Return whether the PRO distribution is importable in this environment.

    Non-raising by construction: the probe targets a top-level module name, so
    ``find_spec`` performs no parent-package traversal and returns ``None``
    instead of raising when the package is absent.
    """
    return importlib.util.find_spec("baldur_pro") is not None


__all__ = ["is_pro_installed"]
