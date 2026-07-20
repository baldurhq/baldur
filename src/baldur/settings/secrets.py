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
- validate_required_secrets(): warns/errors when core secrets are unset
"""

import structlog
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

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


def validate_required_secrets(secrets: SecretsSettings | None = None) -> dict:
    """
    Verify that the core secrets are configured.

    Security hardening:
    - CRITICAL secrets (encryption_key, audit_signing_key): ERROR log when unset
    - IMPORTANT secrets (database_password, redis_password): WARNING log when unset
    - OPTIONAL secrets: INFO log when unset

    In production, a missing CRITICAL secret raises RuntimeError.

    Args:
        secrets: SecretsSettings instance to validate (uses the singleton if None)

    Returns:
        {"critical": [...], "warning": [...], "info": [...]} list of unset secrets

    Raises:
        RuntimeError: When a CRITICAL secret is unset in production
    """
    if secrets is None:
        secrets = get_secrets()

    # Secret classification
    critical_secrets = {
        "encryption_key": secrets.encryption_key,
        "audit_signing_key": secrets.audit_signing_key,
    }
    important_secrets = {
        "database_password": secrets.database_password,
        "redis_password": secrets.redis_password,
    }
    optional_secrets = {
        "toss_secret_key": secrets.toss_secret_key,
        "slack_webhook_token": secrets.slack_webhook_token,
        "slack_bot_token": secrets.slack_bot_token,
        "pagerduty_api_key": secrets.pagerduty_api_key,
        "aws_access_key_id": secrets.aws_access_key_id,
        "aws_secret_access_key": secrets.aws_secret_access_key,
    }

    result: dict[str, list[str]] = {"critical": [], "warning": [], "info": []}

    # CRITICAL secret validation
    for name, secret in critical_secrets.items():
        if not secret.get_secret_value():
            result["critical"].append(name)
            logger.error(
                "security.critical_secret_set_system",
                secret_name=name,
            )

    # IMPORTANT secret validation
    for name, secret in important_secrets.items():
        if not secret.get_secret_value():
            result["warning"].append(name)
            logger.warning(
                "security.important_secret_set_some",
                secret_name=name,
            )

    # OPTIONAL secret validation
    for name, secret in optional_secrets.items():
        if not secret.get_secret_value():
            result["info"].append(name)
            logger.info(
                "security.optional_secret_set",
                secret_name=name,
            )

    # In production, missing CRITICAL secrets must abort startup.
    from baldur.runtime import is_production

    if is_production() and result["critical"]:
        raise RuntimeError(
            f"[Security] CRITICAL secrets not configured in production: "
            f"{', '.join(result['critical'])}. "
            "Cannot start Baldur system without these secrets."
        )

    return result
