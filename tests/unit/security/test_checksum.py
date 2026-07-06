"""
Checksum 유틸리티 단위 테스트.

Tests:
- CRC32 체크섬
- SHA256 체크섬
- 다양한 입력 타입
- 검증 기능
"""

import tempfile
from pathlib import Path

import pytest

from baldur.audit.checksum import (
    ChecksumResult,
    checksum_dict,
    checksum_file,
    compute_checksum,
    compute_crc32,
    compute_sha256,
    verify_checksum,
    verify_crc32,
    verify_file_checksum,
    verify_sha256,
)


class TestComputeCRC32Behavior:
    """CRC32 체크섬 계산 동작 검증."""

    def test_bytes_input(self):
        """bytes 입력."""
        result = compute_crc32(b"hello")
        assert isinstance(result, str)
        assert len(result) == 8  # 8자리 hex

    def test_string_input(self):
        """문자열 입력."""
        result = compute_crc32("hello")
        assert len(result) == 8

    def test_dict_input(self):
        """딕셔너리 입력."""
        result = compute_crc32({"key": "value"})
        assert len(result) == 8

    def test_consistent_results(self):
        """동일 입력에 동일 결과."""
        data = {"a": 1, "b": 2}
        result1 = compute_crc32(data)
        result2 = compute_crc32(data)
        assert result1 == result2

    def test_dict_key_order_independent(self):
        """딕셔너리 키 순서 무관."""
        data1 = {"a": 1, "b": 2}
        data2 = {"b": 2, "a": 1}
        assert compute_crc32(data1) == compute_crc32(data2)

    def test_different_data_different_checksum(self):
        """다른 데이터는 다른 체크섬."""
        result1 = compute_crc32("hello")
        result2 = compute_crc32("world")
        assert result1 != result2


class TestVerifyCRC32Behavior:
    """CRC32 검증 동작 검증."""

    def test_verify_valid(self):
        """유효한 체크섬 검증."""
        data = "test data"
        checksum = compute_crc32(data)

        result = verify_crc32(data, checksum)
        assert result.is_valid
        assert result.expected == checksum.lower()
        assert result.computed == checksum
        assert result.algorithm == "crc32"

    def test_verify_invalid(self):
        """유효하지 않은 체크섬 검증."""
        result = verify_crc32("test", "00000000")
        assert not result.is_valid

    def test_verify_case_insensitive(self):
        """대소문자 구분 없이 검증."""
        data = "test"
        checksum = compute_crc32(data)

        result_upper = verify_crc32(data, checksum.upper())
        result_lower = verify_crc32(data, checksum.lower())

        assert result_upper.is_valid
        assert result_lower.is_valid


class TestComputeSHA256Behavior:
    """SHA256 체크섬 계산 동작 검증."""

    def test_full_length(self):
        """전체 길이 (64자)."""
        result = compute_sha256("hello")
        assert len(result) == 64

    def test_truncated(self):
        """잘린 체크섬."""
        result = compute_sha256("hello", truncate=16)
        assert len(result) == 16

    def test_bytes_input(self):
        """bytes 입력."""
        result = compute_sha256(b"hello")
        assert len(result) == 64

    def test_dict_input(self):
        """딕셔너리 입력."""
        result = compute_sha256({"key": "value"})
        assert len(result) == 64

    def test_consistent_results(self):
        """동일 입력에 동일 결과."""
        data = {"a": 1, "b": 2}
        result1 = compute_sha256(data)
        result2 = compute_sha256(data)
        assert result1 == result2


class TestVerifySHA256Behavior:
    """SHA256 검증 동작 검증."""

    def test_verify_valid_full(self):
        """전체 길이 검증."""
        data = "test data"
        checksum = compute_sha256(data)

        result = verify_sha256(data, checksum)
        assert result.is_valid

    def test_verify_valid_truncated(self):
        """잘린 길이 검증."""
        data = "test data"
        checksum = compute_sha256(data, truncate=16)

        result = verify_sha256(data, checksum, truncate=16)
        assert result.is_valid

    def test_verify_auto_truncate(self):
        """자동 truncate 추론."""
        data = "test data"
        checksum = compute_sha256(data, truncate=16)

        # truncate 없이도 길이로 추론
        result = verify_sha256(data, checksum)
        assert result.is_valid

    def test_verify_invalid(self):
        """유효하지 않은 체크섬."""
        result = verify_sha256("test", "0" * 64)
        assert not result.is_valid


