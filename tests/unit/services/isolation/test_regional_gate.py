"""
RegionalIsolationGate Unit Tests.

"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock


class TestIsolationInfo:
    """IsolationInfo 테스트."""

    def test_basic_creation(self):
        """기본 생성 테스트."""
        from baldur.services.isolation.regional_gate import IsolationInfo

        info = IsolationInfo(
            region="tokyo",
            isolated=True,
            reason="High error rate",
            isolated_by="seoul-prod-01",
        )

        assert info.region == "tokyo"
        assert info.isolated is True
        assert info.reason == "High error rate"
        assert info.isolated_by == "seoul-prod-01"

    def test_to_dict(self):
        """to_dict 변환 테스트."""
        from baldur.services.isolation.regional_gate import IsolationInfo

        info = IsolationInfo(
            region="tokyo",
            isolated=True,
            reason="High error rate",
            isolated_by="seoul-prod-01",
            isolated_at=datetime(2026, 1, 19, 10, 0, 0, tzinfo=UTC),
        )

        data = info.to_dict()

        assert data["region"] == "tokyo"
        assert data["isolated"] is True
        assert data["reason"] == "High error rate"
        assert data["isolated_by"] == "seoul-prod-01"
        assert data["isolated_at"] == "2026-01-19T10:00:00+00:00"

    def test_from_dict(self):
        """from_dict 변환 테스트."""
        from baldur.services.isolation.regional_gate import IsolationInfo

        data = {
            "region": "tokyo",
            "isolated": True,
            "reason": "High error rate",
            "isolated_by": "seoul-prod-01",
            "isolated_at": "2026-01-19T10:00:00+00:00",
            "expires_at": None,
        }

        info = IsolationInfo.from_dict(data)

        assert info.region == "tokyo"
        assert info.isolated is True
        assert info.reason == "High error rate"


class TestRegionalIsolationGate:
    """RegionalIsolationGate 테스트."""

    def setup_method(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.core.cluster_identity import reset_cluster_identity
        from baldur.services.isolation.regional_gate import (
            reset_regional_isolation_gate,
        )

        reset_regional_isolation_gate()
        reset_cluster_identity()

    def teardown_method(self):
        """각 테스트 후에 싱글톤 리셋."""
        from baldur.core.cluster_identity import reset_cluster_identity
        from baldur.services.isolation.regional_gate import (
            reset_regional_isolation_gate,
        )

        reset_regional_isolation_gate()
        reset_cluster_identity()

    # NOTE: test_isolate_region_no_redis는 실제 Redis 연결을 시도하므로
    # tests/integration/baldur/test_regional_gate_integration.py로 이동됨

    def test_isolate_region_with_mock_redis(self):
        """Mock Redis로 리전 격리."""
        from baldur.core.cluster_identity import ClusterIdentity
        from baldur.services.isolation.regional_gate import RegionalIsolationGate

        mock_redis = MagicMock()
        mock_redis.set.return_value = True
        mock_redis.sadd.return_value = 1
        mock_redis.publish.return_value = 1

        identity = ClusterIdentity(
            cluster_id="seoul-prod-01",
            region="seoul",
        )

        gate = RegionalIsolationGate(
            global_redis=mock_redis,
            cluster_identity=identity,
        )

        result = gate.isolate_region(
            "tokyo",
            reason="High error rate",
            duration_seconds=300,
        )

        assert result is True
        mock_redis.set.assert_called_once()
        mock_redis.sadd.assert_called_once()
        mock_redis.publish.assert_called_once()

    def test_is_region_isolated_true(self):
        """격리된 리전 상태 확인."""
        from baldur.services.isolation.regional_gate import RegionalIsolationGate

        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps(
            {
                "region": "tokyo",
                "isolated": True,
                "reason": "High error rate",
                "isolated_by": "seoul-prod-01",
            }
        )

        gate = RegionalIsolationGate(global_redis=mock_redis)

        is_isolated, reason = gate.is_region_isolated("tokyo")

        assert is_isolated is True
        assert reason == "High error rate"

    def test_is_region_isolated_false(self):
        """격리되지 않은 리전 상태 확인."""
        from baldur.services.isolation.regional_gate import RegionalIsolationGate

        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        gate = RegionalIsolationGate(global_redis=mock_redis)

        is_isolated, reason = gate.is_region_isolated("seoul")

        assert is_isolated is False
        assert reason is None

    def test_restore_region(self):
        """리전 격리 해제."""
        from baldur.core.cluster_identity import ClusterIdentity
        from baldur.services.isolation.regional_gate import RegionalIsolationGate

        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # get_isolation_info returns None
        mock_redis.delete.return_value = 1
        mock_redis.srem.return_value = 1
        mock_redis.publish.return_value = 1

        identity = ClusterIdentity(
            cluster_id="seoul-prod-01",
            region="seoul",
        )

        gate = RegionalIsolationGate(
            global_redis=mock_redis,
            cluster_identity=identity,
        )

        result = gate.restore_region("tokyo")

        assert result is True
        mock_redis.delete.assert_called_once()
        mock_redis.srem.assert_called_once()

    def test_list_isolated_regions(self):
        """격리 중인 리전 목록 조회."""
        from baldur.services.isolation.regional_gate import RegionalIsolationGate

        mock_redis = MagicMock()
        mock_redis.smembers.return_value = {"tokyo", "osaka"}
        mock_redis.get.side_effect = [
            json.dumps(
                {
                    "region": "tokyo",
                    "isolated": True,
                    "reason": "High error rate",
                }
            ),
            json.dumps(
                {
                    "region": "osaka",
                    "isolated": True,
                    "reason": "Maintenance",
                }
            ),
        ]

        gate = RegionalIsolationGate(global_redis=mock_redis)

        isolated = gate.list_isolated_regions()

        assert len(isolated) == 2
        assert "tokyo" in isolated
        assert "osaka" in isolated

    def test_is_current_region_isolated(self):
        """현재 리전 격리 상태 확인."""
        from baldur.core.cluster_identity import ClusterIdentity
        from baldur.services.isolation.regional_gate import RegionalIsolationGate

        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps(
            {
                "region": "seoul",
                "isolated": True,
                "reason": "Emergency",
            }
        )

        identity = ClusterIdentity(
            cluster_id="seoul-prod-01",
            region="seoul",
        )

        gate = RegionalIsolationGate(
            global_redis=mock_redis,
            cluster_identity=identity,
        )

        is_isolated, reason = gate.is_current_region_isolated()

        assert is_isolated is True
        assert reason == "Emergency"

    def test_singleton(self):
        """싱글톤 패턴 테스트."""
        from baldur.services.isolation.regional_gate import (
            get_regional_isolation_gate,
            reset_regional_isolation_gate,
        )

        reset_regional_isolation_gate()

        g1 = get_regional_isolation_gate()
        g2 = get_regional_isolation_gate()

        assert g1 is g2
