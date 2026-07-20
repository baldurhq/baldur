"""
IP and PII Masking Utilities.

Provides privacy-preserving data handling for audit logs.
Implements Privacy-by-Design principles for GDPR/CCPA compliance.

Role-based Masking:
    - MaskingLevel.CLIENT: for client responses - full replacement
      (***REDACTED***)
    - MaskingLevel.AUDIT: for internal audit - hashed (equality still
      checkable)
    - MaskingLevel.FORENSIC: for legal investigation - Fernet symmetric
      encryption (recoverable)

Levels accessible per RBAC role:
    - baldur_admin (priority 3): allowed up to FORENSIC
    - baldur_operator (priority 2): allowed up to AUDIT
    - baldur_viewer (priority 1): CLIENT only

Security Hardening (214_SECURITY_VULNERABILITY_FIXES):
    - FORENSIC level: SHA-256 hash → Fernet symmetric encryption
      (actually recoverable)
    - Falls back automatically to the AUDIT level when encryption_key
      is unset
"""

import base64
import hashlib
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger()

# =============================================================================
# Default Sensitive Keys (canonical source of truth)
# =============================================================================

DEFAULT_SENSITIVE_KEYS: list[str] = [
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "private_key",
    "credit_card",
    "ssn",
    "social_security",
    # PCI core
    "card_number",
    "cvv",
    "cvc",
    "iban",
    # Banking
    "account_number",
    "routing_number",
    # Identity
    "passport",
    "driver_license",
    "tax_id",
    # Cloud
    "access_key",
]

# =============================================================================
# MaskingLevel Enum (RBAC integration)
# =============================================================================


class MaskingLevel(str, Enum):
    """
    Masking level.

    Applies a different masking level depending on the RBAC role.

    - CLIENT: for client responses - full replacement (***REDACTED***)
    - AUDIT: for internal audit - SHA-256 hashed (equality still checkable)
    - FORENSIC: for legal investigation - stored encrypted (recoverable)
    """

    CLIENT = "client"
    """For client responses: full replacement (not recoverable)."""

    AUDIT = "audit"
    """For internal audit: SHA-256 hashed (equality checks only)."""

    FORENSIC = "forensic"
    """For legal investigation: Fernet symmetric encryption (recoverable)."""


def _get_forensic_fernet():
    """
    Return a Fernet instance for FORENSIC-level encryption.

    Security Hardening (214_SECURITY_VULNERABILITY_FIXES):
    - Builds the Fernet instance from SecretsSettings.encryption_key
    - Returns None when the key is unset (caller falls back to AUDIT)

    Returns:
        Fernet instance, or None when the key is unset
    """
    try:
        from baldur.settings.secrets import get_secrets

        secrets = get_secrets()
        key = secrets.encryption_key.get_secret_value()
        if not key:
            return None

        from cryptography.fernet import Fernet

        # Fernet requires a URL-safe base64-encoded 32-byte key.
        # If encryption_key is already in Fernet key form, use it as-is.
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except Exception:
            # Not a Fernet-form key: derive a 32-byte key via SHA-256,
            # then base64-encode it
            derived_key = hashlib.sha256(key.encode()).digest()
            fernet_key = base64.urlsafe_b64encode(derived_key)
            return Fernet(fernet_key)
    except ImportError:
        logger.warning("security.cryptography_unavailable")
        return None
    except Exception as e:
        logger.warning(
            "security.initialize_fernet_forensic_failed",
            error=e,
        )
        return None


def mask_with_level(
    value: str,
    level: MaskingLevel,
    salt: str | None = None,
) -> str:
    """
    Apply masking according to the masking level.

    Args:
        value: the original value to mask
        level: masking level (CLIENT, AUDIT, FORENSIC)
        salt: salt for hashing (used at the AUDIT level)

    Returns:
        The masked string

    Examples:
        >>> mask_with_level("admin@example.com", MaskingLevel.CLIENT)
        '***REDACTED***'
        >>> mask_with_level("admin@example.com", MaskingLevel.AUDIT)
        'sha256:a1b2c3d4e5f6...'
        >>> mask_with_level("admin@example.com", MaskingLevel.FORENSIC, salt="secret")
        'encrypted:...'
    """
    if not value:
        return ""

    if level == MaskingLevel.CLIENT:
        # Full replacement - not recoverable
        return "***REDACTED***"

    if level == MaskingLevel.AUDIT:
        # SHA-256 hash - equality checks only
        return hash_for_audit(value, salt)

    if level == MaskingLevel.FORENSIC:
        # Security Hardening (214_SECURITY_VULNERABILITY_FIXES):
        # Fernet symmetric encryption - actually recoverable
        fernet = _get_forensic_fernet()
        if fernet is not None:
            try:
                encrypted = fernet.encrypt(value.encode())
                return f"encrypted:{encrypted.decode()}"
            except Exception as e:
                logger.warning(
                    "security.fernet_encryption_failed_using",
                    error=e,
                )
                return _forensic_hmac_fallback(value, salt)
        else:
            # encryption_key unset: HMAC-based fallback (keeps the
            # "encrypted:" prefix)
            logger.debug("security.forensic_masking_unavailable_no")
            return _forensic_hmac_fallback(value, salt)

    # The default is the CLIENT level
    return "***REDACTED***"


