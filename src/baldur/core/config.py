"""
Configuration management for the baldur system.

NOTE: This module re-exports from baldur.settings for backward compatibility.
New code should use baldur.settings directly.

Data models like ApprovalRequest remain here as they are not configuration.
"""

from dataclasses import dataclass, field
from typing import Any

from baldur.settings import BaldurSettings as BaldurConfig

# Re-export from settings for backward compatibility
from baldur.settings import ChaosSettings as ChaosConfig
from baldur.settings import (
    CircuitBreakerAdvancedSettings as CircuitBreakerAdvancedConfig,
)
from baldur.settings import CircuitBreakerSettings as CircuitBreakerConfig
from baldur.settings import DLQSettings as DLQConfig
from baldur.settings import DriftThresholdSettings as DriftThresholdConfig
from baldur.settings import ErrorBudgetSettings as ErrorBudgetConfig
from baldur.settings import ForensicSettings as ForensicConfig
from baldur.settings import GovernanceSettings as GovernanceConfig
from baldur.settings import IdempotencySettings as IdempotencyConfig
from baldur.settings import L2StorageSettings as L2StorageConfig
from baldur.settings import LoggingSettings as LoggingConfig
from baldur.settings import MetricsSettings as MetricsConfig
from baldur.settings import NotificationSettings as NotificationConfig
from baldur.settings import RateLimitSettings as RateLimitConfig
from baldur.settings import ReplayAutomationSettings as ReplayAutomationConfig
from baldur.settings import RetrySettings as RetryConfig
from baldur.settings import SecuritySettings as SecurityConfig
from baldur.settings import SLASettings as SLAConfig
from baldur.settings import (
    configure,
    get_circuit_breaker_advanced_settings,
    get_circuit_breaker_settings,
    get_config,
    get_dlq_settings,
    get_forensic_settings,
    get_notification_settings,
    get_retry_settings,
    get_security_thresholds,
    get_sla_thresholds,
    reload_config,
    reset_config,
    set_config,
)

# =============================================================================
# Data Models (not configuration, so kept here)
# =============================================================================


@dataclass
class ApprovalRequest:
    """
    4-Eyes Approval Request (듀얼 승인 요청).

    Admin A가 요청 → Admin B가 승인/거부하는 워크플로우.
    금융권 컴플라이언스 요구사항 충족.

    Workflow:
        1. Admin A: 요청 생성 (PENDING)
        2. Admin B: 알림 수신
        3. Admin B: 24시간 내 APPROVED/REJECTED
        4. 만료 시: EXPIRED

    PCI-DSS Dual Control Requirements 준수.
    """

    id: str = ""
    request_type: str = ""  # config_change, mode_change, emergency_action
    description: str = ""

    # 요청자
    requested_by: str = ""
    requested_at: str = ""  # ISO format

    # 승인자
    approved_by: str = ""
    approved_at: str = ""  # ISO format

    # 상태: PENDING, APPROVED, REJECTED, EXPIRED
    status: str = "PENDING"

    # 요청 데이터
    payload: dict[str, Any] = field(default_factory=dict)

    # 만료 시간 (기본 24시간)
    expires_at: str = ""  # ISO format


__all__ = [
    # Settings classes (re-exported)
    "BaldurConfig",
    "CircuitBreakerConfig",
    "CircuitBreakerAdvancedConfig",
    "DLQConfig",
    "RetryConfig",
    "SLAConfig",
    "RateLimitConfig",
    "SecurityConfig",
    "IdempotencyConfig",
    "ForensicConfig",
    "MetricsConfig",
    "NotificationConfig",
    "GovernanceConfig",
    "ErrorBudgetConfig",
    "ChaosConfig",
    "DriftThresholdConfig",
    "L2StorageConfig",
    "LoggingConfig",
    "ReplayAutomationConfig",
    # Functions
    "get_config",
    "set_config",
    "reset_config",
    "reload_config",
    "configure",
    "get_sla_thresholds",
    "get_security_thresholds",
    "get_forensic_settings",
    "get_notification_settings",
    "get_dlq_settings",
    "get_retry_settings",
    "get_circuit_breaker_settings",
    "get_circuit_breaker_advanced_settings",
    # Data models
    "ApprovalRequest",
]
