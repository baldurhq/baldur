"""Unit tests for hashlib.new() fallback in compute_checksum (308-G)."""

import hashlib

import pytest

from baldur.audit.checksum import compute_checksum, verify_checksum


class TestComputeChecksumFallbackBehavior:
    """compute_checksum() hashlib.new() fallback behavior verification."""

    def test_crc32_algorithm_uses_crc32_path(self):
        """algorithm='crc32' returns 8-char hex string."""
        result = compute_checksum(b"hello", algorithm="crc32")
        assert isinstance(result, str)
        assert len(result) == 8

    def test_sha256_algorithm_uses_sha256_path(self):
        """algorithm='sha256' returns 64-char hex string."""
        result = compute_checksum(b"hello", algorithm="sha256")
        assert isinstance(result, str)
        assert len(result) == 64

    def test_md5_algorithm_rejected_by_allowlist(self):
        """algorithm='md5' is not in the allowlist and raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported algorithm"):
            compute_checksum(b"hello", algorithm="md5")

    def test_sha1_algorithm_rejected_by_allowlist(self):
        """algorithm='sha1' is not in the allowlist and raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported algorithm"):
            compute_checksum(b"hello", algorithm="sha1")

    def test_sha512_algorithm_uses_hashlib_new(self):
        """algorithm='sha512' is allowed and uses hashlib.new()."""
        result = compute_checksum(b"data", algorithm="sha512")
        expected = hashlib.new("sha512", b"data").hexdigest()
        assert result == expected

    def test_blake2b_algorithm_uses_hashlib_new(self):
        """algorithm='blake2b' is allowed and uses hashlib.new()."""
        result = compute_checksum(b"data", algorithm="blake2b")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_unsupported_algorithm_raises_value_error(self):
        """Unsupported algorithm raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported algorithm"):
            compute_checksum(b"data", algorithm="nonexistent_algo")

    def test_fallback_normalizes_dict_input(self):
        """Allowed algorithm handles dict input via _normalize_to_bytes."""
        data = {"key": "value"}
        result = compute_checksum(data, algorithm="sha512")
        assert isinstance(result, str)
        assert len(result) == 128  # SHA512 = 128 hex chars

    def test_fallback_normalizes_string_input(self):
        """Allowed algorithm handles string input."""
        result = compute_checksum("hello", algorithm="sha512")
        expected = hashlib.new("sha512", b"hello").hexdigest()
        assert result == expected

    def test_crc32_and_sha256_consistency_with_dedicated_functions(self):
        """compute_checksum dispatches to dedicated functions for crc32/sha256."""
        from baldur.audit.checksum import compute_crc32, compute_sha256

        data = b"test data"
        assert compute_checksum(data, algorithm="crc32") == compute_crc32(data)
        assert compute_checksum(data, algorithm="sha256") == compute_sha256(data)


class TestVerifyChecksumEdgeCaseBehavior:
    """verify_checksum() edge case behavior verification."""

    def test_verify_unsupported_algorithm_raises_value_error(self):
        """verify_checksum with unsupported algorithm raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported algorithm"):
            verify_checksum(b"data", "abcd", algorithm="md5")

    def test_verify_crc32_delegates_correctly(self):
        """verify_checksum with crc32 delegates to verify_crc32."""
        from baldur.audit.checksum import compute_crc32

        data = b"test"
        checksum = compute_crc32(data)
        result = verify_checksum(data, checksum, algorithm="crc32")
        assert result.is_valid is True
        assert result.algorithm == "crc32"

    def test_verify_sha256_delegates_correctly(self):
        """verify_checksum with sha256 delegates to verify_sha256."""
        from baldur.audit.checksum import compute_sha256

        data = b"test"
        checksum = compute_sha256(data)
        result = verify_checksum(data, checksum, algorithm="sha256")
        assert result.is_valid is True
        assert result.algorithm == "sha256"
