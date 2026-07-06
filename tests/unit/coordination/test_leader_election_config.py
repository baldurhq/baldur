"""
Leader Election 설정 테스트.

LeaderElectionSettings 클래스의 설정 및 검증 로직 테스트.
"""

import logging
import os
from unittest.mock import patch

import pytest

from baldur.coordination.config import (
    LeaderElectionSettings,
    get_leader_election_settings,
    reset_leader_election_settings,
)
from baldur.settings.redis import reset_redis_settings

LEADER_LOGGER = "baldur.settings.leader_election"
RESOLVED_EVENT = "leader_election.redis_url_resolved"


@pytest.fixture(autouse=True)
def reset_settings():
    """각 테스트 전후 설정 리셋."""
    reset_leader_election_settings()
    reset_redis_settings()
    yield
    reset_leader_election_settings()
    reset_redis_settings()


class TestLeaderElectionSettings:
    """LeaderElectionSettings 테스트."""

    def test_default_values(self):
        """기본값 확인.

        enabled / self_fencing_enabled 기본값은 False
        (impl 527, v1.1 deferred).
        """
        settings = LeaderElectionSettings()

        assert settings.enabled is False
        assert settings.backend == "redis"
        assert settings.lease_ttl_seconds == 30
        assert settings.region_priority == 100
        assert settings.self_fencing_enabled is False
        assert settings.redis_key_prefix == "baldur:leader:"

    def test_get_node_id_with_explicit_value(self):
        """명시적 node_id 설정 확인."""
        settings = LeaderElectionSettings(node_id="my-custom-node")
        assert settings.get_node_id() == "my-custom-node"

    def test_get_node_id_from_hostname_env(self):
        """HOSTNAME 환경변수에서 node_id 가져오기."""
        with patch.dict(os.environ, {"HOSTNAME": "test-pod-123"}):
            settings = LeaderElectionSettings(node_id="")
            assert settings.get_node_id() == "test-pod-123"

    def test_get_effective_renew_interval_auto_calculated(self):
        """자동 계산된 renew_interval 확인."""
        settings = LeaderElectionSettings(
            lease_ttl_seconds=30,
            renew_interval_seconds=None,
            lease_safety_margin_ratio=0.1,
        )
        # 30/3 - 30*0.1 = 10 - 3 = 7
        assert settings.get_effective_renew_interval() == 7.0

    def test_get_effective_renew_interval_explicit(self):
        """명시적 renew_interval 확인."""
        settings = LeaderElectionSettings(
            lease_ttl_seconds=30,
            renew_interval_seconds=8.0,
        )
        assert settings.get_effective_renew_interval() == 8.0

    def test_timing_validation_renew_too_long(self):
        """renew_interval이 lease_ttl/2 이상일 때 에러."""
        with pytest.raises(ValueError, match="must be < lease_ttl/2"):
            LeaderElectionSettings(
                lease_ttl_seconds=30,
                renew_interval_seconds=20.0,  # 30/2=15보다 큼
            )

    def test_timing_validation_pass(self):
        """유효한 타이밍 설정 통과."""
        settings = LeaderElectionSettings(
            lease_ttl_seconds=60,
            renew_interval_seconds=15.0,  # 60/2=30보다 작음
        )
        assert settings.renew_interval_seconds == 15.0

    def test_region_priority_bounds(self):
        """리전 우선순위 범위 확인."""
        settings = LeaderElectionSettings(region_priority=0)
        assert settings.region_priority == 0

        settings = LeaderElectionSettings(region_priority=1000)
        assert settings.region_priority == 1000

    def test_get_leader_election_settings_singleton(self):
        """싱글톤 패턴 확인."""
        settings1 = get_leader_election_settings()
        settings2 = get_leader_election_settings()
        assert settings1 is settings2

    def test_reset_leader_election_settings(self):
        """설정 리셋 확인."""
        settings1 = get_leader_election_settings()
        reset_leader_election_settings()
        settings2 = get_leader_election_settings()
        # 새 인스턴스이므로 다른 객체
        assert settings1 is not settings2

    def test_env_prefix(self):
        """환경변수 접두사 확인."""
        with patch.dict(
            os.environ,
            {
                "BALDUR_LEADER_ELECTION_ENABLED": "false",
                "BALDUR_LEADER_ELECTION_BACKEND": "redis",
                "BALDUR_LEADER_ELECTION_LEASE_TTL_SECONDS": "60",
            },
        ):
            reset_leader_election_settings()
            settings = LeaderElectionSettings()
            assert settings.enabled is False
            assert settings.lease_ttl_seconds == 60


