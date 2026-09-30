"""``validate_required_secrets`` — the signing key's tier follows a keyed-chain writer.

Source: ``src/baldur/settings/secrets.py`` (``validate_required_secrets``,
``_audit_signing_key_required``).

``audit_signing_key`` keys every audit hash-chain entry, so it is CRITICAL only
while something can write a keyed chain: the audit trail is on, or a PRO
entitlement is active. Otherwise it is reported with the optional secrets.
``encryption_key`` has no production reader and is always optional. In
production an unset CRITICAL secret raises ``ConfigurationError`` naming its
variable; nothing else raises.

The level each report is logged at is pinned in ``test_752_report_levels.py``;
this file pins the classification and the raise across the decision table
(environment x audit switch x entitlement x key).

Verification techniques (UNIT_TEST_GUIDELINES §8): decision table via
parametrize (§6.7), branch-outcome completeness (§8.12), exception cases (§8.2).
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from baldur.core.exceptions import ConfigurationError
from baldur.settings.secrets import SecretsSettings, validate_required_secrets

_SIGNING_KEY = "audit_signing_key"
_ENCRYPTION_KEY = "encryption_key"
_SIGNING_KEY_ENV = "BALDUR_SECRETS_AUDIT_SIGNING_KEY"


@pytest.fixture
def keyed_chain_inputs(monkeypatch):
    """Set the environment, audit switch and entitlement verdict the tier reads.

    The verdict is pinned rather than inherited: the suite-wide fixture reports
    ACTIVE wherever PRO is installed, and the public CI runs with PRO absent.
    """
    from baldur.runtime import reset_runtime
    from baldur.settings.audit import reset_audit_settings

    def _set(*, production: bool, audit_on: bool, entitled: bool) -> None:
        monkeypatch.setenv(
            "BALDUR_ENVIRONMENT", "production" if production else "development"
        )
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_AUDIT_ENABLED", "true" if audit_on else "false")
        monkeypatch.setattr(
            "baldur.core.entitlement.is_entitlement_active", lambda: entitled
        )
        reset_runtime()
        reset_audit_settings()

    yield _set

    reset_runtime()
    reset_audit_settings()


def _secrets(*, signing_key: str, encryption_key: str = "") -> SecretsSettings:
    """The two secrets under test set explicitly; empty means unset."""
    return SecretsSettings(
        audit_signing_key=SecretStr(signing_key),
        encryption_key=SecretStr(encryption_key),
    )


_AUDIT = pytest.mark.parametrize(
    "audit_on", [True, False], ids=["audit_on", "audit_off"]
)
_ENTITLED = pytest.mark.parametrize(
    "entitled", [True, False], ids=["entitled", "not_entitled"]
)
_ENVIRONMENT = pytest.mark.parametrize(
    "production", [True, False], ids=["production", "development"]
)


class TestValidateRequiredSecretsBehavior:
    """Classification and refusal across environment x audit x entitlement x key."""

    @_AUDIT
    @_ENTITLED
    def test_unset_signing_key_is_critical_only_while_a_keyed_chain_can_be_written(
        self, keyed_chain_inputs, audit_on, entitled
    ):
        """Outside production (no raise), an unset key is CRITICAL iff audit is on
        or an entitlement is active; otherwise it is reported as optional."""
        keyed_chain_inputs(production=False, audit_on=audit_on, entitled=entitled)
        required = audit_on or entitled

        result = validate_required_secrets(_secrets(signing_key=""))

        assert (_SIGNING_KEY in result["critical"]) is required
        assert (_SIGNING_KEY in result["info"]) is not required

    @_AUDIT
    @_ENTITLED
    def test_production_refuses_an_unset_signing_key_only_when_it_is_required(
        self, keyed_chain_inputs, audit_on, entitled
    ):
        """Production raises iff the unset key is required; an OSS boot with audit
        off needs no key and reports it as optional."""
        keyed_chain_inputs(production=True, audit_on=audit_on, entitled=entitled)
        secrets = _secrets(signing_key="")

        if audit_on or entitled:
            with pytest.raises(ConfigurationError, match=_SIGNING_KEY_ENV):
                validate_required_secrets(secrets)
        else:
            result = validate_required_secrets(secrets)
            assert result["critical"] == []
            assert _SIGNING_KEY in result["info"]

    @_ENVIRONMENT
    @_AUDIT
    @_ENTITLED
    def test_a_set_signing_key_is_never_reported_and_never_refused(
        self, keyed_chain_inputs, production, audit_on, entitled
    ):
        keyed_chain_inputs(production=production, audit_on=audit_on, entitled=entitled)

        result = validate_required_secrets(_secrets(signing_key="signing-key"))

        assert _SIGNING_KEY not in result["critical"]
        assert _SIGNING_KEY not in result["info"]

    @_ENVIRONMENT
    @_AUDIT
    @_ENTITLED
    def test_an_unset_encryption_key_is_optional_and_never_refused(
        self, keyed_chain_inputs, production, audit_on, entitled
    ):
        """The encryption key has no production reader, whatever else is on."""
        keyed_chain_inputs(production=production, audit_on=audit_on, entitled=entitled)

        result = validate_required_secrets(
            _secrets(signing_key="signing-key", encryption_key="")
        )

        assert _ENCRYPTION_KEY in result["info"]
        assert _ENCRYPTION_KEY not in result["critical"]
        assert result["critical"] == []


class TestValidateRequiredSecretsContract:
    """The production refusal text an operator acts on (801 D1)."""

    def test_production_refusal_names_the_variable_and_the_condition(
        self, keyed_chain_inputs
    ):
        keyed_chain_inputs(production=True, audit_on=True, entitled=False)

        with pytest.raises(ConfigurationError) as excinfo:
            validate_required_secrets(_secrets(signing_key=""))

        message = str(excinfo.value)
        assert "BALDUR_SECRETS_AUDIT_SIGNING_KEY is required in production" in message
        assert "BALDUR_AUDIT_ENABLED" in message
        assert "entitlement is active" in message
