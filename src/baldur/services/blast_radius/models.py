"""
Blast Radius DNA Models - 장애 영향 범위 관련 데이터 모델
"""

from dataclasses import dataclass, field
from datetime import datetime

from baldur.core.serializable import SerializableMixin
from baldur.models.blast_radius import BlastRadiusLevel


@dataclass
class ServiceDependencyEdge(SerializableMixin):
    """서비스 의존성 엣지"""

    source_service: str
    target_service: str
    dependency_type: str = "sync"  # sync, async, weak
    criticality: str = "medium"  # low, medium, high, critical
    metadata: dict = field(default_factory=dict)


@dataclass
class BlastRadiusPolicy(SerializableMixin):
    """영향 범위 정책"""

    policy_id: str
    service_name: str
    level: BlastRadiusLevel = BlastRadiusLevel.MINIMAL
    affected_services: list[str] = field(default_factory=list)
    max_affected_percentage: float = 10.0  # 최대 영향 비율 (%)
    auto_isolate: bool = True
    isolation_timeout_seconds: int = 300
    notify_threshold: BlastRadiusLevel = BlastRadiusLevel.MODERATE
    enabled: bool = True
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ImpactAssessment(SerializableMixin):
    """영향 평가 결과"""

    assessment_id: str
    service_name: str
    trigger_event: str
    level: BlastRadiusLevel
    affected_services: list[str]
    affected_users_estimate: int = 0
    affected_percentage: float = 0.0
    dependencies_analyzed: int = 0
    cascading_risk: bool = False
    recommendations: list[str] = field(default_factory=list)
    assessed_at: datetime = field(default_factory=datetime.now)

    @property
    def is_critical(self) -> bool:
        return self.level in [BlastRadiusLevel.EXTENSIVE, BlastRadiusLevel.CRITICAL]

    def _post_serialize(self, data: dict) -> dict:
        data["is_critical"] = self.is_critical
        return super()._post_serialize(data)