class TestLeaderElectionK8sSettingsContract:
    """K8s backend settings contract verification (336)."""

    def test_backend_supports_kubernetes(self):
        """backend accepts 'kubernetes' as valid value."""
        settings = LeaderElectionSettings(backend="kubernetes", node_id="test")
        assert settings.backend == "kubernetes"

    def test_backend_default_is_redis(self):
        """backend default is 'redis'."""
        settings = LeaderElectionSettings()
        assert settings.backend == "redis"

    def test_k8s_namespace_default(self):
        """k8s_namespace default is 'default'."""
        settings = LeaderElectionSettings()
        assert settings.k8s_namespace == "default"

    def test_k8s_in_cluster_default(self):
        """k8s_in_cluster default is True."""
        settings = LeaderElectionSettings()
        assert settings.k8s_in_cluster is True

    def test_k8s_namespace_from_env(self):
        """k8s_namespace loaded from BALDUR_LEADER_ELECTION_K8S_NAMESPACE env var."""
        with patch.dict(
            os.environ, {"BALDUR_LEADER_ELECTION_K8S_NAMESPACE": "kube-system"}
        ):
            settings = LeaderElectionSettings()
            assert settings.k8s_namespace == "kube-system"

    def test_k8s_in_cluster_from_env(self):
        """k8s_in_cluster loaded from BALDUR_LEADER_ELECTION_K8S_IN_CLUSTER env var."""
        with patch.dict(os.environ, {"BALDUR_LEADER_ELECTION_K8S_IN_CLUSTER": "false"}):
            settings = LeaderElectionSettings()
            assert settings.k8s_in_cluster is False

    def test_backend_rejects_invalid_values(self):
        """Invalid backend values are rejected by Pydantic validation."""
        with pytest.raises(Exception):
            LeaderElectionSettings(backend="etcd", node_id="test")

    def test_k8s_settings_custom_values(self):
        """Custom K8s settings are applied."""
        settings = LeaderElectionSettings(
            backend="kubernetes",
            node_id="my-pod",
            k8s_namespace="production",
            k8s_in_cluster=False,
        )
        assert settings.k8s_namespace == "production"
        assert settings.k8s_in_cluster is False


def _resolved_records(caplog) -> list:
    """Return caplog records matching the redis_url_resolved structlog event.

    Under tests/conftest.py's structlog config, ``wrap_for_formatter`` makes
    the entire structlog event_dict the LogRecord's ``msg``. The event name
    and kwargs are accessed via the dict, not as record attributes.
    """
    return [r for r in caplog.records if _event_dict(r).get("event") == RESOLVED_EVENT]


def _event_dict(record) -> dict:
    """Extract the structlog event_dict from a stdlib LogRecord (or {} if not structlog)."""
    if isinstance(record.msg, dict):
        return record.msg
    if isinstance(record.msg, tuple) and record.msg and isinstance(record.msg[0], dict):
        return record.msg[0]
    return {}


