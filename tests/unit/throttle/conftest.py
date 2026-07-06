"""
Throttle SLA 알림 테스트 공통 설정.

이 패키지의 모든 SLA 알림 테스트에서 사용하는 상수, factory, fixture를 정의합니다.
소스 모듈의 기본값과 동기화된 상수를 사용하여 설정 변경 시 테스트 수정 범위를 최소화합니다.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import os
import tempfile
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

# =============================================================================
# 소스 모듈에서 가져온 상수 (하드코딩 방지)
# =============================================================================
from baldur_pro.services.throttle.redis_cooldown_store import RedisCooldownStore

# Redis Cooldown 상수
KEY_PREFIX = RedisCooldownStore.KEY_PREFIX  # "baldur:notification:cooldown"
DEFAULT_COOLDOWN_SECONDS = 1800

# =============================================================================
# 테스트 전용 상수 (테스트 데이터)
# =============================================================================

# 서비스 이름
SVC_PAYMENT = "payment"
SVC_ORDER = "order"
SVC_DEFAULT = "default"

# Warning 시나리오 데이터
WARNING_RTT_MS = 250.0
WARNING_THRESHOLD_MS = 200
WARNING_CURRENT_LIMIT = 80
WARNING_PREVIOUS_LIMIT = 100
WARNING_GRADIENT = 0.25

# Critical 시나리오 데이터
CRITICAL_RTT_MS = 600.0
CRITICAL_THRESHOLD_MS = 500
CRITICAL_CURRENT_LIMIT = 70
CRITICAL_PREVIOUS_LIMIT = 100
CRITICAL_REDUCTION_PERCENT = 30
CRITICAL_GRADIENT = 0.5

# Recovered 시나리오 데이터
RECOVERED_RTT_MS = 50.0
RECOVERED_PREVIOUS_LIMIT = 70
RECOVERED_NEW_LIMIT = 100

# 환경변수 URL
TEST_DASHBOARD_URL = "https://grafana.internal/d/throttle"
TEST_ADMIN_BASE_URL = "/admin/throttle/"
TEST_RUNBOOK_URL = "https://docs.internal/runbooks/sla"

ALL_URL_ENVS = {
    "THROTTLE_SLA_DASHBOARD_URL": TEST_DASHBOARD_URL,
    "THROTTLE_SLA_ADMIN_BASE_URL": TEST_ADMIN_BASE_URL,
    "THROTTLE_SLA_RUNBOOK_URL": TEST_RUNBOOK_URL,
}

NO_URL_ENVS = {
    "THROTTLE_SLA_DASHBOARD_URL": "",
    "THROTTLE_SLA_ADMIN_BASE_URL": "",
    "THROTTLE_SLA_RUNBOOK_URL": "",
}

# Dedup key
DEDUP_KEY_PAYMENT = f"sla:throttle:{SVC_PAYMENT}"
DEDUP_KEY_ORDER = f"sla:throttle:{SVC_ORDER}"

# Settings 환경변수 prefix
SETTINGS_ENV_PREFIX = "BALDUR_THROTTLE_SLA_NOTIFICATION_"

# Celery 태스크 상수
TASK_NAME = "baldur.adapters.celery.tasks.send_sla_notification"
TASK_QUEUE = "baldur"
TASK_MAX_RETRIES = 3
TASK_DEFAULT_RETRY_DELAY = 30
TASK_TIME_LIMIT = 60
TASK_SOFT_TIME_LIMIT = 55

# Handler 상수
NOTIFICATION_SOURCE = "adaptive_throttle"


# =============================================================================
# Event Data Factory 함수
# =============================================================================


def make_warning_event_data(**overrides) -> dict:
    """Warning 시나리오 event_data 생성."""
    defaults = {
        "rtt_ms": WARNING_RTT_MS,
        "threshold_ms": WARNING_THRESHOLD_MS,
        "current_limit": WARNING_CURRENT_LIMIT,
        "previous_limit": WARNING_PREVIOUS_LIMIT,
        "gradient": WARNING_GRADIENT,
        "service_name": SVC_PAYMENT,
    }
    defaults.update(overrides)
    return defaults


def make_critical_event_data(**overrides) -> dict:
    """Critical 시나리오 event_data 생성."""
    defaults = {
        "rtt_ms": CRITICAL_RTT_MS,
        "threshold_ms": CRITICAL_THRESHOLD_MS,
        "current_limit": CRITICAL_CURRENT_LIMIT,
        "previous_limit": CRITICAL_PREVIOUS_LIMIT,
        "reduction_percent": CRITICAL_REDUCTION_PERCENT,
        "gradient": CRITICAL_GRADIENT,
        "service_name": SVC_PAYMENT,
    }
    defaults.update(overrides)
    return defaults


def make_recovered_event_data(**overrides) -> dict:
    """Recovered 시나리오 event_data 생성."""
    defaults = {
        "previous_limit": RECOVERED_PREVIOUS_LIMIT,
        "new_limit": RECOVERED_NEW_LIMIT,
        "rtt_ms": RECOVERED_RTT_MS,
        "service_name": SVC_PAYMENT,
    }
    defaults.update(overrides)
    return defaults


# =============================================================================
# Mock 객체 & Event
# =============================================================================


@dataclass
class MockEvent:
    """EventBus 이벤트 Mock."""

    data: dict


def make_warning_event(**overrides) -> MockEvent:
    """Warning MockEvent 생성."""
    return MockEvent(data=make_warning_event_data(**overrides))


def make_critical_event(**overrides) -> MockEvent:
    """Critical MockEvent 생성."""
    return MockEvent(data=make_critical_event_data(**overrides))


def make_recovered_event(**overrides) -> MockEvent:
    """Recovered MockEvent 생성."""
    return MockEvent(data=make_recovered_event_data(**overrides))


def make_notify_result(
    success: bool = True, channels: list[str] | None = None
) -> MagicMock:
    """notify_sla 반환값 Mock 생성."""
    result = MagicMock()
    result.success = success
    result.channels_sent = channels or ["slack"]
    result.suppressed = not success
    result.suppression_reason = "cooldown" if not success else None
    result.error = "send failed" if not success else None
    return result


# =============================================================================
# Redis Key 헬퍼
# =============================================================================


def make_redis_key(dedup_key: str) -> str:
    """Redis cooldown 키 전체 경로 생성."""
    return f"{KEY_PREFIX}:{dedup_key}"


# =============================================================================
# Pytest Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _isolate_throttle_state():
    """Reset EventBus + throttle + governance singletons around every throttle test.

    AdaptiveThrottle.__init__ subscribes to six EventBus event types but never
    unsubscribes. Instances leaked from earlier tests would stay attached to
    the singleton EventBus; a later CONFIG_UPDATED emission would rerun their
    `_on_config_updated` and swap `self.config` with the current global
    throttle settings — silently breaking `initial_limit=1` assumptions.

    AdaptiveThrottle.check() calls _sync_governance_state() which reads
    EmergencyManager singleton — stale emergency level from prior tests
    causes limit drift via adjust_for_emergency().

    Clearing all related singletons before every function cuts this
    cross-contamination at its root. See UNIT_TEST_GUIDELINES.md §6.5.
    """
    from baldur.services.event_bus.bus.convenience import reset_event_bus
    from baldur.settings.throttle import reset_throttle_settings
    from baldur_pro.services.emergency_mode import reset_emergency_manager
    from baldur_pro.services.throttle.adaptive import reset_adaptive_throttle

    reset_event_bus()
    reset_throttle_settings()
    reset_emergency_manager()
    reset_adaptive_throttle()

    yield

    reset_adaptive_throttle()
    reset_emergency_manager()
    reset_throttle_settings()
    reset_event_bus()


@pytest.fixture
def reset_url_builder():
    """URL Builder 싱글톤 리셋 (setup + teardown)."""
    from baldur_pro.services.throttle.throttle_sla_alert_urls import (
        reset_throttle_sla_alert_url_builder,
    )

    reset_throttle_sla_alert_url_builder()
    yield
    reset_throttle_sla_alert_url_builder()


@pytest.fixture
def mock_redis_ok() -> MagicMock:
    """정상 동작하는 Mock Redis 클라이언트."""
    mock = MagicMock()
    mock.exists.return_value = 0
    return mock


@pytest.fixture
def mock_redis_exists() -> MagicMock:
    """키가 존재하는 Mock Redis 클라이언트."""
    mock = MagicMock()
    mock.exists.return_value = 1
    return mock


@pytest.fixture
def mock_redis_down() -> MagicMock:
    """장애 상태 Mock Redis 클라이언트."""
    mock = MagicMock()
    mock.exists.side_effect = Exception("Redis down")
    mock.set.side_effect = Exception("Redis down")
    mock.delete.side_effect = Exception("Redis down")
    return mock


@pytest.fixture
def tmp_jsonl_file():
    """임시 JSONL 파일 경로 (cleanup 포함)."""
    tmpdir = tempfile.mkdtemp()
    filepath = os.path.join(tmpdir, "test_fallback.jsonl")
    yield filepath
    # cleanup
    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)
