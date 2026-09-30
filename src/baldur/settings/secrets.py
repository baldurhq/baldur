"""
Secrets Settings - SecretStr-based sensitive configuration.

Pydantic SecretStr characteristics:
- repr(): prints '**********'
- str(): prints '**********'
- get_secret_value(): returns the actual value

Benefits:
- Automatic masking in print(settings)
- Automatic masking in JSON logging
- Safe for audit logs

Security hardening:
- validate_required_secrets(): reports unset secrets, and in production
  refuses to start without ``audit_signing_key`` while a keyed audit chain
  can be written (audit trail on, or a PRO entitlement active)
"""

import structlog
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

from baldur.core.exceptions import ConfigurationError
from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class SecretsSettings(BaseSettings):
    """
    Configuration dedicated to sensitive values.

    All passwords, API keys, and tokens are managed by this class.
    SecretStr is used so values are masked automatically when logged.

    Environment variables:
        BALDUR_SECRETS_DATABASE_PASSWORD=...
        BALDUR_SECRETS_REDIS_PASSWORD=...
        BALDUR_SECRETS_TOSS_SECRET_KEY=...
        BALDUR_SECRETS_SLACK_WEBHOOK_TOKEN=...
        BALDUR_SECRETS_ENCRYPTION_KEY=...

    Usage:
        from baldur.settings.secrets import get_secrets

        secrets = get_secrets()

        # Safe output (masked)
        print(secrets)  # database_password=SecretStr('**********')

        # Access the actual value
        actual_password = secrets.database_password.get_secret_value()
    """

    model_config = make_settings_config("BALDUR_SECRETS_")

    # ==========================================================================
    # Database
    # ==========================================================================
    database_password: SecretStr = Field(
        default=SecretStr(""),
        description="Database password (masked in logs)",
    )

    # ==========================================================================
    # Redis
    # ==========================================================================
    redis_password: SecretStr = Field(
        default=SecretStr(""),
        description="Redis password (masked in logs)",
    )

    # ==========================================================================
    # External APIs
    # ==========================================================================
    toss_secret_key: SecretStr = Field(
        default=SecretStr(""),
        description="Toss Payment secret key (masked in logs)",
    )

    slack_webhook_token: SecretStr = Field(
        default=SecretStr(""),
        description="Slack webhook token (masked in logs)",
    )

    slack_bot_token: SecretStr = Field(
        default=SecretStr(""),
        description="Slack Bot OAuth token (masked in logs)",
    )

    pagerduty_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="PagerDuty API key (masked in logs)",
    )

    # ==========================================================================
    # Encryption
    # ==========================================================================
    encryption_key: SecretStr = Field(
        default=SecretStr(""),
        description="Master encryption key for sensitive data (masked in logs)",
    )

    audit_signing_key: SecretStr = Field(
        default=SecretStr(""),
        description="Key for signing audit logs (masked in logs)",
    )

    # ==========================================================================
    # AWS (if used)
    # ==========================================================================
    aws_access_key_id: SecretStr = Field(
        default=SecretStr(""),
        description="AWS Access Key ID (masked in logs)",
    )

    aws_secret_access_key: SecretStr = Field(
        default=SecretStr(""),
        description="AWS Secret Access Key (masked in logs)",
    )

    # ==========================================================================
    # Helper methods
    # ==========================================================================
    def has_database_password(self) -> bool:
        """Check whether the database password is set."""
        return bool(self.database_password.get_secret_value())

    def has_redis_password(self) -> bool:
        """Check whether the Redis password is set."""
        return bool(self.redis_password.get_secret_value())

    def has_toss_secret(self) -> bool:
        """Check whether the Toss secret key is set."""
        return bool(self.toss_secret_key.get_secret_value())

    def has_slack_webhook(self) -> bool:
        """Check whether the Slack webhook token is set."""
        return bool(self.slack_webhook_token.get_secret_value())

    def get_masked_summary(self) -> dict:
        """
        Return a masked summary of every secret.

        Returns:
            {field_name: is_set (bool)} dictionary
        """
        return {
            "database_password": self.has_database_password(),
            "redis_password": self.has_redis_password(),
            "toss_secret_key": self.has_toss_secret(),
            "slack_webhook_token": self.has_slack_webhook(),
            "slack_bot_token": bool(self.slack_bot_token.get_secret_value()),
            "pagerduty_api_key": bool(self.pagerduty_api_key.get_secret_value()),
            "encryption_key": bool(self.encryption_key.get_secret_value()),
            "audit_signing_key": bool(self.audit_signing_key.get_secret_value()),
            "aws_access_key_id": bool(self.aws_access_key_id.get_secret_value()),
            "aws_secret_access_key": bool(
                self.aws_secret_access_key.get_secret_value()
            ),
        }


def get_secrets_settings() -> "SecretsSettings":
    from baldur.settings.root import get_config

    return get_config().adapters.secrets


# Backward-compatible alias
get_secrets = get_secrets_settings


def reset_secrets_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["secrets"]
    except KeyError:
        pass


# Backward-compatible alias
reset_secrets = reset_secrets_settings


