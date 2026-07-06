"""
Role-based Masking 테스트.

MaskingLevel별 출력 검증, hash_for_audit 동일성 확인,
ActorContext 기반 레벨 결정 테스트.
"""

from unittest.mock import MagicMock, patch

from baldur.audit.masking import (
    MaskingLevel,
    get_masking_level_for_context,
    hash_for_audit,
    mask_with_level,
)


class TestMaskingLevel:
    """MaskingLevel Enum 테스트."""

    def test_masking_level_values(self):
        """MaskingLevel Enum 값 검증."""
        assert MaskingLevel.CLIENT.value == "client"
        assert MaskingLevel.AUDIT.value == "audit"
        assert MaskingLevel.FORENSIC.value == "forensic"

    def test_masking_level_count(self):
        """MaskingLevel Enum 개수 검증."""
        assert len(MaskingLevel) == 3


class TestMaskWithLevel:
    """mask_with_level 함수 테스트."""

    def test_client_level_redacts_completely(self):
        """CLIENT 레벨은 완전히 치환."""
        result = mask_with_level("admin@example.com", MaskingLevel.CLIENT)
        assert result == "***REDACTED***"
        assert "admin" not in result
        assert "example" not in result

    def test_audit_level_uses_hash(self):
        """AUDIT 레벨은 SHA-256 해시 사용."""
        result = mask_with_level("admin@example.com", MaskingLevel.AUDIT)
        assert result.startswith("sha256:")
        assert "admin" not in result

    def test_audit_level_same_input_same_hash(self):
        """AUDIT 레벨: 동일 입력은 동일 해시."""
        value = "test_value_123"
        result1 = mask_with_level(value, MaskingLevel.AUDIT)
        result2 = mask_with_level(value, MaskingLevel.AUDIT)
        assert result1 == result2

    def test_audit_level_different_input_different_hash(self):
        """AUDIT 레벨: 다른 입력은 다른 해시."""
        result1 = mask_with_level("value_a", MaskingLevel.AUDIT)
        result2 = mask_with_level("value_b", MaskingLevel.AUDIT)
        assert result1 != result2

    def test_forensic_level_uses_encryption_prefix(self):
        """FORENSIC 레벨은 encrypted: prefix 사용."""
        result = mask_with_level("admin@example.com", MaskingLevel.FORENSIC)
        assert result.startswith("encrypted:")
        assert "admin" not in result

    def test_forensic_level_with_salt(self):
        """FORENSIC 레벨: salt 사용."""
        result_no_salt = mask_with_level("value", MaskingLevel.FORENSIC)
        result_with_salt = mask_with_level(
            "value", MaskingLevel.FORENSIC, salt="secret"
        )
        assert result_no_salt != result_with_salt

    def test_empty_value_returns_empty(self):
        """빈 값 입력 시 빈 문자열 반환."""
        result = mask_with_level("", MaskingLevel.CLIENT)
        assert result == ""

    def test_none_value_handling(self):
        """None 값은 빈 문자열로 처리."""
        result = mask_with_level(None, MaskingLevel.CLIENT)  # type: ignore
        assert result == ""


class TestHashForAudit:
    """hash_for_audit 함수 테스트."""

    def test_hash_format(self):
        """해시 형식 검증."""
        result = hash_for_audit("test_value")
        assert result.startswith("sha256:")
        # 16자리로 잘림
        hash_part = result.replace("sha256:", "")
        assert len(hash_part) == 16

    def test_same_value_same_hash(self):
        """동일 값은 동일 해시 생성."""
        value = "user@example.com"
        hash1 = hash_for_audit(value)
        hash2 = hash_for_audit(value)
        assert hash1 == hash2

    def test_different_values_different_hash(self):
        """다른 값은 다른 해시 생성."""
        hash1 = hash_for_audit("user1@example.com")
        hash2 = hash_for_audit("user2@example.com")
        assert hash1 != hash2

    def test_salt_changes_hash(self):
        """salt 사용 시 다른 해시 생성."""
        value = "test_value"
        hash_no_salt = hash_for_audit(value)
        hash_with_salt = hash_for_audit(value, salt="my_secret")
        assert hash_no_salt != hash_with_salt

    def test_empty_value(self):
        """빈 값 처리."""
        result = hash_for_audit("")
        assert result == "sha256:empty"


