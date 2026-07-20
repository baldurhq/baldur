"""
Checksum Utilities for Data Integrity.

CRC32 and SHA256 checksum compute/verify utilities.
Used by WAL, cache, audit records, etc.

Minimal dependencies: standard library only (zlib, hashlib, json)

Usage:
    from baldur.audit.checksum import (
        compute_crc32,
        compute_sha256,
        verify_crc32,
        verify_sha256,
    )

    # CRC32 (fast, for WAL)
    checksum = compute_crc32(data)
    is_valid = verify_crc32(data, checksum)

    # SHA256 (secure, for hash chains)
    checksum = compute_sha256(data)
    is_valid = verify_sha256(data, checksum)
"""

import hashlib
import zlib
from dataclasses import dataclass
from typing import Any


@dataclass
class ChecksumResult:
    """Checksum verification result."""

    is_valid: bool
    expected: str
    computed: str
    algorithm: str


def compute_crc32(data: bytes | str | dict | Any) -> str:
    """
    Compute a CRC32 checksum.

    A fast checksum, suited to WAL and cache integrity verification.

    Args:
        data: Data to checksum (bytes, str, dict, or any JSON-serializable)

    Returns:
        8-digit hex string (e.g. "a1b2c3d4")
    """
    data_bytes = _normalize_to_bytes(data)
    crc = zlib.crc32(data_bytes) & 0xFFFFFFFF
    return f"{crc:08x}"


def verify_crc32(data: bytes | str | dict | Any, expected: str) -> ChecksumResult:
    """
    Verify a CRC32 checksum.

    Args:
        data: Data to verify
        expected: Expected checksum

    Returns:
        ChecksumResult with validation result
    """
    computed = compute_crc32(data)
    return ChecksumResult(
        is_valid=computed.lower() == expected.lower(),
        expected=expected.lower(),
        computed=computed,
        algorithm="crc32",
    )


def compute_sha256(
    data: bytes | str | dict | Any,
    truncate: int | None = None,
) -> str:
    """
    Compute a SHA256 checksum.

    A secure hash, suited to hash chains and audit log integrity.

    Args:
        data: Data to checksum
        truncate: Result length in digits (full 64 chars when None)

    Returns:
        Hex string (64 chars by default, or the truncated length)
    """
    data_bytes = _normalize_to_bytes(data)
    full_hash = hashlib.sha256(data_bytes).hexdigest()

    if truncate is not None and truncate > 0:
        return full_hash[:truncate]
    return full_hash


def verify_sha256(
    data: bytes | str | dict | Any,
    expected: str,
    truncate: int | None = None,
) -> ChecksumResult:
    """
    Verify a SHA256 checksum.

    Args:
        data: Data to verify
        expected: Expected checksum
        truncate: Length in digits (set to match expected)

    Returns:
        ChecksumResult with validation result
    """
    # Infer truncate automatically
    if truncate is None and len(expected) < 64:
        truncate = len(expected)

    computed = compute_sha256(data, truncate)
    return ChecksumResult(
        is_valid=computed.lower() == expected.lower(),
        expected=expected.lower(),
        computed=computed,
        algorithm="sha256",
    )


def compute_checksum(
    data: bytes | str | dict | Any,
    algorithm: str = "crc32",
    truncate: int | None = None,
) -> str:
    """
    Generic checksum computation.

    Args:
        data: Data to checksum
        algorithm: Algorithm ("crc32" or "sha256")
        truncate: Length in digits when using SHA256

    Returns:
        Checksum string
    """
    if algorithm == "sha256":
        return compute_sha256(data, truncate)
    if algorithm == "crc32":
        return compute_crc32(data)
    _ALLOWED_ALGORITHMS = {
        "sha384",
        "sha512",
        "sha3_256",
        "sha3_512",
        "blake2b",
        "blake2s",
    }
    if algorithm not in _ALLOWED_ALGORITHMS:
        raise ValueError(
            f"Unsupported algorithm: {algorithm}. "
            f"Allowed: {', '.join(sorted(_ALLOWED_ALGORITHMS))}"
        )
    normalized = _normalize_to_bytes(data)
    return hashlib.new(algorithm, normalized).hexdigest()


def verify_checksum(
    data: bytes | str | dict | Any,
    expected: str,
    algorithm: str = "crc32",
) -> ChecksumResult:
    """
    Generic checksum verification.

    Args:
        data: Data to verify
        expected: Expected checksum
        algorithm: Algorithm ("crc32" or "sha256")

    Returns:
        ChecksumResult with validation result
    """
    if algorithm == "sha256":
        return verify_sha256(data, expected)
    if algorithm == "crc32":
        return verify_crc32(data, expected)
    raise ValueError(f"Unsupported algorithm: {algorithm}")


def _normalize_to_bytes(data: bytes | str | dict | Any) -> bytes:
    """
    Normalize data of various types to bytes.

    Args:
        data: Data to convert

    Returns:
        The data as bytes
    """
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (dict, list)):
        from baldur.utils.serialization import fast_canonical_dumps

        return fast_canonical_dumps(data)
    # Any other type is converted to a string
    return str(data).encode("utf-8")


# =============================================================================
# Convenience functions for common use cases
# =============================================================================


def checksum_dict(data: dict, algorithm: str = "sha256", truncate: int = 16) -> str:
    """
    Dictionary checksum (for cache and audit records).

    Args:
        data: Dictionary data
        algorithm: Algorithm
        truncate: Length in digits

    Returns:
        Checksum string
    """
    return compute_checksum(data, algorithm, truncate)


def checksum_file(filepath: str, algorithm: str = "sha256") -> str:
    """
    Compute a file checksum.

    Args:
        filepath: File path
        algorithm: Algorithm

    Returns:
        Checksum string
    """
    with open(filepath, "rb") as f:
        content = f.read()
    return compute_checksum(content, algorithm)


def verify_file_checksum(
    filepath: str, expected: str, algorithm: str = "sha256"
) -> ChecksumResult:
    """
    Verify a file checksum.

    Args:
        filepath: File path
        expected: Expected checksum
        algorithm: Algorithm

    Returns:
        ChecksumResult
    """
    with open(filepath, "rb") as f:
        content = f.read()
    return verify_checksum(content, expected, algorithm)