def _forensic_hmac_fallback(value: str, salt: str | None = None) -> str:
    """HMAC-based fallback for the FORENSIC level.

    When Fernet encryption is unavailable, produces an irreversible
    encrypted-looking form via HMAC-SHA256. It keeps the "encrypted:"
    prefix so the FORENSIC-level API contract holds, but callers must be
    aware that this value cannot be decrypted.

    Args:
        value: the original value to mask
        salt: additional salt (optional)

    Returns:
        A string of the form 'encrypted:hmac:<base64-encoded HMAC>'
    """
    import hmac as _hmac

    key = (salt or "forensic-fallback-key").encode()
    digest = _hmac.new(key, value.encode(), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode()
    return f"encrypted:hmac:{encoded}"


def decrypt_forensic(encrypted_value: str) -> str:
    """
    Decrypt a FORENSIC-level encrypted value.

    Supported forms:
    - "encrypted:{fernet_token}" → Fernet decryption
    - "sha256:..." → one-way hash, not recoverable → ValueError
    - "encrypted:hmac:..." → HMAC fallback, not recoverable → ValueError

    Args:
        encrypted_value: encrypted string in "encrypted:..." form

    Returns:
        The decrypted original string

    Raises:
        ValueError: on a malformed value or a decryption failure
        RuntimeError: when encryption_key is unset
    """
    # Detect a legacy SHA-256 hash (data from before Fernet was introduced)
    if encrypted_value.startswith("sha256:"):
        raise ValueError(
            "This value was stored as a SHA-256 hash (pre-Fernet era). "
            "Hash values are one-way and cannot be decrypted. "
            "Original data is not recoverable."
        )

    if not encrypted_value.startswith("encrypted:"):
        raise ValueError(
            "Not a FORENSIC encrypted value (must start with 'encrypted:'). "
            f"Got prefix: '{encrypted_value[:20]}...'"
        )

    # Detect the HMAC fallback (output of _forensic_hmac_fallback).
    # When encryption_key is unset, mask_with_level(FORENSIC) falls back to
    # HMAC and produces the "encrypted:hmac:..." form. That value is not
    # recoverable.
    token = encrypted_value[len("encrypted:") :]
    if token.startswith("hmac:"):
        raise ValueError(
            "This value was stored as an HMAC hash (Fernet key was unavailable "
            "at encryption time). HMAC values are one-way and cannot be decrypted. "
            "Original data is not recoverable."
        )

    fernet = _get_forensic_fernet()
    if fernet is None:
        raise RuntimeError(
            "Cannot decrypt: encryption_key is not configured. "
            "Set BALDUR_SECRETS_ENCRYPTION_KEY environment variable."
        )

    try:
        decrypted = fernet.decrypt(token.encode())
        return decrypted.decode()
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}") from e


def get_masking_level_for_context() -> MaskingLevel:
    """
    Decide the masking level from the current ActorContext's RBAC role.

    Levels accessible per RBAC role:
        - baldur_admin (priority 3): FORENSIC
        - baldur_operator (priority 2): AUDIT
        - baldur_viewer (priority 1): CLIENT
        - no role: CLIENT (default)

    Returns:
        MaskingLevel (the highest level the current Actor may access)
    """
    try:
        from baldur.context.actor_context import (
            RBAC_ROLE_PRIORITY,
            ActorContext,
        )

        actor = ActorContext.get_current_or_none()

        if actor is None:
            return MaskingLevel.CLIENT

        highest_role = actor.highest_role
        priority = RBAC_ROLE_PRIORITY.get(highest_role, 0)

        # Decide the level from the priority
        if priority >= 3:  # baldur_admin
            return MaskingLevel.FORENSIC
        if priority >= 2:  # baldur_operator
            return MaskingLevel.AUDIT
        # baldur_viewer, or no role at all
        return MaskingLevel.CLIENT

    except ImportError:
        return MaskingLevel.CLIENT
    except Exception:
        return MaskingLevel.CLIENT


