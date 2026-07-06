"""
Forensic Settings - Pydantic v2.

Single Source of Truth for forensic context configuration.

Replaces:
- core/config.py:ForensicConfig (lines 188-211)
- core/safe_defaults.py:SAFE_DEFAULTS["forensic"]
- core/safe_defaults.py:VALIDATION_RULES["forensic"]

Environment Variables:
    BALDUR_FORENSIC_ERROR_MESSAGE_MAX_LENGTH=500
    BALDUR_FORENSIC_MAX_STACK_FRAMES=50

Reference:
- docs/baldur/middleware_system/40_PYDANTIC_CONFIG_MIGRATION.md
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ForensicSettings(BaseSettings):
    """
    Forensic context truncation limits with validation.

    All defaults match core/config.py:ForensicConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["forensic"]
    """

    model_config = make_settings_config("BALDUR_FORENSIC_")

    # ==========================================================================
    # Truncation Limits (from core/config.py lines 200-202)
    # Validation rules from core/safe_defaults.py lines 269-273
    # ==========================================================================
    error_message_max_length: int = Field(
        default=500,
        ge=50,
        le=5000,
        description="Maximum length for error messages",
    )
    response_body_max_length: int = Field(
        default=5000,
        ge=100,
        le=100000,
        description="Maximum length for response body capture",
    )
    user_agent_max_length: int = Field(
        default=500,
        ge=50,
        le=2000,
        description="Maximum length for user agent strings",
    )

    # ==========================================================================
    # Stack Frame Settings
    # ==========================================================================
    max_stack_frames: int = Field(
        default=50,
        ge=10,
        le=200,
        description="Maximum stack frames to capture",
    )
    max_context_size_bytes: int = Field(
        default=65536,  # 64KB
        ge=1024,
        le=1048576,  # 1MB
        description="Maximum context size in bytes",
    )
    include_local_variables: bool = Field(
        default=False,
        description="Include local variables in stack traces (security risk)",
    )
    sanitize_sensitive_data: bool = Field(
        default=True,
        description="Sanitize sensitive data in forensic context",
    )
    sensitive_key_patterns: list[str] = Field(
        default_factory=lambda: ["password", "secret", "token", "key", "auth"],
        description="Patterns to match sensitive keys for sanitization",
    )

    # ==========================================================================
    # Audit Routing
    # ==========================================================================
    audit_enabled: bool = Field(
        default=True,
        description=(
            "Forward captured forensic context to audit log via "
            "baldur.audit.forensic_recorder. Disable to stop emitting "
            "FORENSIC_CAPTURE_COMPLETED audit entries while keeping context "
            "capture itself active."
        ),
    )

    # ==========================================================================
    # Data Collection (from config.py ForensicContextConfig)
    # ==========================================================================
    max_stacktrace_length: int = Field(
        default=10000,
        ge=1000,
        le=100000,
        description="Maximum stacktrace string length",
    )
    collect_request_body: bool = Field(
        default=False,
        description="Collect request body in forensic context",
    )
    collect_response_body: bool = Field(
        default=False,
        description="Collect response body in forensic context",
    )

    # ==========================================================================
    # Sensitive Field Masking (from config.py ForensicContextConfig)
    # ==========================================================================
    mask_sensitive_fields: bool = Field(
        default=True,
        description="Enable masking of sensitive fields in forensic context",
    )
    sensitive_field_patterns: tuple[str, ...] = (
        # Authentication & Secrets
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "credential",
        "private_key",
        "access_key",
        "secret_key",
        # Payment related
        "card_number",
        "cvv",
        "cvc",
        "credit_card",
        # Internal infrastructure (should not be exposed in logs)
        "internal_ip",
        "server_path",
        "db_password",
        "redis_password",
        "connection_string",
    )

    # IP address masking patterns (regex)
    mask_internal_ip: bool = Field(
        default=True,
        description="Enable masking of internal IP addresses",
    )
    internal_ip_patterns: tuple[str, ...] = (
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}",  # 10.0.0.0/8
        r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}",  # 172.16.0.0/12
        r"192\.168\.\d{1,3}\.\d{1,3}",  # 192.168.0.0/16
    )

    # Server path masking patterns
    mask_server_paths: bool = Field(
        default=True,
        description="Enable masking of server paths",
    )
    server_path_patterns: tuple[str, ...] = (
        r"/home/[^/]+",  # Home directories
        r"/var/[^/]+/[^/]+",  # Var subdirectories
        r"/etc/[^/]+",  # Config files
        r"[A-Z]:\\Users\\[^\\]+",  # Windows user paths
        r"/app/[^/]+/[^/]+",  # Container app paths
    )


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_forensic_settings() -> "ForensicSettings":
    from baldur.settings.root import get_config

    return get_config().services_group.forensic


def reset_forensic_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["forensic"]
    except KeyError:
        pass


def get_forensic_settings_safe() -> "ForensicSettings":
    """Get forensic settings with environment variable drift detection."""
    from baldur.settings.drift_monitor import get_config_drift_monitor

    monitor = get_config_drift_monitor()
    if monitor.check_and_invalidate("forensic", "BALDUR_FORENSIC_"):
        reset_forensic_settings()
    return get_forensic_settings()
