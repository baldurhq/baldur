"""
IP and PII Masking Utilities.

Provides privacy-preserving data handling for audit logs.
Implements Privacy-by-Design principles for GDPR/CCPA compliance.

Role-based Masking:
    - MaskingLevel.CLIENT: 클라이언트 응답용 - 완전 치환 (***REDACTED***)
    - MaskingLevel.AUDIT: 내부 감사용 - 해시화 (동일성 확인 가능)
    - MaskingLevel.FORENSIC: 법적 조사용 - Fernet 대칭 암호화 (복원 가능)

RBAC 역할별 접근 가능 레벨:
    - baldur_admin (우선순위 3): FORENSIC까지 허용
    - baldur_operator (우선순위 2): AUDIT까지 허용
    - baldur_viewer (우선순위 1): CLIENT만 허용

Security Hardening (214_SECURITY_VULNERABILITY_FIXES):
    - FORENSIC 레벨: SHA-256 해시 → Fernet 대칭 암호화 (실제 복원 가능)
    - encryption_key 미설정 시 AUDIT 레벨로 자동 폴백
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
# MaskingLevel Enum (RBAC 연동)
# =============================================================================


class MaskingLevel(str, Enum):
    """
    마스킹 수준.

    RBAC 역할에 따라 다른 마스킹 수준을 적용합니다.

    - CLIENT: 클라이언트 응답용 - 완전 치환 (***REDACTED***)
    - AUDIT: 내부 감사용 - SHA-256 해시화 (동일성 확인 가능)
    - FORENSIC: 법적 조사용 - 암호화 저장 (복원 가능)
    """

    CLIENT = "client"
    """클라이언트 응답용: 완전 치환 (복원 불가)"""

    AUDIT = "audit"
    """내부 감사용: SHA-256 해시화 (동일성 확인만 가능)"""

    FORENSIC = "forensic"
    """법적 조사용: Fernet 대칭 암호화 (복원 가능)"""


def _get_forensic_fernet():
    """
    FORENSIC 레벨 암호화를 위한 Fernet 인스턴스 반환.

    Security Hardening (214_SECURITY_VULNERABILITY_FIXES):
    - SecretsSettings.encryption_key를 사용하여 Fernet 인스턴스 생성
    - 키 미설정 시 None 반환 (호출측에서 AUDIT 폴백)

    Returns:
        Fernet 인스턴스 또는 None (키 미설정 시)
    """
    try:
        from baldur.settings.secrets import get_secrets

        secrets = get_secrets()
        key = secrets.encryption_key.get_secret_value()
        if not key:
            return None

        from cryptography.fernet import Fernet

        # Fernet은 URL-safe base64 인코딩된 32바이트 키가 필요
        # encryption_key가 이미 Fernet 키 형식이면 그대로 사용
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except Exception:
            # 키가 Fernet 형식이 아니면 SHA-256으로 32바이트 키 생성 후 base64 변환
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
    마스킹 수준에 따른 마스킹 적용.

    Args:
        value: 마스킹할 원본 값
        level: 마스킹 수준 (CLIENT, AUDIT, FORENSIC)
        salt: 해시용 솔트 (AUDIT 레벨에서 사용)

    Returns:
        마스킹된 문자열

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
        # 완전 치환 - 복원 불가
        return "***REDACTED***"

    if level == MaskingLevel.AUDIT:
        # SHA-256 해시 - 동일성 확인만 가능
        return hash_for_audit(value, salt)

    if level == MaskingLevel.FORENSIC:
        # Security Hardening (214_SECURITY_VULNERABILITY_FIXES):
        # Fernet 대칭 암호화 - 실제 복원 가능
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
            # encryption_key 미설정 시 HMAC 기반 폴백 (encrypted: 접두사 유지)
            logger.debug("security.forensic_masking_unavailable_no")
            return _forensic_hmac_fallback(value, salt)

    # 기본값은 CLIENT 레벨
    return "***REDACTED***"


def _forensic_hmac_fallback(value: str, salt: str | None = None) -> str:
    """FORENSIC 레벨 HMAC 기반 폴백.

    Fernet 암호화가 불가능할 때 HMAC-SHA256으로 비가역 암호화 형태를 생성.
    encrypted: 접두사를 유지하여 FORENSIC 레벨 API 계약을 보장하되,
    이 값은 복호화가 불가능함을 인지해야 한다.

    Args:
        value: 마스킹할 원본 값
        salt: 추가 솔트 (선택)

    Returns:
        'encrypted:hmac:<base64-encoded HMAC>' 형식 문자열
    """
    import hmac as _hmac

    key = (salt or "forensic-fallback-key").encode()
    digest = _hmac.new(key, value.encode(), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode()
    return f"encrypted:hmac:{encoded}"


def decrypt_forensic(encrypted_value: str) -> str:
    """
    FORENSIC 레벨로 암호화된 값을 복호화.

    지원 형식:
    - "encrypted:{fernet_token}" → Fernet 복호화
    - "sha256:..." → 단방향 해시, 복원 불가 → ValueError
    - "encrypted:hmac:..." → HMAC fallback, 복원 불가 → ValueError

    Args:
        encrypted_value: "encrypted:..." 형식의 암호화된 문자열

    Returns:
        복호화된 원본 문자열

    Raises:
        ValueError: 잘못된 형식이거나 복호화 실패 시
        RuntimeError: encryption_key 미설정 시
    """
    # 레거시 SHA-256 해시 감지 (Fernet 도입 이전 데이터)
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

    # HMAC fallback 감지 (_forensic_hmac_fallback 출력)
    # encryption_key 미설정 시 mask_with_level(FORENSIC)이 HMAC으로 폴백하며
    # "encrypted:hmac:..." 형식을 생성한다. 이 값은 복원 불가.
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
    현재 ActorContext의 RBAC 역할에 따른 마스킹 레벨 결정.

    RBAC 역할별 접근 가능 레벨:
        - baldur_admin (우선순위 3): FORENSIC
        - baldur_operator (우선순위 2): AUDIT
        - baldur_viewer (우선순위 1): CLIENT
        - 역할 없음: CLIENT (기본값)

    Returns:
        MaskingLevel (현재 Actor가 접근 가능한 최대 레벨)
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

        # 우선순위에 따른 레벨 결정
        if priority >= 3:  # baldur_admin
            return MaskingLevel.FORENSIC
        if priority >= 2:  # baldur_operator
            return MaskingLevel.AUDIT
        # baldur_viewer 또는 역할 없음
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