class TestGetMaskingLevelForContext:
    """get_masking_level_for_context 함수 테스트."""

    def test_no_actor_returns_client(self):
        """Actor 미설정 시 CLIENT 레벨 반환."""
        with patch(
            "baldur.context.actor_context.ActorContext.get_current_or_none"
        ) as mock_method:
            mock_method.return_value = None
            result = get_masking_level_for_context()
            assert result == MaskingLevel.CLIENT

    def test_admin_role_returns_forensic(self):
        """baldur_admin은 FORENSIC 레벨 반환."""
        mock_actor = MagicMock()
        mock_actor.highest_role = "baldur_admin"

        with patch(
            "baldur.context.actor_context.ActorContext.get_current_or_none"
        ) as mock_method:
            mock_method.return_value = mock_actor
            result = get_masking_level_for_context()
            assert result == MaskingLevel.FORENSIC

    def test_operator_role_returns_audit(self):
        """baldur_operator는 AUDIT 레벨 반환."""
        mock_actor = MagicMock()
        mock_actor.highest_role = "baldur_operator"

        with patch(
            "baldur.context.actor_context.ActorContext.get_current_or_none"
        ) as mock_method:
            mock_method.return_value = mock_actor
            result = get_masking_level_for_context()
            assert result == MaskingLevel.AUDIT

    def test_viewer_role_returns_client(self):
        """baldur_viewer는 CLIENT 레벨 반환."""
        mock_actor = MagicMock()
        mock_actor.highest_role = "baldur_viewer"

        with patch(
            "baldur.context.actor_context.ActorContext.get_current_or_none"
        ) as mock_method:
            mock_method.return_value = mock_actor
            result = get_masking_level_for_context()
            assert result == MaskingLevel.CLIENT

    def test_unknown_role_returns_client(self):
        """알 수 없는 역할은 CLIENT 레벨 반환."""
        mock_actor = MagicMock()
        mock_actor.highest_role = "unknown_role"

        with patch(
            "baldur.context.actor_context.ActorContext.get_current_or_none"
        ) as mock_method:
            mock_method.return_value = mock_actor
            result = get_masking_level_for_context()
            assert result == MaskingLevel.CLIENT

    def test_exception_returns_client(self):
        """예외 발생 시 CLIENT 레벨 반환 (fail-safe)."""
        with patch(
            "baldur.context.actor_context.ActorContext.get_current_or_none"
        ) as mock_method:
            mock_method.side_effect = Exception("test error")
            result = get_masking_level_for_context()
            assert result == MaskingLevel.CLIENT


class TestMaskingPatternAnalysis:
    """마스킹 패턴 분석 시나리오 테스트."""

    def test_dos_pattern_detection_with_audit_hashes(self):
        """
        동일 사용자의 DoS 패턴 분석 가능 여부 검증.

        같은 에러 메시지가 반복되면 동일한 해시가 생성되어
        패턴 분석이 가능합니다.
        """
        # 같은 사용자가 5번 에러 발생
        user_email = "attacker@example.com"
        error_message = f"Validation error for {user_email}"

        # 각 요청에서 동일한 해시 생성
        hashes = [mask_with_level(error_message, MaskingLevel.AUDIT) for _ in range(5)]

        # 모든 해시가 동일 → 동일 사용자/패턴 식별 가능
        assert len(set(hashes)) == 1

        # 원본 정보는 노출되지 않음
        assert user_email not in hashes[0]

    def test_different_users_different_hashes(self):
        """다른 사용자는 다른 해시 생성."""
        hash1 = mask_with_level(
            "Validation error for user1@example.com", MaskingLevel.AUDIT
        )
        hash2 = mask_with_level(
            "Validation error for user2@example.com", MaskingLevel.AUDIT
        )

        # 다른 해시 → 다른 사용자로 식별
        assert hash1 != hash2
