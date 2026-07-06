"""Regression test for env-var prefix drift on critical security settings (529 G1).

Wave 6A (doc 508) migrated every env var to the ``BALDUR_*`` prefix. Wave 6E
(doc 529) discovered that ``SECURITY.md`` still instructed users to set the
pre-migration ``SELFHEALING_*`` names — leading to silent boot crashes in
production because the CRITICAL secrets (``encryption_key`` /
``audit_signing_key``) raise ``RuntimeError`` when missing.

Existing tests at ``tests/unit/security/test_jwt_blacklist_and_secrets.py``
mock ``validate_required_secrets`` via ``patch(..., side_effect=RuntimeError)``,
which only verifies the ``apps.ready()`` re-raise wrapper — the underlying
env-var-to-settings-class binding is not exercised. A future rename of
``BALDUR_SECRETS_*`` (or its nested fields) would silently pass the mock-based
tests and re-introduce the G1 misconfiguration.

This test pins the canonical env-var names by setting each variable via
``monkeypatch.setenv`` and instantiating the settings class fresh.
"""

from __future__ import annotations

import pytest

from baldur.core.tls import TLSConfig
from baldur.settings.secrets import SecretsSettings


@pytest.mark.parametrize(
    ("env_name", "attr", "raw_value", "expected"),
    [
        (
            "BALDUR_SECRETS_ENCRYPTION_KEY",
            "encryption_key",
            "fake-fernet-key-for-binding-test",
            "fake-fernet-key-for-binding-test",
        ),
        (
            "BALDUR_SECRETS_AUDIT_SIGNING_KEY",
            "audit_signing_key",
            "fake-audit-signing-key-for-binding-test",
            "fake-audit-signing-key-for-binding-test",
        ),
    ],
    ids=["encryption_key", "audit_signing_key"],
)
def test_secrets_env_var_binds_to_settings_field(
    monkeypatch, env_name, attr, raw_value, expected
):
    """``BALDUR_SECRETS_*`` env var must populate the matching SecretsSettings field."""
    monkeypatch.setenv(env_name, raw_value)

    settings = SecretsSettings()

    assert getattr(settings, attr).get_secret_value() == expected


@pytest.mark.parametrize(
    ("env_name", "attr", "raw_value", "expected"),
    [
        ("BALDUR_TLS_ENABLED", "enabled", "true", True),
        ("BALDUR_TLS_MIN_VERSION", "min_version", "TLSv1.3", "TLSv1.3"),
    ],
    ids=["enabled", "min_version"],
)
def test_tls_env_var_binds_to_settings_field(
    monkeypatch, env_name, attr, raw_value, expected
):
    """``BALDUR_TLS_*`` env var must populate the matching TLSConfig field."""
    monkeypatch.setenv(env_name, raw_value)

    config = TLSConfig()

    assert getattr(config, attr) == expected
