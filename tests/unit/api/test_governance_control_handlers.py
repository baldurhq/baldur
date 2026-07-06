"""Handler -> real-service regression for the 625 governance control fix.

Target: ``baldur.api.handlers.governance`` — ``governance_mode_set`` and
``governance_reconcile`` driven against a **real** in-process
``GovernanceApiService`` (PRO), with only the downstream singletons
(``get_reliability_manager``, ``get_emergency_tracker``,
``AuditLogger.get_instance``, ``get_metric_sync_service``) patched.

Why handler -> real service (no service mock): the existing
``tests/unit/services/governance/test_api_service.py`` exercises
``GovernanceApiService`` directly and (correctly) passes ``actor=`` — so it
never traverses the handler's wrong kwarg. The 625 ``actor_id=`` bug class was
therefore invisible to the suite. These tests close that gap by driving the
handler against a real service so the handler->service call contract itself is
exercised: the old ``actor_id=`` call site raised ``TypeError`` on every
request.

Coverage (625 D1/D2/D6):
  - D1 (§8.2 exception-absence): no ``TypeError`` on the ``actor=`` path
    (``governance_mode_set`` + ``governance_reconcile``).
  - D2 (§8.1 boundary / equivalence): STRICT escalation requires a non-empty
    string reason — empty / whitespace-only / non-string reason -> HTTP 400,
    case-insensitive (``STRICT`` and ``strict``), before the service is called.
  - D2 (§8.4 side effect): a valid STRICT reason is recorded **verbatim** in
    the audit event, not the empty-reason default.
  - D6 (§8.2 error path): a non-string ``mode`` -> 400 (not an
    ``AttributeError`` 500); an unrecognized mode **value** -> 400 via the
    handler's ``ValueError`` catch (not an unhandled 500).

Placement: stays under ``tests/`` (import-graph SUT is the OSS handler:
3 ``baldur.*`` imports vs the single in-fixture ``baldur_pro`` reset import ->
``true_boundary`` per ``scripts/classify_pro_importing_tests.py``). Gated with
``pytest.importorskip`` + ``requires_pro`` (G19); the ``baldur_pro`` reset
import lives in the fixture body, not module top.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from baldur.api.handlers.governance import (
    governance_mode_set,
    governance_reconcile,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.metrics.reliability_manager import OperatingMode

# Service-call helper module paths patched to keep each drive to a single Act.
_RELIABILITY_MANAGER = "baldur.metrics.reliability_manager.get_reliability_manager"
_EMERGENCY_TRACKER = "baldur_pro.services.governance.get_emergency_tracker"
_AUDIT_LOGGER = "baldur.audit.logger.AuditLogger.get_instance"
_METRIC_SYNC_SERVICE = "baldur.services.metric_sync_service.get_metric_sync_service"
# The handler's lazy PRO-service getter — patched only to prove a 400 guard
# short-circuits before the service is resolved.
_SERVICE_GETTER = "baldur.api.handlers.governance._governance_api_service"


@pytest.fixture(autouse=True)
def _reset_governance_api_service():
    """Reset the GovernanceApiService singleton around each test.

    The handler resolves the real PRO service lazily via
    ``_governance_api_service()``; resetting drives each test against a fresh
    instance. The ``baldur_pro`` import lives inside the fixture body (not
    module top) so the import-graph classifier counts the OSS handler as the
    system-under-test (true_boundary -> stays under ``tests/``).
    """
    from baldur_pro.services.governance.api_service import (
        reset_governance_api_service,
    )

    reset_governance_api_service()
    yield
    reset_governance_api_service()


def _make_ctx(json_body=None, user=None, path="/governance/mode/"):
    return RequestContext(
        method=HttpMethod("POST"),
        path=path,
        json_body=json_body,
        user=user,
    )


@contextmanager
def _real_set_mode_service(*, tracker_result=None, audit_sink=None):
    """Patch only set_mode's downstream singletons; the real
    ``GovernanceApiService.set_mode`` runs end-to-end through the ``actor=``
    kwarg.

    - ``get_reliability_manager`` -> a stub that records the forced mode
      (no real state backend).
    - ``get_emergency_tracker`` -> a STRICT-activation stub when
      ``tracker_result`` is supplied, else ImportError (the tracker is optional
      and ``_sync_emergency_tracker`` swallows ImportError).
    - ``AuditLogger.get_instance`` -> a sink capturing logged events (or a
      silent stub), so no real audit backend / filesystem is touched.
    """
    manager = SimpleNamespace(
        get_global_mode=lambda: OperatingMode.NORMAL,
        force_global_mode=lambda mode, reason: None,
    )
    if tracker_result is not None:
        tracker = SimpleNamespace(
            record_emergency_activation=lambda **kwargs: dict(tracker_result),
            record_normal_restoration=lambda **kwargs: {"deactivated": True},
        )
        tracker_patch = patch(_EMERGENCY_TRACKER, return_value=tracker)
    else:
        tracker_patch = patch(_EMERGENCY_TRACKER, side_effect=ImportError("absent"))

    audit = SimpleNamespace(
        log=(audit_sink.append if audit_sink is not None else (lambda event: None))
    )
    with (
        patch(_RELIABILITY_MANAGER, return_value=manager),
        tracker_patch,
        patch(_AUDIT_LOGGER, return_value=audit),
    ):
        yield


# =============================================================================
# governance_mode_set — handler -> real GovernanceApiService.set_mode
# =============================================================================


class TestGovernanceModeSetHandlerBehavior:
    """``POST /governance/mode/`` — 625 D1 (actor=) + D2/D6 (400 guards)."""

    def test_valid_mode_actor_path_returns_without_type_error(self):
        """NORMAL switch drives the real set_mode via actor= and returns 200.

        Regression: the handler previously passed ``actor_id=`` to
        ``set_mode(mode, actor, reason)`` (no ``**kwargs``), raising
        ``TypeError`` on every request.
        """
        # Given a real GovernanceApiService with downstream singletons stubbed
        with _real_set_mode_service():
            # When the handler switches the mode
            resp = governance_mode_set(
                _make_ctx(json_body={"mode": "NORMAL", "reason": "back to normal"})
            )

        # Then the actor= call site completes without a TypeError
        assert resp.status_code == 200
        assert resp.body["status"] == "mode_changed"
        assert resp.body["current_mode"] == "normal"
        assert resp.body["actor"] == "anonymous"

    def test_strict_with_valid_reason_succeeds(self):
        """STRICT escalation with a non-empty reason drives set_mode and 200s."""
        with _real_set_mode_service(tracker_result={"expiry_hours": 8}):
            resp = governance_mode_set(
                _make_ctx(json_body={"mode": "STRICT", "reason": "scheduled drill"})
            )

        assert resp.status_code == 200
        assert resp.body["current_mode"] == "strict"

    def test_strict_reason_recorded_verbatim_in_audit(self):
        """A valid STRICT reason is recorded verbatim in the audit event.

        Not the empty-reason default ("Manual mode change"): D2/D4 require the
        operator-supplied reason to reach ``AuditConfigChangeEvent.reason``.
        """
        # Given a sink capturing the audit event emitted by _log_mode_change
        audit_sink: list = []

        # When STRICT is forced with an explicit reason
        with _real_set_mode_service(
            tracker_result={"expiry_hours": 8}, audit_sink=audit_sink
        ):
            resp = governance_mode_set(
                _make_ctx(json_body={"mode": "STRICT", "reason": "scheduled drill"})
            )

        # Then the verbatim reason is on both the response and the audit entry
        assert resp.status_code == 200
        assert resp.body["reason"] == "scheduled drill"
        assert len(audit_sink) == 1
        assert audit_sink[0].reason == "scheduled drill"

    @pytest.mark.parametrize(
        ("mode_value", "reason_value"),
        [
            ("STRICT", ""),
            ("STRICT", "   "),
            ("STRICT", 123),
            ("strict", ""),
            ("strict", "   "),
            ("strict", 123),
        ],
        ids=[
            "upper_empty",
            "upper_whitespace",
            "upper_nonstring",
            "lower_empty",
            "lower_whitespace",
            "lower_nonstring",
        ],
    )
    def test_strict_without_valid_reason_rejected_as_400(
        self, mode_value, reason_value
    ):
        """STRICT requires a non-empty string reason; empty / whitespace-only /
        non-string reason -> HTTP 400 before the service is resolved, for both
        ``STRICT`` and ``strict`` (D2 case-normalized + type-safe guard)."""
        with patch(_SERVICE_GETTER) as service_getter:
            resp = governance_mode_set(
                _make_ctx(json_body={"mode": mode_value, "reason": reason_value})
            )

        assert resp.status_code == 400
        assert "reason is required for STRICT" in resp.body["error"]
        service_getter.assert_not_called()

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"mode": ""},
            {"mode": 123},
            {"mode": ["STRICT"]},
        ],
        ids=["missing", "empty", "nonstring_int", "nonstring_list"],
    )
    def test_mode_input_guard_rejects_bad_mode_as_400(self, body):
        """``mode`` must be a non-empty string; missing / empty / non-string
        ``mode`` -> 400 before the service is resolved. D6 strengthened the
        guard so a non-string value cannot reach ``mode.upper()`` and 500 with
        ``AttributeError``."""
        with patch(_SERVICE_GETTER) as service_getter:
            resp = governance_mode_set(_make_ctx(json_body=body))

        assert resp.status_code == 400
        assert "mode is required" in resp.body["error"]
        service_getter.assert_not_called()

    def test_invalid_mode_value_rejected_as_400_not_500(self):
        """An unrecognized mode **value** reaches set_mode (which raises
        ``ValueError``); the handler converts it to a 400 client error rather
        than letting it escape as an unhandled 500 (D6)."""
        with _real_set_mode_service():
            resp = governance_mode_set(
                _make_ctx(json_body={"mode": "BOGUS", "reason": "irrelevant"})
            )

        assert resp.status_code == 400
        assert "Invalid mode" in resp.body["error"]


# =============================================================================
# governance_reconcile — handler -> real GovernanceApiService.reconcile
# =============================================================================


class TestGovernanceReconcileHandlerBehavior:
    """``POST /governance/reconcile/`` — 625 D1 (actor= no TypeError)."""

    def test_reconcile_actor_path_returns_without_type_error(self):
        """``governance_reconcile`` drives the real reconcile() via actor=.

        Regression: the handler previously passed ``actor_id=`` to
        ``reconcile(domains, dry_run, actor, reason)``, raising ``TypeError``.
        The real reconcile() delegates to ``sync_metrics(actor=...)``; the
        metric-sync service (reconcile's downstream) is stubbed so the actor
        threads handler -> reconcile -> sync_metrics.
        """
        # Given a metric-sync service stub returned to the real reconcile()
        sync_service = SimpleNamespace(
            sync_metrics=lambda **kwargs: {
                "status": "completed",
                "synced_at": "2026-06-13T00:00:00+00:00",
                "actor": kwargs.get("actor"),
                "dry_run": kwargs.get("dry_run", False),
                "results": {},
                "summary": {},
            }
        )

        # When the handler reconciles
        with patch(_METRIC_SYNC_SERVICE, return_value=sync_service):
            resp = governance_reconcile(
                _make_ctx(
                    json_body={"domains": ["payment"], "dry_run": True},
                    user=SimpleNamespace(username="alice"),
                    path="/governance/reconcile/",
                )
            )

        # Then the actor= call site completes and the actor threads through
        assert resp.status_code == 200
        assert resp.body["reconciliation_result"] == "completed"
        assert resp.body["actor"] == "alice"
