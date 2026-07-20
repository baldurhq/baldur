"""
TLS/SSL Error Handler

Handles TLS-related failures gracefully:
- Certificate expiration
- Certificate validation failures
- Handshake timeouts
- Protocol version mismatches

Provides:
- Error classification
- Retry strategies for transient failures
- Alert mechanisms for certificate issues
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class TLSErrorType(str, Enum):
    """Classification of TLS errors"""

    CERTIFICATE_EXPIRED = "cert_expired"
    CERTIFICATE_NOT_YET_VALID = "cert_not_yet_valid"
    CERTIFICATE_REVOKED = "cert_revoked"
    CERTIFICATE_HOSTNAME_MISMATCH = "cert_hostname_mismatch"
    CERTIFICATE_SELF_SIGNED = "cert_self_signed"
    CERTIFICATE_CHAIN_INVALID = "cert_chain_invalid"
    HANDSHAKE_TIMEOUT = "handshake_timeout"
    HANDSHAKE_FAILURE = "handshake_failure"
    PROTOCOL_VERSION_MISMATCH = "protocol_mismatch"
    CONNECTION_RESET = "connection_reset"
    UNKNOWN = "unknown"


class TLSErrorSeverity(str, Enum):
    """Severity level for TLS errors"""

    CRITICAL = "critical"  # Immediate alert needed (certificate expired)
    HIGH = "high"  # Prompt action needed
    MEDIUM = "medium"  # Monitoring needed
    LOW = "low"  # Transient, resolvable by retry


@dataclass
class TLSErrorInfo:
    """Detailed TLS error information"""

    error_type: TLSErrorType
    severity: TLSErrorSeverity
    endpoint: str
    error_message: str
    is_retryable: bool
    detected_at: datetime
    certificate_expiry: datetime | None = None
    days_until_expiry: int | None = None
    recommended_action: str = ""

    @property
    def is_certificate_error(self) -> bool:
        """True if this is a certificate-related error."""
        return self.error_type in {
            TLSErrorType.CERTIFICATE_EXPIRED,
            TLSErrorType.CERTIFICATE_NOT_YET_VALID,
            TLSErrorType.CERTIFICATE_REVOKED,
            TLSErrorType.CERTIFICATE_HOSTNAME_MISMATCH,
            TLSErrorType.CERTIFICATE_SELF_SIGNED,
            TLSErrorType.CERTIFICATE_CHAIN_INVALID,
        }

    @property
    def requires_immediate_action(self) -> bool:
        """True if this requires immediate attention."""
        return self.severity in {TLSErrorSeverity.CRITICAL, TLSErrorSeverity.HIGH}


@dataclass(frozen=True)
class _ErrorPattern:
    """Pattern for matching TLS errors."""

    error_type: TLSErrorType
    severity: TLSErrorSeverity
    is_retryable: bool
    recommended_action: str
    patterns: tuple[str, ...]  # Simple patterns - all must match if multiple
    any_patterns: tuple[str, ...] = ()  # Any of these patterns must match


# Error patterns ordered by specificity (most specific first)
_TLS_ERROR_PATTERNS: tuple[_ErrorPattern, ...] = (
    _ErrorPattern(
        error_type=TLSErrorType.CERTIFICATE_EXPIRED,
        severity=TLSErrorSeverity.CRITICAL,
        is_retryable=False,
        recommended_action="Renew certificate immediately",
        patterns=(),
        any_patterns=("certificate has expired", "cert_has_expired"),
    ),
    _ErrorPattern(
        error_type=TLSErrorType.CERTIFICATE_NOT_YET_VALID,
        severity=TLSErrorSeverity.HIGH,
        is_retryable=False,
        recommended_action="Check system clock or certificate dates",
        patterns=(),
        any_patterns=("certificate is not yet valid", "cert_not_yet_valid"),
    ),
    _ErrorPattern(
        error_type=TLSErrorType.CERTIFICATE_REVOKED,
        severity=TLSErrorSeverity.CRITICAL,
        is_retryable=False,
        recommended_action="Obtain new certificate - current one is revoked",
        patterns=(),
        any_patterns=("certificate revoked", "cert_revoked"),
    ),
    _ErrorPattern(
        error_type=TLSErrorType.CERTIFICATE_HOSTNAME_MISMATCH,
        severity=TLSErrorSeverity.HIGH,
        is_retryable=False,
        recommended_action="Check endpoint URL and certificate SAN",
        patterns=("hostname",),
        any_patterns=("mismatch", "doesn't match"),
    ),
    _ErrorPattern(
        error_type=TLSErrorType.CERTIFICATE_SELF_SIGNED,
        severity=TLSErrorSeverity.MEDIUM,
        is_retryable=False,
        recommended_action="Use CA-signed certificate or add to trust store",
        patterns=(),
        any_patterns=("self signed", "self-signed"),
    ),
    _ErrorPattern(
        error_type=TLSErrorType.CERTIFICATE_CHAIN_INVALID,
        severity=TLSErrorSeverity.HIGH,
        is_retryable=False,
        recommended_action="Check intermediate certificates in chain",
        patterns=(),
        any_patterns=("certificate chain", "unable to get local issuer certificate"),
    ),
    _ErrorPattern(
        error_type=TLSErrorType.HANDSHAKE_TIMEOUT,
        severity=TLSErrorSeverity.MEDIUM,
        is_retryable=True,
        recommended_action="Check network connectivity and firewall",
        patterns=("handshake",),
        any_patterns=("timeout", "timed out"),
    ),
    _ErrorPattern(
        error_type=TLSErrorType.CONNECTION_RESET,
        severity=TLSErrorSeverity.MEDIUM,
        is_retryable=True,
        recommended_action="Retry with backoff",
        patterns=(),
        any_patterns=("connection reset", "econnreset"),
    ),
    _ErrorPattern(
        error_type=TLSErrorType.PROTOCOL_VERSION_MISMATCH,
        severity=TLSErrorSeverity.HIGH,
        is_retryable=False,
        recommended_action="Check TLS version compatibility",
        patterns=("protocol",),
        any_patterns=("version", "unsupported"),
    ),
    _ErrorPattern(
        error_type=TLSErrorType.HANDSHAKE_FAILURE,
        severity=TLSErrorSeverity.MEDIUM,
        is_retryable=True,
        recommended_action="Check TLS configuration and cipher suites",
        patterns=("handshake", "fail"),
        any_patterns=(),
    ),
)


class TLSErrorClassifier:
    """Classifies TLS/SSL errors using pattern matching."""

    @staticmethod
    def _matches_pattern(error_str: str, pattern: _ErrorPattern) -> bool:
        """Check if error string matches a pattern."""
        # All required patterns must be present
        if not all(p in error_str for p in pattern.patterns):
            return False
        # If any_patterns specified, at least one must match
        if pattern.any_patterns and not any(
            p in error_str for p in pattern.any_patterns
        ):
            return False
        # If no any_patterns, patterns alone are sufficient (if non-empty)
        return bool(pattern.patterns) or bool(pattern.any_patterns)

    @staticmethod
    def _create_error_info(
        pattern: _ErrorPattern,
        endpoint: str,
        error_message: str,
        detected_at: datetime,
    ) -> TLSErrorInfo:
        """Create TLSErrorInfo from a matched pattern."""
        return TLSErrorInfo(
            error_type=pattern.error_type,
            severity=pattern.severity,
            endpoint=endpoint,
            error_message=error_message,
            is_retryable=pattern.is_retryable,
            detected_at=detected_at,
            recommended_action=pattern.recommended_action,
        )

    @classmethod
    def classify(cls, error: Exception, endpoint: str = "") -> TLSErrorInfo:
        """
        Classify an SSL/TLS error.

        Args:
            error: The exception to classify
            endpoint: The endpoint URL where error occurred

        Returns:
            TLSErrorInfo with classification details
        """
        error_str = str(error).lower()
        error_message = str(error)
        now = utc_now()

        # Try to match against known patterns
        for pattern in _TLS_ERROR_PATTERNS:
            if cls._matches_pattern(error_str, pattern):
                return cls._create_error_info(pattern, endpoint, error_message, now)

        # Default: unknown
        return TLSErrorInfo(
            error_type=TLSErrorType.UNKNOWN,
            severity=TLSErrorSeverity.MEDIUM,
            endpoint=endpoint,
            error_message=error_message,
            is_retryable=True,
            detected_at=now,
            recommended_action="Investigate error details",
        )
