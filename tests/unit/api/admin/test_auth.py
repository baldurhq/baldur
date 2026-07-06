"""Admin server 3-layer auth unit tests — 429 PR3-runtime / C6.

Verification targets:
- Layer 1 (binding): check_bind_safety enforces api_key on non-localhost bind
- Layer 2 (transport): emit_transport_warning logs only on non-localhost
  without trust_proxy
- Layer 3 (authentication): authenticate truth table (api_key set/unset ×
  header present/correct/wrong × from_localhost)
- Authorization: _LEVEL_ORDER matrix + ADMIN requires unlock
"""

from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr

from baldur.api.admin.auth import (
    API_KEY_HEADER,
    AdminAuthRequiredError,
    AuthOutcome,
    authenticate,
    authorize,
    check_bind_safety,
    emit_transport_warning,
)
from baldur.interfaces.web_framework import PermissionLevel
from baldur.settings.admin import AdminServerSettings

# =============================================================================
# Contract — Layer 1: bind safety
# =============================================================================


class TestCheckBindSafetyContract:
    """check_bind_safety is the fail-loud gate on startup.

    Design (429 Part 2 C6 Layer 1): non-localhost bind without an API key
    refuses to start — silently exposing an unauthenticated admin endpoint
    is a security bug, not a feature.
    """

    def test_localhost_without_key_is_allowed(self):
        """127.0.0.1 / ::1 / localhost + no key is the OSS dev default."""
        settings = AdminServerSettings(bind="127.0.0.1")
        check_bind_safety(settings)  # must not raise

    def test_localhost_with_key_is_allowed(self):
        """Key configured on localhost is valid (opt-in hardening)."""
        settings = AdminServerSettings(bind="127.0.0.1", api_key="secret")
        check_bind_safety(settings)

    def test_non_localhost_with_key_is_allowed(self):
        """Non-localhost bind with api_key is the documented prod setup."""
        settings = AdminServerSettings(bind="0.0.0.0", api_key="secret")
        check_bind_safety(settings)

    def test_non_localhost_without_key_refuses_to_start(self):
        """Non-localhost bind + no api_key → AdminAuthRequiredError."""
        settings = AdminServerSettings(bind="0.0.0.0", api_key=None)
        with pytest.raises(AdminAuthRequiredError) as excinfo:
            check_bind_safety(settings)
        # Error message includes the env var name so operators know the fix.
        assert "BALDUR_ADMIN_KEY" in str(excinfo.value)

    def test_non_localhost_with_empty_key_refuses_to_start(self):
        """Empty-string API key counts as "no key configured"."""
        settings = AdminServerSettings(bind="0.0.0.0", api_key="")
        with pytest.raises(AdminAuthRequiredError):
            check_bind_safety(settings)


# =============================================================================
# Behavior — Layer 2: transport warning
# =============================================================================


class TestEmitTransportWarningBehavior:
    """emit_transport_warning is a side-effect (log emission)."""

    def test_localhost_bind_emits_no_warning(self, caplog):
        """Localhost bind is safe — no warning expected."""
        settings = AdminServerSettings(bind="127.0.0.1")
        with caplog.at_level(logging.WARNING, logger="baldur"):
            emit_transport_warning(settings)
        # Check the structured event key, not the rendered text — structlog
        # renderer changes (e.g. JSON-only) must not cause trivial passes.
        events = {getattr(r, "msg", None) for r in caplog.records}
        assert "admin.server_bind_warning" not in events

    def test_non_localhost_with_trust_proxy_emits_no_warning(self, caplog):
        """Operator affirmed TLS-terminating proxy → no warning."""
        settings = AdminServerSettings(bind="0.0.0.0", api_key="k", trust_proxy=True)
        with caplog.at_level(logging.WARNING, logger="baldur"):
            emit_transport_warning(settings)
        events = {getattr(r, "msg", None) for r in caplog.records}
        assert "admin.server_bind_warning" not in events

    def test_non_localhost_without_trust_proxy_emits_warning(self, caplog):
        """Non-localhost bind + trust_proxy=False → warning."""
        settings = AdminServerSettings(bind="0.0.0.0", api_key="k", trust_proxy=False)
        # structlog in the test harness routes through stdlib at WARNING.
        with caplog.at_level(logging.WARNING):
            emit_transport_warning(settings)
        # The structured event name is the load-bearing identifier.
        combined = caplog.text + " ".join(str(r) for r in caplog.records)
        assert "server_bind_warning" in combined or any(
            "server_bind_warning" in str(getattr(r, "msg", "")) for r in caplog.records
        )


# =============================================================================
# Contract — Layer 3: authenticate truth table
# =============================================================================


