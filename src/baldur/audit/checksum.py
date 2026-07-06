"""
Checksum Utilities for Data Integrity.

CRC32 및 SHA256 체크섬 계산/검증 유틸리티.
WAL, 캐시, 감사 기록 등에서 사용.

최소 의존성: 표준 라이브러리만 사용 (zlib, hashlib, json)

Usage:
    from baldur.audit.checksum import (
        compute_crc32,
        compute_sha256,
        verify_crc32,
        verify_sha256,
    )

    # CRC32 (빠름, WAL용)
    checksum = compute_crc32(data)
    is_valid = verify_crc32(data, checksum)

    # SHA256 (보안, 해시 체인용)
    checksum = compute_sha256(data)
    is_valid = verify_sha256(data, checksum)
"""

import hashlib
import zlib
from dataclasses import dataclass
from typing import Any


@dataclass
class ChecksumResult:
    """체크섬 검증 결과."""

    is_valid: bool
    expected: str
    computed: str
    algorithm: str


def compute_crc32(data: bytes | str | dict | Any) -> str:
    """
    CRC32 체크섬 계산.

    빠른 체크섬으로 WAL, 캐시 무결성 검증에 적합.

    Args:
        data: 체크섬 대상 데이터 (bytes, str, dict, or any JSON-serializable)

    Returns:
        8자리 16진수 문자열 (예: "a1b2c3d4")
    """
    data_bytes = _normalize_to_bytes(data)
    crc = zlib.crc32(data_bytes) & 0xFFFFFFFF
    return f"{crc:08x}"


def verify_crc32(data: bytes | str | dict | Any, expected: str) -> ChecksumResult:
    """
    CRC32 체크섬 검증.

    Args:
        data: 검증할 데이터
        expected: 예상 체크섬

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
    SHA256 체크섬 계산.

    보안 해시로 해시 체인, 감사 로그 무결성에 적합.

    Args:
        data: 체크섬 대상 데이터
        truncate: 결과 자릿수 (None이면 전체 64자)

    Returns:
        16진수 문자열 (기본 64자, truncate 시 해당 자릿수)
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
    SHA256 체크섬 검증.

    Args:
        data: 검증할 데이터
        expected: 예상 체크섬
        truncate: 자릿수 (expected와 동일하게 설정)

    Returns:
        ChecksumResult with validation result
    """
    # truncate 자동 추론
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
    범용 체크섬 계산.

    Args:
        data: 체크섬 대상 데이터
        algorithm: 알고리즘 ("crc32" or "sha256")
        truncate: SHA256일 때 자릿수

    Returns:
        체크섬 문자열
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
    범용 체크섬 검증.

    Args:
        data: 검증할 데이터
        expected: 예상 체크섬
        algorithm: 알고리즘 ("crc32" or "sha256")

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
    다양한 타입의 데이터를 bytes로 정규화.

    Args:
        data: 변환할 데이터

    Returns:
        bytes 형태의 데이터
    """
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (dict, list)):
        from baldur.utils.serialization import fast_canonical_dumps

        return fast_canonical_dumps(data)
    # 기타 타입은 문자열로 변환
    return str(data).encode("utf-8")


# =============================================================================
# Convenience functions for common use cases
# =============================================================================


def checksum_dict(data: dict, algorithm: str = "sha256", truncate: int = 16) -> str:
    """
    딕셔너리 체크섬 (캐시, 감사 기록용).

    Args:
        data: 딕셔너리 데이터
        algorithm: 알고리즘
        truncate: 자릿수

    Returns:
        체크섬 문자열
    """
    return compute_checksum(data, algorithm, truncate)


def checksum_file(filepath: str, algorithm: str = "sha256") -> str:
    """
    파일 체크섬 계산.

    Args:
        filepath: 파일 경로
        algorithm: 알고리즘

    Returns:
        체크섬 문자열
    """
    with open(filepath, "rb") as f:
        content = f.read()
    return compute_checksum(content, algorithm)


def verify_file_checksum(
    filepath: str, expected: str, algorithm: str = "sha256"
) -> ChecksumResult:
    """
    파일 체크섬 검증.

    Args:
        filepath: 파일 경로
        expected: 예상 체크섬
        algorithm: 알고리즘

    Returns:
        ChecksumResult
    """
    with open(filepath, "rb") as f:
        content = f.read()
    return verify_checksum(content, expected, algorithm)
