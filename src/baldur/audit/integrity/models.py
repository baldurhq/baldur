"""
Integrity Models and Core Functions.

Contains:
- IntegrityInfo: Dataclass for integrity information
- compute_hash: keyed-or-keyless chain hash for dictionaries
- sanitize_integrity_annotations / record_source_reset: the shared shape of
  the extra keys a manager writes into an entry's ``integrity`` block
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from baldur.utils.serialization import fast_canonical_dumps

logger = structlog.get_logger()

# Keys the chain itself owns inside an entry's ``integrity`` block. An
# annotation may never carry one: ``current_hash`` in particular is assigned
# *after* the hash is computed, so an annotation under that key would be hashed
# in and then overwritten, and the entry would verify as tampered.
INTEGRITY_RESERVED_KEYS = frozenset(
    {"sequence", "previous_hash", "timestamp", "pod_id", "current_hash"}
)


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


def sanitize_integrity_annotations(
    annotations: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Drop the keys the chain owns, so an annotation cannot shadow one.

    Args:
        annotations: Caller-supplied extra keys for the ``integrity`` block.

    Returns:
        A plain dict safe to merge underneath the chain's own fields.
    """
    if not annotations:
        return {}
    return {
        key: value
        for key, value in annotations.items()
        if key not in INTEGRITY_RESERVED_KEYS
    }


def record_source_reset(
    *,
    manager: str,
    reason: str,
    observed: int | None,
    adopted: int,
    ledger_path: str,
) -> dict[str, Any]:
    """Announce that a chain re-anchored to its ledger, and stamp the entry.

    One call site per manager, so the log record, the counter and the stamp
    that rides under the entry's hash cannot describe the repair differently.

    Args:
        manager: ``"redis"`` or ``"local"`` — which source lost its state.
        reason: Why the re-anchor fired.
        observed: The sequence the source offered, or ``None`` when it minted
            nothing.
        adopted: The ledger tail sequence the chain re-anchored to.
        ledger_path: The file the tail was read from.

    Returns:
        The annotation to merge into the entry's ``integrity`` block.
    """
    logger.warning(
        "hash_chain.sequence_source_reset",
        manager=manager,
        reason=reason,
        observed=observed,
        adopted=adopted,
        ledger_path=ledger_path,
    )
    try:
        from baldur.metrics.audit_backend_metrics import (
            increment_audit_hash_chain_source_reset,
        )

        increment_audit_hash_chain_source_reset(manager=manager, reason=reason)
    except Exception as e:
        logger.debug("hash_chain.source_reset_metric_skipped", error=str(e))

    return {
        "source_reset": {
            "manager": manager,
            "reason": reason,
            "observed": observed,
            "adopted": adopted,
        }
    }


__all__ = [
    "INTEGRITY_RESERVED_KEYS",
    "IntegrityInfo",
    "canonical_json_bytes",
    "compute_hash",
    "record_source_reset",
    "sanitize_integrity_annotations",
]