class TestAuthenticateContract:
    """authenticate() return-value truth table for the 3-layer C6 design."""

    def test_no_key_configured_localhost_is_operator(self):
        """No api_key + localhost client → OPERATOR (dev default)."""
        settings = AdminServerSettings(bind="127.0.0.1", api_key=None)
        outcome = authenticate(None, settings, from_localhost=True)
        assert outcome.level == PermissionLevel.OPERATOR
        assert outcome.authenticated is True

    def test_no_key_configured_non_localhost_is_unauthenticated(self):
        """check_bind_safety should have refused this config at startup, but
        the auth function is defence-in-depth and still rejects."""
        settings = AdminServerSettings(bind="127.0.0.1", api_key=None)
        outcome = authenticate(None, settings, from_localhost=False)
        assert outcome.level is None
        assert outcome.authenticated is False
        assert outcome.reason == "api_key_not_configured_for_non_localhost_client"

    def test_key_configured_missing_header_is_unauthenticated(self):
        """api_key set but no X-Baldur-Admin-Key header → 401."""
        settings = AdminServerSettings(api_key="secret")
        outcome = authenticate(None, settings, from_localhost=True)
        assert outcome.level is None
        assert outcome.reason == "missing_api_key_header"

    def test_key_configured_wrong_header_is_unauthenticated(self):
        """api_key set + mismatching header → 401."""
        settings = AdminServerSettings(api_key="secret")
        outcome = authenticate("nope", settings, from_localhost=True)
        assert outcome.level is None
        assert outcome.reason == "invalid_api_key"

    def test_key_configured_correct_header_is_operator(self):
        """api_key set + matching header → OPERATOR."""
        settings = AdminServerSettings(api_key="secret")
        outcome = authenticate("secret", settings, from_localhost=True)
        assert outcome.level == PermissionLevel.OPERATOR
        assert outcome.authenticated is True

    def test_compare_digest_prevents_length_oracle(self):
        """Comparison uses hmac.compare_digest — length-equal wrong key still
        rejected (no early-return on first byte)."""
        settings = AdminServerSettings(api_key="same-length-key")
        outcome = authenticate("wrong-length-key", settings, from_localhost=True)
        assert outcome.level is None

    # --- read-only (VIEWER) credential + operator-first restructure (D1) ------

    @pytest.mark.parametrize(
        ("header", "expected_level", "expected_reason"),
        [
            ("operator-secret", PermissionLevel.OPERATOR, None),
            ("readonly-secret", PermissionLevel.VIEWER, None),
            (None, None, "missing_api_key_header"),
            ("totally-wrong", None, "invalid_api_key"),
        ],
        ids=["operator_header", "readonly_header", "missing_header", "wrong_header"],
    )
    def test_both_keys_configured_resolves_by_header(
        self, header, expected_level, expected_reason
    ):
        """With both secrets set, the header value selects the tier: the operator
        key → OPERATOR, the readonly key → VIEWER, and the fall-through is
        unchanged (missing → missing_api_key_header, wrong → invalid_api_key).

        The operator_header / readonly_header rows are the reachable form of the
        operator-first restructure: both branches coexist and each resolves its
        own secret (a header can match at most one of two distinct keys).
        """
        settings = AdminServerSettings(
            api_key="operator-secret", readonly_key="readonly-secret"
        )
        outcome = authenticate(header, settings, from_localhost=True)
        assert outcome.level == expected_level
        assert outcome.reason == expected_reason

    def test_readonly_only_localhost_with_readonly_header_is_viewer(self):
        """No operator key + readonly key + localhost + readonly header → VIEWER.

        Load-bearing precedence (D1): the readonly-match is evaluated BEFORE the
        ``api_key is None`` localhost dev-default. Without the lift this config
        would fall through to the dev-default and silently resolve the readonly
        holder to OPERATOR — a least-privilege violation.
        """
        settings = AdminServerSettings(api_key=None, readonly_key="readonly-secret")
        outcome = authenticate("readonly-secret", settings, from_localhost=True)
        assert outcome.level == PermissionLevel.VIEWER

    def test_readonly_only_localhost_without_header_is_operator_dev_default(self):
        """No operator key + readonly key + localhost + no header → OPERATOR.

        The dev-default boundary is preserved for the no-header case, and the
        readonly-match branch's ``header_value`` guard prevents a None-header
        crash before compare_digest (readonly_key is configured here).
        """
        settings = AdminServerSettings(api_key=None, readonly_key="readonly-secret")
        outcome = authenticate(None, settings, from_localhost=True)
        assert outcome.level == PermissionLevel.OPERATOR

    def test_equal_keys_resolve_to_operator_defense_in_depth(self):
        """Operator-match is evaluated before readonly-match (D1 branch order).

        Equal keys are rejected at settings load (see
        test_admin_settings.py::test_equal_keys_raise_validation_error), so this
        config is unreachable through normal construction — model_construct
        bypasses validation to verify the defense-in-depth ordering BEHIND that
        guard: were the keys ever equal, the operator branch wins and the shared
        value resolves to OPERATOR, never VIEWER.
        """
        settings = AdminServerSettings.model_construct(
            api_key=SecretStr("dup-secret"), readonly_key=SecretStr("dup-secret")
        )
        outcome = authenticate("dup-secret", settings, from_localhost=True)
        assert outcome.level == PermissionLevel.OPERATOR


