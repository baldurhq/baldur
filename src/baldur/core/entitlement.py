"""
Entitlement Validator — Ed25519 subscription validation.

Validates PRO subscription tokens at startup and via periodic TTL-based
re-checks. Follows the Sidekiq model: binary check, no grace period.

Entitlement format (JSON):
    {
        "alg": "ed25519",
        "payload": {
            "customer_id": "cust_a1b2c3d4",
            "org": "acme-corp",
            "tier": "PRO",
            "plan": "monthly",
            "issued_at": "2026-04-01",
            "expires": "2026-05-01"
        },
        "signature": "base64..."
    }
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.exceptions import BaldurError

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

__all__ = [
    "EntitlementStatus",
    "EntitlementClaims",
    "EntitlementError",
    "EntitlementResult",
    "get_entitlement_status",
    "reset_entitlement_status",
]

# TTL for cached validation result (24 hours)
_RECHECK_TTL_SECONDS = 86400


class EntitlementStatus(str, Enum):
    """Entitlement validation status."""

    ACTIVE = "active"
    INVALID = "invalid"
    MISSING = "missing"


class EntitlementError(BaldurError):
    """Entitlement validation error."""

    def __init__(
        self,
        message: str = "",
        *,
        reason: str = "",
        code: str = "",
    ):
        super().__init__(message, code=code)
        self.reason = reason

    def extra_context(self) -> dict[str, Any]:
        ctx = super().extra_context()
        if self.reason:
            ctx["reason"] = self.reason
        return ctx


@dataclass(frozen=True)
class EntitlementClaims:
    """Decoded entitlement token claims."""

    customer_id: str
    org: str
    tier: str
    plan: str
    issued_at: str
    expires: str

    @property
    def expiry_date(self) -> date:
        """Parse expires field as date."""
        return datetime.strptime(self.expires, "%Y-%m-%d").date()

    @property
    def days_until_expiry(self) -> int:
        """Days until expiry (negative = past due)."""
        return (self.expiry_date - date.today()).days

    @property
    def is_expired(self) -> bool:
        """Check if token has expired."""
        return self.days_until_expiry < 0


@dataclass(frozen=True)
class EntitlementResult:
    """Result of entitlement validation."""

    status: EntitlementStatus
    claims: EntitlementClaims | None = None

    @property
    def is_active(self) -> bool:
        return self.status == EntitlementStatus.ACTIVE


class _EntitlementValidator:
    """Ed25519 entitlement validator with TTL-based caching.

    Validates at startup; on next access after 24h, re-verifies signature.
    Thread-safe via monotonic timestamp check (read is atomic for floats).
    """

    def __init__(self) -> None:
        self._cached_result: EntitlementResult | None = None
        self._last_checked: float = 0.0

    def validate(self, *, force: bool = False) -> EntitlementResult:
        """Validate entitlement token.

        Returns cached result if within TTL, otherwise re-validates.
        """
        now = time.monotonic()
        if (
            not force
            and self._cached_result is not None
            and (now - self._last_checked) < _RECHECK_TTL_SECONDS
        ):
            return self._cached_result

        result = self._do_validate()
        self._cached_result = result
        self._last_checked = now
        self._log_result(result)
        self._update_metrics(result)
        return result

    def _do_validate(self) -> EntitlementResult:
        """Perform actual Ed25519 validation."""
        from baldur.settings.license import get_entitlement_settings

        settings = get_entitlement_settings()

        # Load token from key or file
        token_str = self._load_token(settings.key, settings.file)
        if not token_str:
            return EntitlementResult(status=EntitlementStatus.MISSING)

        # Parse token JSON
        try:
            token_data = json.loads(
                base64.b64decode(token_str) if self._is_base64(token_str) else token_str
            )
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return EntitlementResult(status=EntitlementStatus.INVALID)

        # Verify structure
        if not isinstance(token_data, dict):
            return EntitlementResult(status=EntitlementStatus.INVALID)

        payload = token_data.get("payload")
        signature_b64 = token_data.get("signature")
        if not payload or not signature_b64:
            return EntitlementResult(status=EntitlementStatus.INVALID)

        # Extract claims
        try:
            claims = EntitlementClaims(
                customer_id=payload["customer_id"],
                org=payload["org"],
                tier=payload["tier"],
                plan=payload["plan"],
                issued_at=payload["issued_at"],
                expires=payload["expires"],
            )
        except (KeyError, TypeError):
            return EntitlementResult(status=EntitlementStatus.INVALID)

        # Verify Ed25519 signature
        if not self._verify_signature(payload, signature_b64):
            return EntitlementResult(status=EntitlementStatus.INVALID, claims=claims)

        # Check expiry
        if claims.is_expired:
            return EntitlementResult(status=EntitlementStatus.INVALID, claims=claims)

        return EntitlementResult(status=EntitlementStatus.ACTIVE, claims=claims)

    def _load_token(self, license_key: str, license_file: str) -> str:
        """Load entitlement token from key or file."""
        if license_key:
            return license_key

        if license_file:
            try:
                return Path(license_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning(
                    "entitlement.file_read_failed",
                    path=license_file,
                    error=str(exc),
                )
                return ""

        return ""

    @staticmethod
    def _is_base64(s: str) -> bool:
        """Heuristic: check if string looks like base64-encoded JSON."""
        try:
            if s.startswith("{"):
                return False
            base64.b64decode(s, validate=True)
            return True
        except Exception:
            return False

    @staticmethod
    def _verify_signature(payload: dict[str, Any], signature_b64: str) -> bool:
        """Verify Ed25519 signature over payload.

        The public key is expected to be embedded in baldur_pro._entitlement.
        In the OSS package (without baldur_pro), there is no public key,
        so verification always returns False — which is correct because
        OSS mode does not need entitlement validation.
        """
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
        except ImportError:
            logger.debug("entitlement.cryptography_unavailable")
            return False

        try:
            # Load public key from baldur_pro (PRO package)
            from baldur_pro._entitlement import PUBLIC_KEY_BYTES
        except ImportError:
            # baldur_pro not installed — no public key available.
            # This is expected in OSS mode.
            logger.debug("entitlement.no_public_key")
            return False

        try:
            public_key = Ed25519PublicKey.from_public_bytes(PUBLIC_KEY_BYTES)
            # Canonical form: sorted keys, compact separators, UTF-8, no ASCII
            # escaping. Must byte-match the license-issuing worker's signer so
            # tokens signed off-platform verify here (non-ASCII org names too).
            payload_bytes = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            signature = base64.b64decode(signature_b64)
            public_key.verify(signature, payload_bytes)
            return True
        except Exception:
            return False

    @staticmethod
    def _log_result(result: EntitlementResult) -> None:
        """Log the validation result as a structured ``entitlement.*`` event.

        An invalid token logs at WARNING; an active or absent one at INFO.

        Best-effort: a console that cannot encode a non-ASCII field (e.g. a
        cp949 Windows terminal rendering a non-ASCII org name) must never
        crash entitlement validation, which runs on the startup path.
        """
        try:
            if result.status == EntitlementStatus.ACTIVE:
                claims = result.claims
                logger.info(
                    "entitlement.token_validated",
                    customer_id=claims.customer_id if claims else "",
                    org=claims.org if claims else "",
                    tier=claims.tier if claims else "",
                    expires=claims.expires if claims else "",
                )
            elif result.status == EntitlementStatus.INVALID:
                logger.warning(
                    "entitlement.token_invalid",
                    reason="signature_invalid_or_expired",
                )
            else:
                logger.info("entitlement.token_missing")
        except Exception:
            # Logging must not break validation; the result is already computed.
            pass

    @staticmethod
    def _update_metrics(result: EntitlementResult) -> None:
        """Update Prometheus metrics for entitlement status."""
        try:
            from baldur.metrics.recorders.entitlement import (
                set_entitlement_expiry_days,
                set_entitlement_status,
            )

            status_value = {
                EntitlementStatus.MISSING: 0,
                EntitlementStatus.INVALID: 1,
                EntitlementStatus.ACTIVE: 2,
            }[result.status]
            set_entitlement_status(status_value)

            if result.claims:
                set_entitlement_expiry_days(result.claims.days_until_expiry)
            else:
                set_entitlement_expiry_days(-1)
        except Exception:
            pass


# =============================================================================
# Singleton
# =============================================================================

_validator: _EntitlementValidator | None = None


def get_entitlement_status(*, force: bool = False) -> EntitlementResult:
    """Return current entitlement status (cached with 24h TTL).

    First call validates the token. Subsequent calls within 24h
    return the cached result. After 24h, re-validates on next access.

    Args:
        force: If True, bypass TTL cache and re-validate immediately.
    """
    global _validator
    if _validator is None:
        _validator = _EntitlementValidator()
    return _validator.validate(force=force)


def reset_entitlement_status() -> None:
    """Reset entitlement validator singleton (for testing)."""
    global _validator
    _validator = None