# 525 D4: xdist 격리 flake — env var patch + settings singleton race
# (project_xdist_isolation pattern). Class-level marker because all 4
# methods in the class flake under -n 6 parallel collection.
@pytest.mark.flaky_quarantine(
    issue="525", first_seen="2026-05-20", category="env_isolation"
)
class TestLeaderElectionRedisUrlFallback:
    """469/D3: redis_url 해석 우선순위 (BALDUR_LEADER_ELECTION_REDIS_URL > BALDUR_REDIS_URL > default)."""

    REDIS_DEFAULT = "redis://localhost:6379/0"

    @pytest.fixture(autouse=True)
    def _isolate_redis_env(self, monkeypatch):
        """각 테스트가 두 Redis URL env 모두 비운 상태에서 시작하도록 격리."""
        monkeypatch.delenv("BALDUR_LEADER_ELECTION_REDIS_URL", raising=False)
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        return

    @staticmethod
    def _enable_info_capture(caplog) -> None:
        """caplog 모든 활성 LogCaptureHandler를 INFO로 낮춰 _fallback_redis_url의
        INFO 이벤트가 캡처되도록 한다.

        ``caplog.set_level()``은 케이스에 따라 모든 LogCaptureHandler에 적용되지
        않으므로 root에 attach된 모든 LogCaptureHandler를 직접 INFO로 낮춘다.
        """
        caplog.set_level(logging.INFO, logger=LEADER_LOGGER)
        caplog.set_level(logging.INFO)
        for handler in logging.getLogger().handlers:
            if handler.__class__.__name__ == "LogCaptureHandler":
                handler.setLevel(logging.INFO)

    def test_leader_redis_url_wins_when_set(self, monkeypatch, caplog):
        """(a) BALDUR_LEADER_ELECTION_REDIS_URL만 설정 → 해당 값이 채택, source=BALDUR_LEADER_ELECTION_REDIS_URL."""
        self._enable_info_capture(caplog)
        monkeypatch.setenv(
            "BALDUR_LEADER_ELECTION_REDIS_URL", "redis://leader-host:6379/2"
        )
        settings = LeaderElectionSettings()

        assert settings.redis_url == "redis://leader-host:6379/2"
        records = _resolved_records(caplog)
        assert len(records) == 1
        ev = _event_dict(records[0])
        assert ev["source"] == "BALDUR_LEADER_ELECTION_REDIS_URL"
        assert ev["redis_url"] == "redis://leader-host:6379/2"
        assert records[0].levelno == logging.INFO

    def test_global_redis_url_used_when_only_global_set(self, monkeypatch, caplog):
        """(b) BALDUR_REDIS_URL만 설정 → fallback이 채택, source=BALDUR_REDIS_URL."""
        self._enable_info_capture(caplog)
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://global-host:6379/3")
        settings = LeaderElectionSettings()

        assert settings.redis_url == "redis://global-host:6379/3"
        records = _resolved_records(caplog)
        assert len(records) == 1
        ev = _event_dict(records[0])
        assert ev["source"] == "BALDUR_REDIS_URL"
        assert ev["redis_url"] == "redis://global-host:6379/3"

    def test_leader_redis_url_overrides_global(self, monkeypatch, caplog):
        """(c) 둘 다 설정 → BALDUR_LEADER_ELECTION_REDIS_URL 우선, source=BALDUR_LEADER_ELECTION_REDIS_URL."""
        self._enable_info_capture(caplog)
        monkeypatch.setenv(
            "BALDUR_LEADER_ELECTION_REDIS_URL", "redis://leader-host:6379/2"
        )
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://global-host:6379/3")
        settings = LeaderElectionSettings()

        assert settings.redis_url == "redis://leader-host:6379/2"
        records = _resolved_records(caplog)
        assert len(records) == 1
        ev = _event_dict(records[0])
        assert ev["source"] == "BALDUR_LEADER_ELECTION_REDIS_URL"

    def test_default_used_when_neither_set(self, caplog):
        """(d) 둘 다 미설정 → RedisSettings.url 기본값으로 fallback, source=default."""
        self._enable_info_capture(caplog)
        settings = LeaderElectionSettings()

        assert settings.redis_url == self.REDIS_DEFAULT
        records = _resolved_records(caplog)
        assert len(records) == 1
        ev = _event_dict(records[0])
        assert ev["source"] == "default"
        assert ev["redis_url"] == self.REDIS_DEFAULT
