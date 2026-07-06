"""
AbstractPostmortemRecord abstract model.

이 모듈은 baldur.adapters.django.models 패키지의 내부 구현입니다.
"""

from __future__ import annotations

from typing import Any

try:
    from django.db import models
    from django.utils import timezone

    DJANGO_AVAILABLE = True
except ImportError:
    DJANGO_AVAILABLE = False
    models = None  # type: ignore
    timezone = None  # type: ignore


class AbstractPostmortemRecord(models.Model if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    장애 사후 분석(Post-mortem) 영속 저장소 추상 모델.

    서버 재시작 시 데이터 손실 방지를 위해 PostgreSQL에 영구 저장합니다.
    In-Memory 저장소의 한계(최대 100개, 다중 워커 불일치)를 해결합니다.

    Attributes:
        incident_id: 고유 인시던트 식별자
        started_at: 인시던트 시작 시각
        resolved_at: 인시던트 종료 시각
        duration_seconds: 장애 지속 시간(초)
        affected_services: 영향받은 서비스 목록
        timeline: 시간순 이벤트 기록
        auto_actions: 자동으로 수행된 복구 조치
        recommendations: 권장 사항 목록
        system_snapshot: 장애 시점 시스템 상태 스냅샷
        created_at: 레코드 생성 시각
        source: 생성 출처 (auto/manual)
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use AbstractPostmortemRecord. "
            "Install it with: pip install django"
        )

    class Source(models.TextChoices):
        """Post-mortem 생성 출처."""

        AUTO = "auto", "Automatic (System Generated)"
        MANUAL = "manual", "Manual (User Created)"

    # ========================================
    # Primary Identifier
    # ========================================
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        verbose_name="ID",
    )

    incident_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        verbose_name="Incident ID",
        help_text="Unique identifier for the incident",
    )

    # ========================================
    # Timing Information
    # ========================================
    started_at = models.DateTimeField(
        db_index=True,
        verbose_name="Incident Start Time",
        help_text="When the incident started",
    )

    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Incident Resolution Time",
        help_text="When the incident was resolved",
    )

    duration_seconds = models.FloatField(
        default=0.0,
        db_index=True,
        verbose_name="Duration (seconds)",
        help_text="Total duration of the incident in seconds",
    )

    # ========================================
    # Incident Details (JSON Fields)
    # ========================================
    affected_services = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Affected Services",
        help_text="List of services impacted by the incident",
    )

    timeline = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Timeline",
        help_text="Chronological list of events during the incident",
    )

    auto_actions = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Automatic Actions",
        help_text="List of automatic recovery actions performed",
    )

    recommendations = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Recommendations",
        help_text="Suggested actions for future prevention",
    )

    system_snapshot = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="System Snapshot",
        help_text="System state snapshot at the time of incident",
    )

    # ========================================
    # Metadata
    # ========================================
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="Record Created At",
    )

    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.AUTO,
        db_index=True,
        verbose_name="Source",
        help_text="How this post-mortem was created (auto/manual)",
    )

    class Meta:
        abstract = True
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["-started_at", "-duration_seconds"]),
            models.Index(fields=["source", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Postmortem {self.incident_id} ({self.started_at})"

    def to_dict(self) -> dict[str, Any]:
        """레코드를 딕셔너리로 변환 (API 응답용)."""
        return {
            "id": str(self.id),
            "incident_id": self.incident_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "duration_seconds": self.duration_seconds,
            "affected_services": self.affected_services,
            "timeline": self.timeline,
            "auto_actions": self.auto_actions,
            "recommendations": self.recommendations,
            "system_snapshot": self.system_snapshot,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "source": self.source,
        }
