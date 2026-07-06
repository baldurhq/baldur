"""
Django Session Signal Hooks for Baldur System.

Automatically maintains the session_key reverse-mapping in UserSessionRegistry
via the Django user_logged_in / user_logged_out signals.

Connection model:
    BaldurConfig.ready() calls connect_session_signals(), which automatically
    wires up the Django signals. Host apps do not need to add any code.

Architecture:
    Same pattern as adapters/celery/signal_hooks.py (Celery signals).
    The baldur package handles Django authentication signals at the adapter
    layer, so host apps do not need to write signal handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from django.http import HttpRequest

from baldur.services.security.session_registry import get_user_session_registry

logger = structlog.get_logger()

_connected = False


def on_user_login_register_session(
    sender: Any, request: HttpRequest, user: Any, **kwargs: Any
) -> None:
    """
    Register the session_key mapping in UserSessionRegistry on login.

    The Redis session backend cannot perform a reverse lookup from
    user_id -> session_key. Registering the mapping at login time enables
    that reverse lookup during session invalidation.
    """
    session_key = request.session.session_key
    if not session_key:
        request.session.save()
        session_key = request.session.session_key

    if session_key and user and user.pk:
        registry = get_user_session_registry()
        registry.register(user.pk, session_key)


def on_user_logout_unregister_session(
    sender: Any, request: HttpRequest, user: Any, **kwargs: Any
) -> None:
    """
    Remove the session_key mapping from UserSessionRegistry on logout.

    Removes the session_key mapping registered at login time to prevent
    unnecessary invalidation attempts.
    """
    session_key = getattr(request.session, "session_key", None)
    if session_key and user and user.pk:
        registry = get_user_session_registry()
        registry.unregister(user.pk, session_key)


def connect_session_signals() -> None:
    """
    Connect the Django session signal handlers.

    Called from BaldurConfig.ready().
    dispatch_uid prevents duplicate connections.
    """
    global _connected
    if _connected:
        return

    from django.contrib.auth.signals import user_logged_in, user_logged_out

    user_logged_in.connect(
        on_user_login_register_session,
        dispatch_uid="baldur_session_register",
    )
    user_logged_out.connect(
        on_user_logout_unregister_session,
        dispatch_uid="baldur_session_unregister",
    )
    _connected = True
    logger.debug("baldur.session_signal_handlers_connected")


def disconnect_session_signals() -> None:
    """
    Disconnect the Django session signal handlers (for tests).
    """
    global _connected
    from django.contrib.auth.signals import user_logged_in, user_logged_out

    user_logged_in.disconnect(dispatch_uid="baldur_session_register")
    user_logged_out.disconnect(dispatch_uid="baldur_session_unregister")
    _connected = False


def is_session_signals_connected() -> bool:
    """Check whether the session signals are connected."""
    return _connected
