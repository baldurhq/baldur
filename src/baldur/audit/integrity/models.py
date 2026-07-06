"""
Integrity Models and Core Functions.

Contains:
- IntegrityInfo: Dataclass for integrity information
- compute_hash: keyed-or-keyless chain hash for dictionaries
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from baldur.utils.serialization import fast_canonical_dumps


@dataclass
class IntegrityInfo:
    """Integrity information for a log entry."""

    sequence: int
    previous_hash: str
    current_hash: str
    timestamp: str


def _audit_signing_key() -> bytes | None:
    """Return the configured audit signing key as bytes, or None when unset.

    Read lazily inside the function (mirroring the FORENSIC Fernet key lookup
    in ``audit/masking.py``) so the import graph stays acyclic and the key is
    re-read fresh on every call — never module-cached. An empty-string key is
    treated as unset (falsy), the same invariant the production boot gate
    enforces, so an empty key never silently selects HMAC mode.
    """
    from baldur.settings.secrets import get_secrets

    key = get_secrets().audit_signing_key.get_secret_value()
    return key.encode() if key else None


def compute_hash(
    data: dict[str, Any],
) -> str:  # verified-by: test_forge_without_key_fails
    """
    Compute the chain hash of a dictionary.

    When ``audit_signing_key`` is configured, the hash is an HMAC-SHA256 keyed
    by that secret, so an actor without the key cannot forge a matching hash —
    rewriting the whole stored chain is detected on recompute. When the key is
    unset (development / non-production), it degrades to keyless SHA-256, which
    is tamper-evident only against actors who cannot rewrite the entire store.
    Production always has the key (enforced by the boot-time CRITICAL-secret
    gate), so production chains are uniformly keyed.

    Uses fast_canonical_dumps (compact separators, sort_keys, ensure_ascii=False)
    to match canonical_json_bytes() output, so the hash is stable across key
    orderings and serialization paths.

    Args:
        data: Dictionary to hash

    Returns:
        64-character hex digest (HMAC-SHA256 when keyed, SHA-256 when keyless)
    """
    payload = fast_canonical_dumps(data, default=str)
    key = _audit_signing_key()
    if key is not None:
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(data: dict[str, Any]) -> bytes:
    """
    Deterministic JSON serialization for Merkle hash computation.

    Delegates to fast_canonical_dumps for consistency across
    all hash/integrity call sites.

    Serialization rules:
        sort_keys=True: Deterministic key order
        default=str: Handle non-serializable types (datetime, etc.)
        separators=(",", ":"): Compact output without whitespace
        ensure_ascii=False: Preserve UTF-8 originals
    """
    return fast_canonical_dumps(data, default=str)


__all__ = ["IntegrityInfo", "compute_hash", "canonical_json_bytes"]