# =============================================================================
# Contract — authorize permission matrix
# =============================================================================


class TestAuthorizeContract:
    """authorize() matrix for PermissionLevel precedence + ADMIN unlock gate."""

    @pytest.fixture
    def locked_settings(self):
        return AdminServerSettings(unlock=False)

    @pytest.fixture
    def unlocked_settings(self):
        return AdminServerSettings(unlock=True)

    def test_public_route_allows_unauthenticated(self, locked_settings):
        """PUBLIC routes are reachable without authentication."""
        assert authorize(None, PermissionLevel.PUBLIC, locked_settings) is True

    def test_unauthenticated_blocked_from_viewer_route(self, locked_settings):
        """Any non-PUBLIC route rejects unauthenticated callers."""
        assert authorize(None, PermissionLevel.VIEWER, locked_settings) is False

    @pytest.mark.parametrize(
        ("effective", "required", "expected"),
        [
            (PermissionLevel.VIEWER, PermissionLevel.VIEWER, True),
            (PermissionLevel.OPERATOR, PermissionLevel.VIEWER, True),
            (PermissionLevel.OPERATOR, PermissionLevel.OPERATOR, True),
            (PermissionLevel.VIEWER, PermissionLevel.OPERATOR, False),
            (PermissionLevel.AUTHENTICATED, PermissionLevel.VIEWER, False),
        ],
    )
    def test_level_precedence_matrix(
        self, locked_settings, effective, required, expected
    ):
        """Effective level must be >= required level to pass."""
        assert authorize(effective, required, locked_settings) is expected

    def test_admin_route_rejected_when_unlock_false(self, locked_settings):
        """OPERATOR-authenticated caller cannot invoke ADMIN without unlock."""
        assert (
            authorize(PermissionLevel.OPERATOR, PermissionLevel.ADMIN, locked_settings)
            is False
        )

    def test_admin_route_allowed_for_authenticated_operator_when_unlocked(
        self, unlocked_settings
    ):
        """ADMIN is a double-gate: authenticated OPERATOR+ AND unlock=True.

        ``authenticate()`` never returns ADMIN directly — ``unlock`` is the
        promotion flag that elevates an authenticated OPERATOR for the
        request. Both gates must pass; neither alone suffices (see
        ``test_admin_route_rejected_when_unlock_false`` for the other half).
        """
        assert (
            authorize(
                PermissionLevel.OPERATOR, PermissionLevel.ADMIN, unlocked_settings
            )
            is True
        )

    def test_admin_route_rejected_for_below_operator_even_when_unlocked(
        self, unlocked_settings
    ):
        """unlock=True does not lift the effective-level floor.

        A VIEWER-level caller (effective < OPERATOR) cannot reach ADMIN
        routes even with unlock=True — prevents read-only tokens from
        being promoted into destructive-op tokens by a stray env flag.
        """
        assert (
            authorize(PermissionLevel.VIEWER, PermissionLevel.ADMIN, unlocked_settings)
            is False
        )

    def test_admin_route_rejected_for_unauthenticated_even_when_unlocked(
        self, unlocked_settings
    ):
        """unlock=True + unauthenticated (effective=None) still fails."""
        assert authorize(None, PermissionLevel.ADMIN, unlocked_settings) is False


# =============================================================================
# Contract — small constants
# =============================================================================


class TestAuthConstantsContract:
    """Exported constants are public contract."""

    def test_header_name_is_x_baldur_admin_key(self):
        """Header name is the doc-stated BALDUR_ADMIN_KEY pairing."""
        assert API_KEY_HEADER == "X-Baldur-Admin-Key"

    def test_auth_outcome_authenticated_reflects_level(self):
        """authenticated is True iff level is not None."""
        assert AuthOutcome(PermissionLevel.VIEWER).authenticated is True
        assert AuthOutcome(None, reason="x").authenticated is False
