"""
Cascade Event Archive Django Model.

PostgreSQL 영속성 저장소용 Django Abstract 모델.

용도:
- Redis Hot Tier에서 Warm Tier로 이관된 Cascade Event 저장
- 인과관계(causation_chain) 영속 보관
- 감사 추적 및 법적 준수

사용법:
    # Django 프로젝트의 models.py에서:
    from baldur.models import AbstractCascadeEventArchive

    class CascadeEventArchive(AbstractCascadeEventArchive):
        class Meta(AbstractCascadeEventArchive.Meta):
            abstract = False
            db_table = "baldur_cascade_events"

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
    adapters/django/models.py (AbstractFailedOperation 패턴)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    from django.db import models
    from django.utils import timezone

    DJANGO_AVAILABLE = True
except ImportError:
    DJANGO_AVAILABLE = False
    models = None  # type: ignore
    timezone = None  # type: ignore


if TYPE_CHECKING:
    pass


class AbstractCascadeEventArchive(models.Model if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    Cascade Event Archive를 위한 Abstract Django 모델.

    특징:
    - 인과관계 체인(causation_chain) 저장
    - Hash Chain 무결성 정보 보관
    - 월별 파티셔닝 지원 (PostgreSQL)
    - JSONB 인덱스 지원

    스키마 설계 근거:
    - cascade_id: Cascade Event 고유 ID
    - causation_chain: 인과관계 이벤트 ID 목록 (JSONB)
    - effects: 연쇄 효과 목록 (JSONB)
    - current_hash, previous_hash: Hash Chain 무결성
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use AbstractCascadeEventArchive. "
            "Install it with: pip install django"
        )

    # ========================================
    # Trigger Type Choices
    # ========================================
    class TriggerType(models.TextChoices):
        """Cascade 트리거 유형."""

        EMERGENCY_LEVEL_CHANGED = "EMERGENCY_LEVEL_CHANGED", "Emergency Level Changed"
        MANUAL_INTERVENTION = "MANUAL_INTERVENTION", "Manual Intervention"
        MANUAL_ACTIVATION = "MANUAL_ACTIVATION", "Manual Activation"
        CANARY_ROLLBACK = "CANARY_ROLLBACK", "Canary Rollback"
        CIRCUIT_BREAKER_OPENED = "CIRCUIT_BREAKER_OPENED", "Circuit Breaker Opened"
        GOVERNANCE_MODE_CHANGED = "GOVERNANCE_MODE_CHANGED", "Governance Mode Changed"
        ERROR_BUDGET_EXHAUSTED = "ERROR_BUDGET_EXHAUSTED", "Error Budget Exhausted"
        RECOVERY_STARTED = "RECOVERY_STARTED", "Recovery Started"
        DEESCALATION = "DEESCALATION", "De-escalation"

    # ========================================
    # Primary Key & Identifiers
    # ========================================
    cascade_id = models.CharField(
        max_length=100,
        primary_key=True,
        verbose_name="Cascade ID",
        help_text="Unique Cascade Event ID (e.g., cascade-evt-abc123)",
    )

    namespace = models.CharField(
        max_length=100,
        db_index=True,
        verbose_name="Namespace",
        help_text="Namespace where the event occurred (e.g., seoul, global)",
    )

    # ========================================
    # Trigger Information
    # ========================================
    trigger_type = models.CharField(
        max_length=50,
        choices=TriggerType.choices,
        db_index=True,
        verbose_name="Trigger Type",
        help_text="Trigger type that initiated the cascade",
    )

    trigger_details = models.JSONField(
        default=dict,
        verbose_name="Trigger Details",
        help_text="Trigger details (old_level, new_level, etc.)",
    )

    # ========================================
    # Effects & Causation Chain
    # ========================================
    effects = models.JSONField(
        default=list,
        verbose_name="Effects",
        help_text="List of cascading effects (each includes action_type, success, caused_by)",
    )

    causation_chain = models.JSONField(
        default=list,
        verbose_name="Causation Chain",
        help_text="Causation event ID chain [trigger_id, effect_1_id, effect_2_id, ...]",
    )

    # ========================================
    # Hash Chain (Integrity)
    # ========================================
    previous_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="Previous Hash",
        help_text="Hash of the previous Cascade Event (chain link)",
    )

    current_hash = models.CharField(
        max_length=64,
        db_index=True,
        verbose_name="Current Hash",
        help_text="SHA-256 hash of the current Cascade Event",
    )

    # ========================================
    # Statistics
    # ========================================
    total_effects = models.PositiveIntegerField(
        default=0,
        verbose_name="Total Effects",
        help_text="Total number of effects",
    )

    success_count = models.PositiveIntegerField(
        default=0,
        verbose_name="Success Count",
        help_text="Number of successful effects",
    )

    failure_count = models.PositiveIntegerField(
        default=0,
        verbose_name="Failure Count",
        help_text="Number of failed effects",
    )

    # ========================================
    # Timestamps
    # ========================================
    timestamp = models.DateTimeField(
        db_index=True,
        verbose_name="Event Timestamp",
        help_text="Cascade Event occurrence time",
    )

    archived_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Archived At",
        help_text="Time when archived from Redis to PostgreSQL",
    )

    # ========================================
    # External Trace Context (Optional)
    # ========================================
    external_trace = models.JSONField(
        null=True,
        blank=True,
        default=None,
        verbose_name="External Trace Context",
        help_text="W3C Trace Context / OpenTelemetry integration info",
    )

    # ========================================
    # Version
    # ========================================
    version = models.CharField(
        max_length=10,
        default="1.0",
        verbose_name="Schema Version",
        help_text="Data schema version",
    )

    # ========================================
    # Test Mode Flag
    # ========================================
    is_test = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Is Test Event",
        help_text="Whether this is a test environment event (True when created with X-Test-Mode)",
    )

    class Meta:
        abstract = True
        ordering = ["-timestamp"]
        verbose_name = "Cascade Event Archive"
        verbose_name_plural = "Cascade Event Archives"
        indexes = [
            # 네임스페이스 + 시간 복합 인덱스 (가장 빈번한 쿼리)
            models.Index(
                fields=["namespace", "-timestamp"],
                name="idx_cascade_ns_ts",
            ),
            # 트리거 타입 인덱스
            models.Index(
                fields=["trigger_type"],
                name="idx_cascade_trigger",
            ),
            # 해시 체인 검증용
            models.Index(
                fields=["current_hash"],
                name="idx_cascade_hash",
            ),
            # 테스트 데이터 시간순 조회용
            models.Index(
                fields=["is_test", "-timestamp"],
                name="idx_cascade_test_ts",
            ),
        ]

    def __str__(self) -> str:
        return f"CascadeEvent({self.cascade_id}, {self.trigger_type})"

    def get_causation_chain_display(self) -> str:
        """인과관계 체인을 보기 좋게 포맷팅."""
        if not self.causation_chain:
            return "No chain"
        return " → ".join(self.causation_chain)

    def verify_hash_integrity(self) -> bool:
        """
        해시 무결성 검증.

        저장된 current_hash와 재계산된 해시를 비교합니다.
        """
        import hashlib

        from baldur.utils.serialization import fast_canonical_dumps

        content = {
            "id": self.cascade_id,
            "trigger": {
                "trigger_type": self.trigger_type,
                "details": self.trigger_details,
            },
            "effects": self.effects,
            "namespace": self.namespace,
            "timestamp": self.timestamp.isoformat() if self.timestamp else "",
            "previous_hash": self.previous_hash,
        }
        computed_hash = hashlib.sha256(fast_canonical_dumps(content)).hexdigest()

        return computed_hash == self.current_hash

    @classmethod
    def from_cascade_event(cls, event: Any) -> AbstractCascadeEventArchive:
        """
        CascadeEvent 객체에서 Archive 모델 인스턴스 생성.

        Args:
            event: CascadeEvent 인스턴스

        Returns:
            AbstractCascadeEventArchive 인스턴스 (저장 전)
        """
        from datetime import datetime

        # timestamp 파싱
        if isinstance(event.timestamp, str):
            timestamp = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
        else:
            timestamp = event.timestamp

        return cls(
            cascade_id=event.id,
            namespace=event.namespace,
            trigger_type=event.trigger.trigger_type,
            trigger_details=event.trigger.details,
            effects=[e.to_dict() for e in event.effects],
            causation_chain=event.get_causation_chain(),
            previous_hash=event.previous_hash,
            current_hash=event.current_hash,
            total_effects=event.total_effects,
            success_count=event.success_count,
            failure_count=event.failure_count,
            timestamp=timestamp,
            external_trace=(
                event.external_trace.to_dict() if event.external_trace else None
            ),
            version=getattr(event, "version", "1.0"),
            is_test=getattr(event, "is_test", False),
        )


# =============================================================================
# Concrete Model (Django 환경에서 사용)
# =============================================================================

# Django 환경이 아닌 경우를 위한 기본 클래스
if DJANGO_AVAILABLE:

    class CascadeEventArchive(AbstractCascadeEventArchive):
        """
        Cascade Event Archive 구체 모델.

        Django 프로젝트에서 직접 사용하거나,
        상속하여 커스터마이징할 수 있습니다.

        테이블명: baldur_cascade_events

        PostgreSQL 파티셔닝 적용 시:
            CREATE TABLE baldur_cascade_events_2026_01
                PARTITION OF baldur_cascade_events
                FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
        """

        class Meta(AbstractCascadeEventArchive.Meta):
            abstract = False
            db_table = "baldur_cascade_events"
            app_label = "baldur"  # 명시적 app_label (테스트 환경용)
            # PostgreSQL 파티셔닝은 Migration에서 직접 처리

else:
    # Django 없는 환경에서는 빈 클래스
    class CascadeEventArchive:  # type: ignore
        """Django 없는 환경에서의 Placeholder."""

        pass
