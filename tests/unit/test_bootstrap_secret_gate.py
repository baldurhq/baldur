"""632 D7 — centralized CRITICAL-secret boot gate (``bootstrap._validate_critical_secrets``).

The prod boot-abort for missing CRITICAL secrets (``encryption_key`` /
``audit_signing_key``) was lifted out of the Django-only
``apps.py._validate_secrets`` into ``baldur.init()`` so it fires on every
framework adapter (Django / Flask / FastAPI / CLI).

This file pins the **end-to-end** gate behavior against the REAL
``validate_required_secrets`` under a REAL production runtime — the genuinely
new coverage owned by ``/test``. The mock-isolated wrapper-dispatch contract
(``patch(validate_required_secrets, ...)`` → log-event names, best-effort vs
re-raise) is covered by ``TestValidateCriticalSecretsBehavior`` in
``tests/unit/security/test_jwt_blacklist_and_secrets.py`` (migrated by
``/execute``); this file does not duplicate it.

Env control (§6.5.8 sanctioned pattern + Testability Notes):
- ``is_production()`` is read once at ``BaldurRuntime`` construction, so
  ``BALDUR_ENVIRONMENT``/``BALDUR_TEST_MODE`` changes take effect only after
  ``reset_init_state()`` rebuilds the runtime.
- ``tests/testapp/settings.py`` ambiently sets the CRITICAL secrets at import,
  so every case controls them explicitly via ``monkeypatch`` +
  ``reset_secrets_settings()`` and never relies on ambient state.

This module lives at ``tests/unit/test_bootstrap_secret_gate.py`` following
the existing ``tests/unit/test_bootstrap_*.py`` convention (``baldur.bootstrap``
is a top-level module without a parent package).
"""

from __future__ import annotations

import pytest

from baldur import bootstrap
from baldur.bootstrap import _validate_critical_secrets
from baldur.settings.secrets import reset_secrets_settings

_AUDIT_KEY_ENV = "BALDUR_SECRETS_AUDIT_SIGNING_KEY"
_ENCRYPTION_KEY_ENV = "BALDUR_SECRETS_ENCRYPTION_KEY"


@pytest.fixture(autouse=True)
def _reset_bootstrap_runtime_and_secrets():
    """Each case starts and ends with clean runtime + secrets state."""
    bootstrap.reset_init_state()
    reset_secrets_settings()
    yield
    bootstrap.reset_init_state()
    reset_secrets_settings()


def _seed(
    monkeypatch,
    *,
    production: bool,
    audit_signing_key: str | None,
    encryption_key: str | None = "encryption-value",
) -> None:
    """Set environment + rebuild runtime so ``is_production()`` re-reads it.

    ``audit_signing_key`` / ``encryption_key`` of ``None`` means *unset*
    (delenv, overriding the ambient test-value); a string (incl. ``""``) is set
    verbatim so the empty-string ≡ unset invariant is exercisable end-to-end.
    """
    monkeypatch.setenv(
        "BALDUR_ENVIRONMENT", "production" if production else "development"
    )
    monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)

    for env_name, value in (
        (_AUDIT_KEY_ENV, audit_signing_key),
        (_ENCRYPTION_KEY_ENV, encryption_key),
    ):
        if value is None:
            monkeypatch.delenv(env_name, raising=False)
        else:
            monkeypatch.setenv(env_name, value)

    # Rebuild the runtime (is_production) and drop the cached secrets instance.
    bootstrap.reset_init_state()
    reset_secrets_settings()


class TestValidateCriticalSecretsGate:
    """End-to-end behavior of the centralized prod secret gate (D7)."""

    def test_production_with_unset_audit_signing_key_aborts(self, monkeypatch):
        # Given production with audit_signing_key unset (encryption present)
        _seed(monkeypatch, production=True, audit_signing_key=None)

        # Then the gate raises, naming the missing CRITICAL secret
        with pytest.raises(RuntimeError, match="audit_signing_key"):
            _validate_critical_secrets()

    def test_production_with_empty_audit_signing_key_aborts(self, monkeypatch):
        # Given production with an empty-string key (empty == unset, D4 / SC #5)
        _seed(monkeypatch, production=True, audit_signing_key="")

        # Then the empty key is treated as missing and aborts boot
        with pytest.raises(RuntimeError, match="audit_signing_key"):
            _validate_critical_secrets()

    def test_production_with_unset_encryption_key_aborts(self, monkeypatch):
        # Given production with the OTHER CRITICAL secret unset
        _seed(
            monkeypatch,
            production=True,
            audit_signing_key="audit-value",
            encryption_key=None,
        )

        # Then the gate also covers encryption_key (D7 side benefit)
        with pytest.raises(RuntimeError, match="encryption_key"):
            _validate_critical_secrets()

    def test_production_with_all_critical_secrets_set_passes(self, monkeypatch):
        # Given production with every CRITICAL secret configured
        _seed(
            monkeypatch,
            production=True,
            audit_signing_key="audit-value",
            encryption_key="encryption-value",
        )

        # Then the gate is a no-op (no abort) — boot proceeds
        _validate_critical_secrets()

    def test_non_production_with_unset_keys_does_not_abort(self, monkeypatch):
        # Given a non-production runtime with both CRITICAL secrets unset
        _seed(
            monkeypatch,
            production=False,
            audit_signing_key=None,
            encryption_key=None,
        )

        # Then the gate is best-effort (no abort outside production)
        _validate_critical_secrets()

    def test_baldur_init_in_production_without_audit_key_raises(self, monkeypatch):
        # Given production with audit_signing_key unset
        _seed(monkeypatch, production=True, audit_signing_key=None)

        # Then the full init() path aborts — the gate is wired into init()
        # before the heavy startup steps (SC #8: RuntimeError from baldur.init()
        # on the central, non-Django path).
        with pytest.raises(RuntimeError, match="CRITICAL secrets"):
            bootstrap.init()
