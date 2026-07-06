"""
TLS Configuration Settings.

Provides TLS/SSL configuration used by compliance checks and connection setup.

Environment Variables:
    BALDUR_TLS_ENABLED=true
    BALDUR_TLS_CERTIFICATE_PATH=/etc/ssl/certs/server.crt
    BALDUR_TLS_KEY_PATH=/etc/ssl/private/server.key
    BALDUR_TLS_CA_BUNDLE_PATH=/etc/ssl/certs/ca-bundle.crt
    BALDUR_TLS_VERIFY_SSL=true
    BALDUR_TLS_MIN_VERSION=TLSv1.2
    BALDUR_TLS_VERIFY_MODE=CERT_REQUIRED
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = ["TLSConfig", "get_tls_config", "reset_tls_config"]


class TLSConfig(BaseSettings):
    """
    TLS/SSL configuration for secure connections.

    Controls certificate paths, verification settings, and minimum
    protocol version for all TLS-secured communication channels.
    """

    model_config = make_settings_config("BALDUR_TLS_")

    # ==========================================================================
    # Core TLS Settings
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable TLS for connections",
    )
    certificate_path: str | None = Field(
        default=None,
        description="Path to TLS certificate file (.crt/.pem)",
    )
    key_path: str | None = Field(
        default=None,
        description="Path to TLS private key file (.key/.pem)",
    )
    ca_bundle_path: str | None = Field(
        default=None,
        description="Path to CA bundle for certificate verification",
    )
    verify_ssl: bool = Field(
        default=True,
        description="Enable SSL certificate verification",
    )

    # ==========================================================================
    # Protocol Settings
    # ==========================================================================
    min_version: str = Field(
        default="TLSv1.2",
        description="Minimum TLS protocol version (e.g. TLSv1.2, TLSv1.3)",
    )
    verify_mode: str = Field(
        default="CERT_REQUIRED",
        description="SSL verification mode (CERT_NONE, CERT_OPTIONAL, CERT_REQUIRED)",
    )


# =============================================================================
# Singleton Pattern
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_tls_config, configure_tls_config, reset_tls_config = make_singleton_factory(
    "tls_config", TLSConfig
)