def _report_unset_secrets(
    secrets: dict[str, SecretStr],
    event: str,
    level: str,
) -> list[str]:
    """Report every empty secret in ``secrets`` at ``level``; return their names."""
    log = getattr(logger, level)
    unset = [name for name, secret in secrets.items() if not secret.get_secret_value()]
    for name in unset:
        log(event, secret_name=name)
    return unset


def _audit_signing_key_required() -> bool:
    """Whether a keyed audit chain can be written in this process.

    ``audit_signing_key`` keys every audit hash-chain entry. Chains are
    written by the audit adapters only while the audit trail is on, and by
    PRO (postmortem sealing, weighted error-budget audit) only under an
    active entitlement. Read the entitlement after the PRO bootstrap hook
    has run: the hook re-validates it and turns audit on, so an earlier
    read can see neither.

    An audit switch that cannot be read (an invalid ``BALDUR_AUDIT_*`` value)
    counts as on: the key's safe direction is "required", and the settings
    error must not let an entitled process boot and chain without it.
    """
    from baldur.settings.audit import get_audit_settings

    try:
        audit_on = get_audit_settings().enabled
    except Exception as e:
        logger.warning(
            "security.audit_switch_read_failed",
            error=str(e),
            hint="Treating the audit trail as on, so the signing key is required.",
        )
        return True
    if audit_on:
        return True

    from baldur.core.entitlement import is_entitlement_active

    return is_entitlement_active()


def validate_required_secrets(secrets: SecretsSettings | None = None) -> dict:
    """
    Verify that the core secrets are configured.

    Security hardening, in production:
    - CRITICAL: ``audit_signing_key`` while a keyed audit chain can be
      written — the audit trail is on (``BALDUR_AUDIT_ENABLED``) or a PRO
      entitlement is active. ERROR log when unset.
    - IMPORTANT secrets (database_password, redis_password): WARNING log when unset
    - OPTIONAL secrets, ``encryption_key`` among them, and
      ``audit_signing_key`` when nothing writes a keyed chain: INFO log when
      unset

    Outside production the CRITICAL and IMPORTANT reports drop to INFO and
    DEBUG. Every one of these fields defaults to an empty ``SecretStr``, so
    an empty secret is the expected state of a zero-config development boot;
    a security ERROR that fires on every healthy dev machine teaches
    operators to ignore security ERRORs.

    In production, a missing CRITICAL secret raises ConfigurationError —
    the deliberate fail-loud class every framework adapter's startup path
    aborts on. The class matters on Celery: the worker receiver converts
    only this class to SystemExit, and celery's signal dispatch swallows a
    plain Exception, so a bare RuntimeError would boot the worker on
    pre-init defaults instead of stopping it.

    Args:
        secrets: SecretsSettings instance to validate (uses the singleton if None)

    Returns:
        {"critical": [...], "warning": [...], "info": [...]} list of unset secrets

    Raises:
        ConfigurationError: When a CRITICAL secret is unset in production
    """
    if secrets is None:
        secrets = get_secrets()

    # Secret classification. The signing key is CRITICAL only where a keyed
    # chain can be written; the encryption key has no production reader.
    critical_secrets: dict[str, SecretStr] = {}
    optional_secrets: dict[str, SecretStr] = {"encryption_key": secrets.encryption_key}
    if _audit_signing_key_required():
        critical_secrets["audit_signing_key"] = secrets.audit_signing_key
    else:
        optional_secrets["audit_signing_key"] = secrets.audit_signing_key
    important_secrets = {
        "database_password": secrets.database_password,
        "redis_password": secrets.redis_password,
    }
    optional_secrets.update(
        {
            "toss_secret_key": secrets.toss_secret_key,
            "slack_webhook_token": secrets.slack_webhook_token,
            "slack_bot_token": secrets.slack_bot_token,
            "pagerduty_api_key": secrets.pagerduty_api_key,
            "aws_access_key_id": secrets.aws_access_key_id,
            "aws_secret_access_key": secrets.aws_secret_access_key,
        }
    )

    from baldur.runtime import is_production

    production = is_production()

    result: dict[str, list[str]] = {
        "critical": _report_unset_secrets(
            critical_secrets,
            "security.critical_secret_set_system",
            "error" if production else "info",
        ),
        "warning": _report_unset_secrets(
            important_secrets,
            "security.important_secret_set_some",
            "warning" if production else "debug",
        ),
        "info": _report_unset_secrets(
            optional_secrets,
            "security.optional_secret_set",
            "info",
        ),
    }

    # In production, missing CRITICAL secrets must abort startup. The only
    # CRITICAL secret is the audit signing key, so the message names its
    # variable and the condition that made it required.
    if production and result["critical"]:
        raise ConfigurationError(
            "[Security] "
            "BALDUR_SECRETS_AUDIT_SIGNING_KEY is required in production "
            "while the audit trail is on (BALDUR_AUDIT_ENABLED) or a PRO "
            "entitlement is active: it keys every audit hash-chain entry, so "
            "an actor without it cannot forge one. Set it to a long random "
            "secret."
        )

    return result