def mask_ip(ip: str, mask_last_octets: int = 2) -> str:
    """
    Mask an IP address for privacy compliance.

    IPv4: Masks last N octets with ***
    IPv6: Masks last N groups with ***

    Args:
        ip: The IP address to mask
        mask_last_octets: Number of octets/groups to mask (default: 2)

    Returns:
        Masked IP address (e.g., "192.168.***.***")

    Examples:
        >>> mask_ip("192.168.1.100")
        '192.168.***.***'
        >>> mask_ip("192.168.1.100", mask_last_octets=1)
        '192.168.1.***'
        >>> mask_ip("2001:0db8:85a3:0000:0000:8a2e:0370:7334")
        '2001:0db8:85a3:0000:0000:8a2e:***:***'
    """
    if not ip:
        return "unknown"

    ip = ip.strip()

    # Handle IPv6
    if ":" in ip:
        parts = ip.split(":")
        if len(parts) > mask_last_octets:
            masked_parts = parts[:-mask_last_octets] + ["***"] * mask_last_octets
            return ":".join(masked_parts)
        return ip

    # Handle IPv4
    parts = ip.split(".")
    if len(parts) == 4:
        if mask_last_octets >= 4:
            return "***.***.***.***"
        masked_parts = parts[:-mask_last_octets] + ["***"] * mask_last_octets
        return ".".join(masked_parts)

    # Unknown format, return as-is with partial masking
    return ip[: len(ip) // 2] + "***"


def mask_email(email: str) -> str:
    """
    Mask an email address for privacy compliance.

    Args:
        email: The email address to mask

    Returns:
        Masked email (e.g., "a***n@example.com")

    Examples:
        >>> mask_email("admin@example.com")
        'a***n@example.com'
        >>> mask_email("ab@example.com")
        'a***b@example.com'
    """
    if not email or "@" not in email:
        return "***@***.***"

    local, domain = email.rsplit("@", 1)

    if len(local) <= 2:
        masked_local = local[0] + "***" if local else "***"
    else:
        masked_local = local[0] + "***" + local[-1]

    return f"{masked_local}@{domain}"


def hash_for_audit(value: str, salt: str | None = None) -> str:
    """
    Create a SHA-256 hash of a value for audit purposes.

    This allows for later verification without storing the original value.
    Use a secret salt stored securely for reversibility in investigations.

    Args:
        value: The value to hash
        salt: Optional salt for the hash (store securely!)

    Returns:
        SHA-256 hash prefixed with "sha256:"

    Examples:
        >>> hash_for_audit("192.168.1.100")
        'sha256:a1b2c3...'
    """
    if not value:
        return "sha256:empty"

    data = value
    if salt:
        data = f"{salt}:{value}"

    hash_value = hashlib.sha256(data.encode()).hexdigest()
    return f"sha256:{hash_value[:16]}"  # Truncate for readability


def mask_sensitive_fields(data, sensitive_keys: list | None = None):
    """
    Mask sensitive fields in a dictionary.

    Args:
        data: Data containing potentially sensitive fields (dict, list, or primitive)
        sensitive_keys: List of keys to mask (default: common sensitive keys)

    Returns:
        Data with sensitive values masked
    """
    # Handle non-dict types
    if data is None:
        return None
    if not isinstance(data, (dict, list)):
        return data
    if isinstance(data, list):
        return [mask_sensitive_fields(item, sensitive_keys) for item in data]

    if sensitive_keys is None:
        sensitive_keys = DEFAULT_SENSITIVE_KEYS

    result: dict[str, Any] = {}
    for key, value in data.items():
        key_lower = key.lower()

        # Check if key matches sensitive patterns
        is_sensitive = any(s in key_lower for s in sensitive_keys)

        if is_sensitive:
            result[key] = "***REDACTED***"
        elif isinstance(value, dict):
            result[key] = mask_sensitive_fields(value, sensitive_keys)
        elif isinstance(value, list):
            result[key] = [
                (
                    mask_sensitive_fields(item, sensitive_keys)
                    if isinstance(item, dict)
                    else item
                )
                for item in value
            ]
        else:
            result[key] = value

    return result


def extract_ip_from_request(request) -> str:
    """
    Extract client IP from a Django request, handling proxies.

    Thin wrapper around :func:`baldur.utils.network.extract_client_ip`
    that preserves the original ``"unknown"`` default for backward
    compatibility with audit callers.

    Args:
        request: Django HttpRequest object

    Returns:
        Client IP address (``"unknown"`` when unresolvable)
    """
    from baldur.utils.network import extract_client_ip

    return extract_client_ip(request, default="unknown")  # type: ignore[return-value]
