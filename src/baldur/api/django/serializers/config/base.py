"""
Base Mixin for Configuration Serializers.

Provides apply strategy and safe fallback validation.

Fail-Safe Default 강화 추가.
"""

from rest_framework import serializers


class ApplyStrategyMixin(serializers.Serializer):
    """
    Mixin that adds apply strategy fields to config serializers.

    Safe Default 검증 및 폴백 기능 추가.
    """

    # 서브클래스에서 오버라이드하여 config_type 지정
    _config_type: str = ""

    apply_strategy = serializers.ChoiceField(
        required=False,
        choices=["immediate", "delayed", "graceful"],
        help_text="How to apply the changes: immediate (now), delayed (after N seconds), graceful (wait for in-progress ops)",
    )
    delay_seconds = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=3600,
        help_text="Seconds to wait before applying (only for 'delayed' strategy)",
    )
    grace_timeout_seconds = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=300,
        help_text="Max seconds to wait for in-progress operations (only for 'graceful' strategy)",
    )
    reason = serializers.CharField(
        required=False,
        max_length=500,
        allow_blank=True,
        help_text="Reason for the configuration change (optional, for audit trail)",
    )

    def get_apply_options(self) -> dict:
        """Extract apply strategy options from validated data."""
        return {
            "strategy": self.validated_data.get("apply_strategy"),
            "delay_seconds": self.validated_data.get("delay_seconds"),
            "grace_timeout_seconds": self.validated_data.get("grace_timeout_seconds"),
            "reason": self.validated_data.get("reason", ""),
        }

    def get_config_changes(self) -> dict:
        """Extract config changes (excluding apply strategy fields)."""
        exclude_fields = {
            "apply_strategy",
            "delay_seconds",
            "grace_timeout_seconds",
            "reason",
        }
        return {
            k: v
            for k, v in self.validated_data.items()
            if k not in exclude_fields and v is not None
        }

    def validate_with_safe_fallback(self, data: dict) -> dict:
        """
        Safe Default 검증 및 폴백 적용.

        잘못된 값은 Safe Default로 대체됩니다.
        서브클래스에서 _config_type을 설정해야 합니다.

        Args:
            data: 검증할 데이터

        Returns:
            Safe Default가 적용된 데이터
        """
        if not self._config_type:
            return data

        try:
            from baldur.core.safe_defaults import validate_with_safe_fallback

            return validate_with_safe_fallback(self._config_type, data)
        except ImportError:
            return data
