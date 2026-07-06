"""
Secrets Settings - SecretStr 기반 민감 정보 설정.

Pydantic SecretStr 특징:
- repr(): '**********' 출력
- str(): '**********' 출력
- get_secret_value(): 실제 값 반환

이점:
- print(settings) 시 자동 마스킹
- JSON 로깅 시 자동 마스킹
- 감사(Audit) 로그 안전

Security Hardening (214_SECURITY_VULNERABILITY_FIXES):
- validate_required_secrets() 추가: 핵심 시크릿 미설정 시 경고/에러
"""

import structlog
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class SecretsSettings(BaseSettings):
    """
    민감 정보 전용 설정.

    모든 비밀번호, API 키, 토큰은 이 클래스에서 관리합니다.
    SecretStr을 사용하여 로깅 시 자동으로 마스킹됩니다.

    Environment variables:
        BALDUR_SECRETS_DATABASE_PASSWORD=...
        BALDUR_SECRETS_REDIS_PASSWORD=...
        BALDUR_SECRETS_TOSS_SECRET_KEY=...
        BALDUR_SECRETS_SLACK_WEBHOOK_TOKEN=...
        BALDUR_SECRETS_ENCRYPTION_KEY=...

    Usage:
        from baldur.settings.secrets import get_secrets

        secrets = get_secrets()

        # 안전한 출력 (마스킹됨)
        print(secrets)  # database_password=SecretStr('**********')

        # 실제 값 접근
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
        """Database password가 설정되었는지 확인."""
        return bool(self.database_password.get_secret_value())

    def has_redis_password(self) -> bool:
        """Redis password가 설정되었는지 확인."""
        return bool(self.redis_password.get_secret_value())

    def has_toss_secret(self) -> bool:
        """Toss secret key가 설정되었는지 확인."""
        return bool(self.toss_secret_key.get_secret_value())

    def has_slack_webhook(self) -> bool:
        """Slack webhook token이 설정되었는지 확인."""
        return bool(self.slack_webhook_token.get_secret_value())

    def get_masked_summary(self) -> dict:
        """
        모든 시크릿의 마스킹된 요약 반환.

        Returns:
            {field_name: is_set (bool)} 딕셔너리
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
    핵심 시크릿이 설정되었는지 검증.

    Security Hardening (214_SECURITY_VULNERABILITY_FIXES):
    - CRITICAL 시크릿 (encryption_key, audit_signing_key): 미설정 시 ERROR 로그
    - IMPORTANT 시크릿 (database_password, redis_password): 미설정 시 WARNING 로그
    - OPTIONAL 시크릿: 미설정 시 INFO 로그

    프로덕션 환경에서 CRITICAL 시크릿 미설정 시 RuntimeError 발생.

    Args:
        secrets: 검증할 SecretsSettings 인스턴스 (None이면 싱글톤 사용)

    Returns:
        {"critical": [...], "warning": [...], "info": [...]} 미설정 시크릿 목록

    Raises:
        RuntimeError: 프로덕션에서 CRITICAL 시크릿 미설정 시
    """
    if secrets is None:
        secrets = get_secrets()

    # 시크릿 분류
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

    # CRITICAL 시크릿 검증
    for name, secret in critical_secrets.items():
        if not secret.get_secret_value():
            result["critical"].append(name)
            logger.error(
                "security.critical_secret_set_system",
                secret_name=name,
            )

    # IMPORTANT 시크릿 검증
    for name, secret in important_secrets.items():
        if not secret.get_secret_value():
            result["warning"].append(name)
            logger.warning(
                "security.important_secret_set_some",
                secret_name=name,
            )

    # OPTIONAL 시크릿 검증
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
