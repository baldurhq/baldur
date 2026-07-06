"""
decrypt_forensic() 레거시/HMAC 감지 및 Fernet 복호화 테스트.

sha256: 접두사, encrypted:hmac: 접두사, encrypted: Fernet 형식의
처리를 검증한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.masking import (
    MaskingLevel,
    decrypt_forensic,
    mask_with_level,
)


class TestSha256LegacyDetectionBehavior:
    """sha256: 접두사 레거시 해시 감지 동작 검증."""

    def test_sha256_prefix_raises_value_error(self):
        """sha256: 접두사 값은 복원 불가 에러 발생."""
        with pytest.raises(ValueError, match="SHA-256 hash"):
            decrypt_forensic("sha256:a1b2c3d4e5f6")

    def test_sha256_error_mentions_not_recoverable(self):
        """에러 메시지에 '복원 불가' 정보 포함."""
        with pytest.raises(ValueError, match="not recoverable"):
            decrypt_forensic("sha256:anything")


class TestHmacFallbackDetectionBehavior:
    """encrypted:hmac: 접두사 HMAC fallback 감지 동작 검증."""

    def test_hmac_prefix_raises_value_error(self):
        """encrypted:hmac: 접두사 값은 복원 불가 에러 발생."""
        with pytest.raises(ValueError, match="HMAC"):
            decrypt_forensic("encrypted:hmac:dGVzdA==")

    def test_hmac_error_mentions_not_recoverable(self):
        """에러 메시지에 '복원 불가' 정보 포함."""
        with pytest.raises(ValueError, match="not recoverable"):
            decrypt_forensic("encrypted:hmac:dGVzdA==")


class TestUnknownPrefixBehavior:
    """인식 불가 접두사 처리 동작 검증."""

    def test_unknown_prefix_raises_value_error(self):
        """encrypted: 로 시작하지 않는 값은 ValueError."""
        with pytest.raises(ValueError, match="must start with 'encrypted:'"):
            decrypt_forensic("unknown:something")

    def test_shows_received_prefix(self):
        """에러 메시지에 실제 수신한 접두사 표시."""
        with pytest.raises(ValueError, match="Got prefix:"):
            decrypt_forensic("random_value_without_prefix")

    def test_empty_string_raises_value_error(self):
        """빈 문자열은 ValueError."""
        with pytest.raises(ValueError):
            decrypt_forensic("")


class TestFernetDecryptionBehavior:
    """Fernet 정상 복호화 경로 동작 검증."""

    def test_no_fernet_key_raises_runtime_error(self):
        """encryption_key 미설정 시 RuntimeError."""
        with patch("baldur.audit.masking._get_forensic_fernet", return_value=None):
            with pytest.raises(RuntimeError, match="encryption_key"):
                decrypt_forensic("encrypted:validtoken")

    def test_roundtrip_encrypt_decrypt(self):
        """mask_with_level(FORENSIC) → decrypt_forensic 왕복 검증."""
        mock_fernet = MagicMock()
        mock_fernet.encrypt.return_value = b"fernet_token_data"
        mock_fernet.decrypt.return_value = b"original_value"

        with patch(
            "baldur.audit.masking._get_forensic_fernet",
            return_value=mock_fernet,
        ):
            encrypted = mask_with_level("original_value", MaskingLevel.FORENSIC)
            assert encrypted.startswith("encrypted:")

            decrypted = decrypt_forensic(encrypted)
            assert decrypted == "original_value"

    def test_invalid_fernet_token_raises_value_error(self):
        """유효하지 않은 Fernet 토큰은 ValueError(Decryption failed)."""
        mock_fernet = MagicMock()
        mock_fernet.decrypt.side_effect = Exception("InvalidToken")

        with patch(
            "baldur.audit.masking._get_forensic_fernet",
            return_value=mock_fernet,
        ):
            with pytest.raises(ValueError, match="Decryption failed"):
                decrypt_forensic("encrypted:corrupted_data")


class TestCryptographyOptionalDependencyBehavior:
    """cryptography optional dependency 동작 검증."""

    def test_forensic_masking_falls_back_to_hmac_without_fernet(self):
        """Fernet 미사용 시 HMAC fallback (encrypted:hmac:... 형식)."""
        with patch("baldur.audit.masking._get_forensic_fernet", return_value=None):
            result = mask_with_level("test", MaskingLevel.FORENSIC)
            assert result.startswith("encrypted:hmac:")

    def test_forensic_masking_uses_fernet_when_available(self):
        """Fernet 사용 가능 시 encrypted:... 형식 (hmac 아님)."""
        mock_fernet = MagicMock()
        mock_fernet.encrypt.return_value = b"fernet_encrypted_data"
        with patch(
            "baldur.audit.masking._get_forensic_fernet",
            return_value=mock_fernet,
        ):
            result = mask_with_level("test", MaskingLevel.FORENSIC)
            assert result.startswith("encrypted:")
            assert not result.startswith("encrypted:hmac:")