class TestGenericChecksumBehavior:
    """범용 체크섬 함수 동작 검증."""

    def test_compute_crc32(self):
        """compute_checksum with crc32."""
        result = compute_checksum("test", algorithm="crc32")
        assert len(result) == 8

    def test_compute_sha256(self):
        """compute_checksum with sha256."""
        result = compute_checksum("test", algorithm="sha256")
        assert len(result) == 64

    def test_compute_sha256_truncated(self):
        """compute_checksum with sha256 truncated."""
        result = compute_checksum("test", algorithm="sha256", truncate=16)
        assert len(result) == 16

    def test_invalid_algorithm(self):
        """잘못된 알고리즘 (hashlib에도 없는 경우)."""
        with pytest.raises(ValueError):
            compute_checksum("test", algorithm="nonexistent_algo")

    def test_md5_algorithm_rejected_by_allowlist(self):
        """md5 알고리즘은 allowlist에 없어 ValueError 발생."""
        with pytest.raises(ValueError, match="Unsupported algorithm"):
            compute_checksum("test", algorithm="md5")

    def test_verify_crc32(self):
        """verify_checksum with crc32."""
        checksum = compute_checksum("test", algorithm="crc32")
        result = verify_checksum("test", checksum, algorithm="crc32")
        assert result.is_valid

    def test_verify_sha256(self):
        """verify_checksum with sha256."""
        checksum = compute_checksum("test", algorithm="sha256")
        result = verify_checksum("test", checksum, algorithm="sha256")
        assert result.is_valid


class TestChecksumDictBehavior:
    """checksum_dict 동작 검증."""

    def test_default_sha256_16(self):
        """기본 설정: SHA256, 16자."""
        result = checksum_dict({"key": "value"})
        assert len(result) == 16

    def test_custom_algorithm(self):
        """사용자 정의 알고리즘."""
        result = checksum_dict({"key": "value"}, algorithm="crc32")
        assert len(result) == 8

    def test_nested_dict(self):
        """중첩 딕셔너리."""
        data = {"level1": {"level2": {"value": 123}}}
        result = checksum_dict(data)
        assert len(result) == 16


class TestChecksumFileBehavior:
    """파일 체크섬 동작 검증."""

    def test_file_checksum(self):
        """파일 체크섬 계산."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("test content")
            filepath = f.name

        try:
            result = checksum_file(filepath)
            assert len(result) == 64
        finally:
            Path(filepath).unlink()

    def test_file_checksum_crc32(self):
        """파일 CRC32 체크섬."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("test content")
            filepath = f.name

        try:
            result = checksum_file(filepath, algorithm="crc32")
            assert len(result) == 8
        finally:
            Path(filepath).unlink()

    def test_verify_file_checksum(self):
        """파일 체크섬 검증."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("test content")
            filepath = f.name

        try:
            checksum = checksum_file(filepath)
            result = verify_file_checksum(filepath, checksum)
            assert result.is_valid
        finally:
            Path(filepath).unlink()

    def test_verify_file_checksum_invalid(self):
        """파일 체크섬 검증 실패."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("test content")
            filepath = f.name

        try:
            result = verify_file_checksum(filepath, "0" * 64)
            assert not result.is_valid
        finally:
            Path(filepath).unlink()


class TestChecksumResultContract:
    """ChecksumResult dataclass 구조 계약 검증."""

    def test_dataclass_fields(self):
        """dataclass 필드 확인."""
        result = ChecksumResult(
            is_valid=True,
            expected="abc123",
            computed="abc123",
            algorithm="crc32",
        )
        assert result.is_valid
        assert result.expected == "abc123"
        assert result.computed == "abc123"
        assert result.algorithm == "crc32"


class TestChecksumEdgeCaseBehavior:
    """체크섬 엣지 케이스 동작 검증."""

    def test_empty_string(self):
        """빈 문자열."""
        result = compute_crc32("")
        assert len(result) == 8

    def test_empty_bytes(self):
        """빈 bytes."""
        result = compute_crc32(b"")
        assert len(result) == 8

    def test_empty_dict(self):
        """빈 딕셔너리."""
        result = compute_crc32({})
        assert len(result) == 8

    def test_unicode_string(self):
        """유니코드 문자열."""
        result = compute_crc32("한글 테스트 🎉")
        assert len(result) == 8

    def test_large_data(self):
        """큰 데이터."""
        data = {"key": "x" * 100000}
        result = compute_crc32(data)
        assert len(result) == 8

    def test_list_input(self):
        """리스트 입력."""
        result = compute_crc32([1, 2, 3, "test"])
        assert len(result) == 8

    def test_int_input(self):
        """정수 입력."""
        result = compute_crc32(12345)
        assert len(result) == 8

    def test_float_input(self):
        """실수 입력."""
        result = compute_crc32(3.14159)
        assert len(result) == 8

    def test_none_in_dict(self):
        """딕셔너리 내 None."""
        result = compute_crc32({"key": None})
        assert len(result) == 8
