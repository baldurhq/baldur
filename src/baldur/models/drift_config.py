"""
Drift Threshold Configuration Model.

Provides dynamic configuration for metric drift thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now


@dataclass
class DriftThresholdConfig(SerializableMixin):
    """
    Drift 임계값 설정.

    운영자가 동적으로 조정할 수 있으며,
    변경 시 Audit 로그가 기록됩니다.

    Thresholds (임계값):
        - warning: 5% - 경고, 로그만 기록
        - critical: 20% - 심각, 알림 발송
        - incident: 50% - 인시던트, 이벤트 유실 의심

    Example:
        >>> config = DriftThresholdConfig()
        >>> print(f"Warning at: {config.warning_threshold * 100}%")
        Warning at: 5.0%
        >>>
        >>> # 사용자 정의 임계값
        >>> config = DriftThresholdConfig(
        ...     warning_threshold=0.10,
        ...     critical_threshold=0.30,
        ... )
    """

    # 임계값 (0.0 ~ 1.0)
    warning_threshold: float = 0.05  # 5%
    critical_threshold: float = 0.20  # 20%
    incident_threshold: float = 0.50  # 50%

    # 알림 설정
    alert_enabled: bool = True
    incident_auto_create: bool = True

    # 메타데이터
    updated_at: str | None = None
    updated_by: str | None = None

    def __post_init__(self) -> None:
        """생성 후 유효성 검사 수행."""
        self._validate()

    def _validate(self) -> None:
        """임계값 유효성 검사."""
        if not (
            0
            < self.warning_threshold
            < self.critical_threshold
            < self.incident_threshold
            <= 1.0
        ):
            raise ValueError(
                "Thresholds must be: 0 < warning < critical < incident <= 1.0. "
                f"Got: warning={self.warning_threshold}, critical={self.critical_threshold}, "
                f"incident={self.incident_threshold}"
            )

    @classmethod
    def from_env(cls) -> DriftThresholdConfig:
        """환경 변수에서 생성 (BaseSettings 위임).

        환경변수 파싱을 DriftThresholdSettings(BaseSettings)에 위임하여
        os.environ.get() 수동 파싱 제거 (202 패러다임 통일).
        """
        from baldur.settings.drift_threshold import DriftThresholdSettings

        settings = DriftThresholdSettings()
        return cls(
            warning_threshold=settings.warning_threshold,
            critical_threshold=settings.critical_threshold,
            incident_threshold=settings.incident_threshold,
            alert_enabled=settings.alert_enabled,
            incident_auto_create=settings.incident_auto_create,
        )

    def update(
        self,
        actor_id: str | None = None,
        **kwargs: Any,
    ) -> DriftThresholdConfig:
        """
        새로운 값으로 업데이트된 설정을 반환합니다.

        Args:
            actor_id: 업데이트를 수행한 사용자 ID
            **kwargs: 업데이트할 필드들

        Returns:
            업데이트된 새 DriftThresholdConfig 인스턴스
        """
        current = self.to_dict()
        current.update(kwargs)
        current["updated_at"] = utc_now().isoformat()
        current["updated_by"] = actor_id
        return self.from_dict(current)

    def get_threshold_percent_display(self) -> dict[str, str]:
        """임계값을 퍼센트 문자열로 반환."""
        return {
            "warning": f"{self.warning_threshold * 100:.1f}%",
            "critical": f"{self.critical_threshold * 100:.1f}%",
            "incident": f"{self.incident_threshold * 100:.1f}%",
        }


__all__ = ["DriftThresholdConfig"]
