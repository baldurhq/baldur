"""Admin-server central ConfigLockError -> 409 ROLLOUT_CONFLICT mapping (685 D5).

Verification target: the plain admin server's ``_dispatch`` catch maps any
handler exception whose class NAME is ``ConfigLockError`` to a 409 with
``error_code="ROLLOUT_CONFLICT"`` and the message passed through (it names the
owning rollout id), while every other exception stays a 500 server fault. One
site covers every admin-route handler (editor, SLO, governance, drift,
chaos-safety).

PRO-absent safe (the OSS mirror runs this): the dispatch matches by class name,
so the test raises a stub exception *named* ``ConfigLockError`` with NO
``baldur_pro`` import — exactly the boundary the name-match exists to preserve.

Reference: 685 CANARY_CONFIG_LOCK_WRITER_ENFORCEMENT (D5)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.interfaces.web_framework import PermissionLevel, ResponseContext


class ConfigLockError(Exception):
    """Stub whose class NAME is ``ConfigLockError`` — stands in for the PRO
    exception without importing it, so this OSS test runs PRO-absent."""


def _dispatch_with_handler(route_handler, *, method: str = "PUT") -> ResponseContext:
    """Drive ``_AdminHTTPHandler._dispatch`` through a PUBLIC route whose handler
    is ``route_handler``, returning the ResponseContext written back.

    The handler is built via ``__new__`` (no socket) with just the attributes /
    seams ``_dispatch`` touches stubbed; auth + origin gates are patched open so
    the test isolates the exception-mapping branch.
    """
    from baldur.api.admin.server import _AdminHTTPHandler

    route = MagicMock()
    route.permission_level = PermissionLevel.PUBLIC
    route.handler = route_handler

    admin = MagicMock()
    admin.settings = MagicMock(trust_proxy=False, max_body_bytes=1_000_000)
    admin.registry.resolve.return_value = (route, {})

    handler = _AdminHTTPHandler.__new__(_AdminHTTPHandler)
    handler.server = MagicMock(_baldur_admin=admin)
    handler.path = "/config/retry/"
    handler.headers = {}
    handler.client_address = ("127.0.0.1", 5555)
    handler._read_body = lambda _max: b""
    handler._maybe_parse_json = lambda _body: {}
    written: list[ResponseContext] = []
    handler._write = written.append

    with (
        patch("baldur.api.admin.server._request_origin_allowed", return_value=True),
        patch(
            "baldur.api.admin.server.authenticate",
            return_value=MagicMock(authenticated=True, level=MagicMock(), reason=None),
        ),
        patch("baldur.api.admin.server.authorize", return_value=True),
        patch("baldur.api.admin.server._apply_admin_identity"),
    ):
        handler._dispatch(method)

    assert len(written) == 1
    return written[0]


class TestAdminServerRolloutConflictBehavior:
    def test_config_lock_error_maps_to_409_rollout_conflict(self):
        message = "Config 'retry' is locked by an active canary rollout 'roll-1'."

        def _raise_lock(_ctx):
            raise ConfigLockError(message)

        response = _dispatch_with_handler(_raise_lock)

        assert response.status_code == 409
        assert response.body["error_code"] == "ROLLOUT_CONFLICT"
        # The owning rollout id is passed through so the console can name it.
        assert "roll-1" in response.body["error"]

    def test_other_exception_stays_a_500_server_fault(self):
        """Only a name-matched ConfigLockError is a 409 — an unrelated error must
        not be misclassified as a client conflict."""

        def _raise_other(_ctx):
            raise ValueError("unexpected boom")

        response = _dispatch_with_handler(_raise_other)

        assert response.status_code == 500
        assert response.body["error_code"] == "INTERNAL_ERROR"

    def test_successful_handler_response_passes_through_untouched(self):
        def _ok(_ctx):
            return ResponseContext.json({"ok": True}, status_code=200)

        response = _dispatch_with_handler(_ok)

        assert response.status_code == 200
        assert response.body == {"ok": True}
