"""
CellTopologySettings 계약 검증 테스트.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: 설계 문서에 명시된 기본값 계약 검증 (하드코딩)

참조 소스:
- settings/cell_topology.py (CellTopologySettings)
"""

from __future__ import annotations

import pytest

from baldur.settings.cell_topology import (
    CellTopologySettings,
    get_cell_topology_settings,
    reset_cell_topology_settings,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    """각 테스트 전후 싱글톤 리셋."""
    reset_cell_topology_settings()
    yield
    reset_cell_topology_settings()


class TestCellTopologySettingsContract:
    """CellTopologySettings 설계 계약값 검증."""

    def test_enabled_default_false(self):
        """마스터 토글 기본값: False."""
        settings = CellTopologySettings()
        assert settings.enabled is False

    def test_tagging_enabled_default_false(self):
        """태깅 기본값: False."""
        settings = CellTopologySettings()
        assert settings.tagging_enabled is False

    def test_bulkhead_isolation_enabled_default_false(self):
        """Bulkhead 격벽 기본값: False."""
        settings = CellTopologySettings()
        assert settings.bulkhead_isolation_enabled is False

    def test_evacuation_enabled_default_false(self):
        """대피 기능 기본값: False."""
        settings = CellTopologySettings()
        assert settings.evacuation_enabled is False

    def test_cell_count_default_8(self):
        """Cell 수 기본값: 8."""
        settings = CellTopologySettings()
        assert settings.cell_count == 8

    def test_cell_prefix_default_cell(self):
        """Cell 접두사 기본값: 'cell'."""
        settings = CellTopologySettings()
        assert settings.cell_prefix == "cell"

    def test_bulkhead_max_concurrent_per_cell_default_100(self):
        """Cell별 Bulkhead 최대 동시 요청 기본값: 100."""
        settings = CellTopologySettings()
        assert settings.bulkhead_max_concurrent_per_cell == 100

    def test_bulkhead_type_default_semaphore(self):
        """Bulkhead 유형 기본값: 'semaphore'."""
        settings = CellTopologySettings()
        assert settings.bulkhead_type == "semaphore"

    def test_evacuation_health_threshold_default_0_3(self):
        """대피 건강도 임계값 기본값: 0.3."""
        settings = CellTopologySettings()
        assert settings.evacuation_health_threshold == pytest.approx(0.3)

    def test_evacuation_traffic_drain_seconds_default_30(self):
        """트래픽 드레인 대기 시간 기본값: 30초."""
        settings = CellTopologySettings()
        assert settings.evacuation_traffic_drain_seconds == 30

    def test_health_check_interval_seconds_default_10(self):
        """건강 체크 주기 기본값: 10초."""
        settings = CellTopologySettings()
        assert settings.health_check_interval_seconds == 10

    def test_metrics_enabled_default_true(self):
        """메트릭 수집 기본값: True."""
        settings = CellTopologySettings()
        assert settings.metrics_enabled is True

    def test_warmup_initial_percentage_default_10(self):
        """새 Cell 초기 트래픽 비율 기본값: 10.0%."""
        settings = CellTopologySettings()
        assert settings.warmup_initial_percentage == pytest.approx(10.0)

    def test_warmup_step_percentage_default_20(self):
        """프로모션 단계별 증가량 기본값: 20.0%."""
        settings = CellTopologySettings()
        assert settings.warmup_step_percentage == pytest.approx(20.0)

    def test_warmup_step_interval_seconds_default_60(self):
        """프로모션 단계 간 대기 시간 기본값: 60.0초."""
        settings = CellTopologySettings()
        assert settings.warmup_step_interval_seconds == pytest.approx(60.0)

    def test_reconciliation_interval_seconds_default_15(self):
        """Reconciliation 주기 기본값: 15.0초."""
        settings = CellTopologySettings()
        assert settings.reconciliation_interval_seconds == pytest.approx(15.0)

    def test_service_heartbeat_interval_seconds_default_30(self):
        """서비스 Heartbeat 갱신 주기 기본값: 30.0초."""
        settings = CellTopologySettings()
        assert settings.service_heartbeat_interval_seconds == pytest.approx(30.0)

    def test_service_heartbeat_ttl_seconds_default_300(self):
        """서비스 Heartbeat 만료 시간 기본값: 300.0초 (5분)."""
        settings = CellTopologySettings()
        assert settings.service_heartbeat_ttl_seconds == pytest.approx(300.0)

    def test_internal_dns_suffixes_default(self):
        """내부 DNS 접미사 기본값: ['.svc.cluster.local', '.internal']."""
        settings = CellTopologySettings()
        assert settings.internal_dns_suffixes == [".svc.cluster.local", ".internal"]

    def test_trusted_source_cidrs_default(self):
        """신뢰 소스 CIDR 기본값: RFC 1918 사설 대역 + Loopback."""
        settings = CellTopologySettings()
        assert settings.trusted_source_cidrs == [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "127.0.0.0/8",
        ]


class TestCellTopologySettingsBehavior:
    """CellTopologySettings 동작 검증."""

    def test_singleton_returns_same_instance(self):
        """싱글톤은 동일 인스턴스를 반환해야 한다."""
        s1 = get_cell_topology_settings()
        s2 = get_cell_topology_settings()
        assert s1 is s2

    def test_reset_clears_singleton(self):
        """reset 후 새 인스턴스가 생성되어야 한다."""
        s1 = get_cell_topology_settings()
        reset_cell_topology_settings()
        s2 = get_cell_topology_settings()
        assert s1 is not s2

    def test_env_prefix_is_baldur_cell_topology(self):
        """환경변수 접두사: BALDUR_CELL_TOPOLOGY_."""
        prefix = CellTopologySettings.model_config.get("env_prefix")
        assert prefix == "BALDUR_CELL_TOPOLOGY_"
