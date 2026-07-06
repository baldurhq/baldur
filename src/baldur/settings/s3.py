"""
S3 Object Storage Settings - Pydantic v2.

Environment Variables:
    BALDUR_S3_ENDPOINT_URL=https://s3.amazonaws.com
    BALDUR_S3_BUCKET_NAME=baldur-data
    BALDUR_S3_REGION=us-east-1
    BALDUR_S3_ACCESS_KEY_ID=<secret>
    BALDUR_S3_SECRET_ACCESS_KEY=<secret>
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = ["S3Settings", "get_s3_settings", "reset_s3_settings"]


class S3Settings(BaseSettings):
    """
    S3-compatible object storage settings.

    Provides endpoint, bucket, region, and credential configuration
    for S3-compatible storage backends (AWS S3, MinIO, etc.).
    """

    model_config = make_settings_config("BALDUR_S3_")

    # ==========================================================================
    # Connection Settings
    # ==========================================================================
    endpoint_url: str | None = Field(
        default=None,
        description="S3 endpoint URL (None for default AWS endpoint)",
    )
    bucket_name: str = Field(
        default="baldur-data",
        description="S3 bucket name for data storage",
    )
    region: str = Field(
        default="us-east-1",
        description="AWS region for S3 bucket",
    )

    # ==========================================================================
    # Authentication (masked in logs)
    # ==========================================================================
    access_key_id: SecretStr = Field(
        default=SecretStr(""),
        description="S3 access key ID (masked in logs)",
    )
    secret_access_key: SecretStr = Field(
        default=SecretStr(""),
        description="S3 secret access key (masked in logs)",
    )


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_s3_settings() -> S3Settings:
    """Get singleton S3Settings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(S3Settings)


def reset_s3_settings() -> None:
    """Reset S3Settings singleton (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(S3Settings)
