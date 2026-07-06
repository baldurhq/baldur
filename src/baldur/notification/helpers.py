"""OSS-side thin wrappers for PRO unified_notification helpers (518 D4).

Provides a single, stable import target for OSS callsites that need to send
notifications. When ``baldur_pro.services.unified_notification`` is installed,
each wrapper delegates to the corresponding PRO function. When PRO is not
installed, each wrapper silently no-ops and returns ``None``.

Each wrapper accepts ``*args, **kwargs`` and forwards them verbatim — the
caller's exact argument shape is preserved into the PRO call. Consult
``src/baldur_pro/services/unified_notification/`` for parameter types and
defaults.

Scope (batch a)
---------------
Function-style convenience helpers only. Singleton getters
(``get_unified_notification_manager``, ``reset_notification_manager``) belong
to the (c) singletons batch; value-typed enums and DTOs (``NotificationPriority``,
``NotificationCategory``, ``NotificationPayload``, ``UnifiedNotificationManager``)
belong to the (d) types batch.

Test isolation
--------------
Tests that swap PRO presence or pop ``baldur_pro.services.unified_notification``
from ``sys.modules`` MUST reset the module-level cache via the
``reset_notification_helpers`` fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

from typing import Any

_pro: Any = None
_resolved: bool = False


def _get_pro() -> Any:
    """Return the cached :mod:`baldur_pro.services.unified_notification` module or ``None``."""
    global _pro, _resolved
    if not _resolved:
        try:
            import baldur_pro.services.unified_notification as _m

            _pro = _m
        except ImportError:
            _pro = None
        _resolved = True
    return _pro


# ============================================================
# Convenience notifiers
# ============================================================


def notify(*args: Any, **kwargs: Any) -> Any | None:
    """PRO: notify(title, message, priority='medium', category='operations', source='unknown', **kwargs)."""
    if (p := _get_pro()) is None:
        return None
    return p.notify(*args, **kwargs)


def notify_security(*args: Any, **kwargs: Any) -> Any | None:
    """PRO: notify_security(title, message, priority='high', source='security', **kwargs)."""
    if (p := _get_pro()) is None:
        return None
    return p.notify_security(*args, **kwargs)


def notify_sla(*args: Any, **kwargs: Any) -> Any | None:
    """PRO: notify_sla(title, message, domain, priority='medium', source='sla_monitor', **kwargs)."""
    if (p := _get_pro()) is None:
        return None
    return p.notify_sla(*args, **kwargs)


def notify_incident(*args: Any, **kwargs: Any) -> Any | None:
    """PRO: notify_incident(incident_id, incident_type, severity='high', description='', source_ip=None, user_id=None, action_taken='', **kwargs)."""
    if (p := _get_pro()) is None:
        return None
    return p.notify_incident(*args, **kwargs)


def notify_error(*args: Any, **kwargs: Any) -> Any | None:
    """PRO: notify_error(title, message, error, source='unknown', **kwargs)."""
    if (p := _get_pro()) is None:
        return None
    return p.notify_error(*args, **kwargs)


__all__ = [
    "notify",
    "notify_error",
    "notify_incident",
    "notify_security",
    "notify_sla",
]
