"""``_enforce_post_hook_requirements`` — production requirements read after the PRO hook.

Source: ``src/baldur/bootstrap.py:_enforce_post_hook_requirements``.

Two production requirements depend on what the PRO bootstrap hook settles —
the entitlement verdict and the audit switch — so ``init()`` checks both in one
step directly after the hook:

1. the audit signing key, while the audit trail is on or an entitlement is
   active (through ``_validate_critical_secrets``);
2. a SQL or Django store (``BALDUR_SQL_DSN`` or Django ``DATABASES``) under an
   active entitlement.

The key check runs first. Test mode returns before either; outside production
the key report is best-effort and the store is never required.

The stale-verdict composition — a MISSING cached before the hook's forced
re-validation — is pinned through ``init()`` in
``tests/integration/test_init_fail_loud.py``.

Verification techniques (UNIT_TEST_GUIDELINES §8): decision table via
parametrize (§6.7), branch-outcome completeness (§8.12), exception cases (§8.2).
"""

from __future__ import annotations

from contextlib import nullcontext

import pytest

from baldur.bootstrap import _enforce_post_hook_requirements
from baldur.core.exceptions import ConfigurationError

_KEY_REFUSAL = "BALDUR_SECRETS_AUDIT_SIGNING_KEY"
_STORE_REFUSAL = "Neither BALDUR_SQL_DSN nor Django DATABASES"


@pytest.fixture
def requirement_inputs(monkeypatch):
    """Set every input the step reads, then rebuild what caches them.

    The entitlement verdict and the Django probe are pinned: the suite-wide
    fixture reports ACTIVE wherever PRO is installed, the public CI runs with
    PRO absent, and CI sets ``DJANGO_SETTINGS_MODULE`` for the whole run.
    """
    from baldur.runtime import reset_runtime
    from baldur.settings.audit import reset_audit_settings
    from baldur.settings.secrets import reset_secrets_settings

    def _set(
        *,
        production: bool = True,
        test_mode: bool = False,
        entitled: bool = False,
        audit_on: bool = False,
        key_set: bool = False,
        sql_dsn: str | None = None,
        django: bool = False,
    ) -> None:
        monkeypatch.setenv(
            "BALDUR_ENVIRONMENT", "production" if production else "development"
        )
        if test_mode:
            monkeypatch.setenv("BALDUR_TEST_MODE", "true")
        else:
            monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_AUDIT_ENABLED", "true" if audit_on else "false")
        if key_set:
            monkeypatch.setenv(_KEY_REFUSAL, "signing-key")
        else:
            monkeypatch.delenv(_KEY_REFUSAL, raising=False)
        if sql_dsn is None:
            monkeypatch.delenv("BALDUR_SQL_DSN", raising=False)
        else:
            monkeypatch.setenv("BALDUR_SQL_DSN", sql_dsn)
        monkeypatch.setattr(
            "baldur.core.entitlement.is_entitlement_active", lambda: entitled
        )
        monkeypatch.setattr(
            "baldur.bootstrap._django_databases_configured", lambda: django
        )
        reset_runtime()
        reset_audit_settings()
        reset_secrets_settings()

    yield _set

    reset_runtime()
    reset_audit_settings()
    reset_secrets_settings()


class TestPostHookRequirementsBehavior:
    """Which inputs refuse a production boot, and which refusal comes first."""

    @pytest.mark.parametrize(
        ("inputs", "refusal"),
        [
            ({"test_mode": True, "entitled": True, "audit_on": True}, None),
            ({"production": False, "entitled": True, "audit_on": True}, None),
            ({}, None),
            ({"audit_on": True}, _KEY_REFUSAL),
            ({"entitled": True}, _KEY_REFUSAL),
            ({"entitled": True, "key_set": True}, _STORE_REFUSAL),
            ({"entitled": True, "key_set": True, "sql_dsn": "   "}, _STORE_REFUSAL),
            ({"entitled": True, "key_set": True, "sql_dsn": "sqlite:///x.db"}, None),
            ({"entitled": True, "key_set": True, "django": True}, None),
            ({"audit_on": True, "key_set": True}, None),
        ],
        ids=[
            "test_mode_checks_nothing",
            "development_refuses_nothing",
            "published_oss_block_needs_neither",
            "audit_on_requires_the_key",
            "entitled_without_either_names_the_key_first",
            "entitled_with_key_requires_a_store",
            "blank_dsn_is_no_store",
            "sql_dsn_satisfies_the_store",
            "django_databases_satisfy_the_store",
            "audit_without_entitlement_needs_no_store",
        ],
    )
    def test_production_refusal_follows_the_active_writers(
        self, requirement_inputs, inputs, refusal
    ):
        requirement_inputs(**inputs)
        expectation = (
            nullcontext()
            if refusal is None
            else pytest.raises(ConfigurationError, match=refusal)
        )

        with expectation:
            _enforce_post_hook_requirements()


class TestPostHookRequirementsContract:
    """The store refusal text an operator acts on (801 D2)."""

    def test_store_refusal_names_both_signals_and_why_it_is_required(
        self, requirement_inputs
    ):
        requirement_inputs(entitled=True, key_set=True)

        with pytest.raises(ConfigurationError) as excinfo:
            _enforce_post_hook_requirements()

        message = str(excinfo.value)
        assert "Neither BALDUR_SQL_DSN nor Django DATABASES" in message
        assert "PRO entitlement is active" in message
        assert "BALDUR_SQL_DSN=postgresql://" in message
