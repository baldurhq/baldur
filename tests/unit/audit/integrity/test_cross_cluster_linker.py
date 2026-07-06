"""
CrossClusterAuditLinker Unit Tests.

Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
"""

from datetime import date
from unittest.mock import MagicMock


class TestClusterDailyAnchor:
    """ClusterDailyAnchor 테스트."""

    def test_basic_creation(self):
        """기본 생성 테스트."""
        from baldur.audit.integrity.cross_cluster_linker import ClusterDailyAnchor

        anchor = ClusterDailyAnchor(
            cluster_id="seoul-prod-01",
            anchor_date=date(2026, 1, 18),
            final_sequence=1000,
            final_hash="abc123def456",
            entry_count=1000,
        )

        assert anchor.cluster_id == "seoul-prod-01"
        assert anchor.anchor_date == date(2026, 1, 18)
        assert anchor.final_sequence == 1000
        assert anchor.final_hash == "abc123def456"
        assert anchor.entry_count == 1000
        assert anchor.created_at is not None

    def test_to_dict(self):
        """to_dict 변환 테스트."""
        from baldur.audit.integrity.cross_cluster_linker import ClusterDailyAnchor

        anchor = ClusterDailyAnchor(
            cluster_id="tokyo-prod-01",
            anchor_date=date(2026, 1, 18),
            final_sequence=500,
            final_hash="xyz789",
            entry_count=500,
        )

        data = anchor.to_dict()

        assert data["cluster_id"] == "tokyo-prod-01"
        assert data["date"] == "2026-01-18"
        assert data["final_sequence"] == 500
        assert data["final_hash"] == "xyz789"
        assert data["entry_count"] == 500
        assert "created_at" in data

    def test_from_dict(self):
        """from_dict 변환 테스트."""
        from baldur.audit.integrity.cross_cluster_linker import ClusterDailyAnchor

        data = {
            "cluster_id": "seoul-prod-01",
            "date": "2026-01-18",
            "final_sequence": 1000,
            "final_hash": "abc123",
            "entry_count": 1000,
            "created_at": "2026-01-19T00:00:00+00:00",
        }

        anchor = ClusterDailyAnchor.from_dict(data)

        assert anchor.cluster_id == "seoul-prod-01"
        assert anchor.anchor_date == date(2026, 1, 18)
        assert anchor.final_sequence == 1000

    def test_compute_anchor_hash(self):
        """앵커 해시 계산 테스트."""
        from baldur.audit.integrity.cross_cluster_linker import ClusterDailyAnchor

        anchor = ClusterDailyAnchor(
            cluster_id="seoul-prod-01",
            anchor_date=date(2026, 1, 18),
            final_sequence=1000,
            final_hash="abc123",
            entry_count=1000,
        )

        hash1 = anchor.compute_anchor_hash()

        # 같은 데이터면 같은 해시
        anchor2 = ClusterDailyAnchor(
            cluster_id="seoul-prod-01",
            anchor_date=date(2026, 1, 18),
            final_sequence=1000,
            final_hash="abc123",
            entry_count=1000,
        )

        hash2 = anchor2.compute_anchor_hash()

        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256


class TestGlobalDailyAnchor:
    """GlobalDailyAnchor 테스트."""

    def test_global_hash_computed_on_init(self):
        """생성 시 글로벌 해시 자동 계산."""
        from baldur.audit.integrity.cross_cluster_linker import (
            ClusterDailyAnchor,
            GlobalDailyAnchor,
        )

        anchors = [
            ClusterDailyAnchor(
                cluster_id="seoul-prod-01",
                anchor_date=date(2026, 1, 18),
                final_sequence=1000,
                final_hash="abc123",
                entry_count=1000,
            ),
            ClusterDailyAnchor(
                cluster_id="tokyo-prod-01",
                anchor_date=date(2026, 1, 18),
                final_sequence=500,
                final_hash="xyz789",
                entry_count=500,
            ),
        ]

        global_anchor = GlobalDailyAnchor(
            anchor_date=date(2026, 1, 18),
            cluster_anchors=anchors,
        )

        assert global_anchor.global_hash != ""
        assert len(global_anchor.global_hash) == 64

    def test_global_hash_deterministic(self):
        """글로벌 해시는 결정론적."""
        from baldur.audit.integrity.cross_cluster_linker import (
            ClusterDailyAnchor,
            GlobalDailyAnchor,
        )

        anchors1 = [
            ClusterDailyAnchor(
                cluster_id="seoul-prod-01",
                anchor_date=date(2026, 1, 18),
                final_sequence=1000,
                final_hash="abc123",
                entry_count=1000,
            ),
            ClusterDailyAnchor(
                cluster_id="tokyo-prod-01",
                anchor_date=date(2026, 1, 18),
                final_sequence=500,
                final_hash="xyz789",
                entry_count=500,
            ),
        ]

        # 순서가 달라도 결과는 같아야 함 (cluster_id 순으로 정렬)
        anchors2 = [
            ClusterDailyAnchor(
                cluster_id="tokyo-prod-01",
                anchor_date=date(2026, 1, 18),
                final_sequence=500,
                final_hash="xyz789",
                entry_count=500,
            ),
            ClusterDailyAnchor(
                cluster_id="seoul-prod-01",
                anchor_date=date(2026, 1, 18),
                final_sequence=1000,
                final_hash="abc123",
                entry_count=1000,
            ),
        ]

        global1 = GlobalDailyAnchor(
            anchor_date=date(2026, 1, 18), cluster_anchors=anchors1
        )
        global2 = GlobalDailyAnchor(
            anchor_date=date(2026, 1, 18), cluster_anchors=anchors2
        )

        assert global1.global_hash == global2.global_hash

    def test_to_dict(self):
        """to_dict 변환 테스트."""
        from baldur.audit.integrity.cross_cluster_linker import (
            ClusterDailyAnchor,
            GlobalDailyAnchor,
        )

        anchors = [
            ClusterDailyAnchor(
                cluster_id="seoul-prod-01",
                anchor_date=date(2026, 1, 18),
                final_sequence=1000,
                final_hash="abc123",
                entry_count=1000,
            ),
        ]

        global_anchor = GlobalDailyAnchor(
            anchor_date=date(2026, 1, 18),
            cluster_anchors=anchors,
        )

        data = global_anchor.to_dict()

        assert data["date"] == "2026-01-18"
        assert data["cluster_count"] == 1
        assert len(data["cluster_anchors"]) == 1
        assert "global_hash" in data

    def test_from_dict(self):
        """from_dict 변환 테스트."""
        from baldur.audit.integrity.cross_cluster_linker import GlobalDailyAnchor

        data = {
            "date": "2026-01-18",
            "cluster_count": 1,
            "cluster_anchors": [
                {
                    "cluster_id": "seoul-prod-01",
                    "date": "2026-01-18",
                    "final_sequence": 1000,
                    "final_hash": "abc123",
                    "entry_count": 1000,
                    "created_at": "2026-01-19T00:00:00+00:00",
                }
            ],
            "global_hash": "dummy_hash",
            "created_at": "2026-01-19T00:00:00+00:00",
        }

        global_anchor = GlobalDailyAnchor.from_dict(data)

        assert global_anchor.anchor_date == date(2026, 1, 18)
        assert len(global_anchor.cluster_anchors) == 1
        assert global_anchor.cluster_anchors[0].cluster_id == "seoul-prod-01"


class TestCrossClusterAuditLinker:
    """CrossClusterAuditLinker 테스트."""

    def setup_method(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.audit.integrity.cross_cluster_linker import (
            reset_cross_cluster_audit_linker,
        )
        from baldur.core.cluster_identity import reset_cluster_identity

        reset_cross_cluster_audit_linker()
        reset_cluster_identity()

    def teardown_method(self):
        """각 테스트 후에 싱글톤 리셋."""
        from baldur.audit.integrity.cross_cluster_linker import (
            reset_cross_cluster_audit_linker,
        )
        from baldur.core.cluster_identity import reset_cluster_identity

        reset_cross_cluster_audit_linker()
        reset_cluster_identity()

    # NOTE: test_create_local_anchor_no_redis는 실제 Redis 연결을 시도하므로
    # tests/integration/baldur/test_regional_gate_integration.py로 이동됨

    def test_create_local_anchor_with_mock_redis(self):
        """Mock Redis로 로컬 앵커 생성."""
        from baldur.audit.integrity.cross_cluster_linker import (
            CrossClusterAuditLinker,
        )
        from baldur.core.cluster_identity import ClusterIdentity

        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {
            "sequence": "1000",
            "previous_hash": "abc123def456",
        }
        mock_redis.set.return_value = True

        identity = ClusterIdentity(
            cluster_id="seoul-prod-01",
            region="seoul",
        )

        linker = CrossClusterAuditLinker(
            local_redis=mock_redis,
            cluster_identity=identity,
            key_prefix="baldur:seoul:",
        )

        anchor = linker.create_local_anchor(target_date=date(2026, 1, 18))

        assert anchor is not None
        assert anchor.cluster_id == "seoul-prod-01"
        assert anchor.final_sequence == 1000
        assert anchor.final_hash == "abc123def456"

    def test_submit_to_global_new_anchor(self):
        """새 앵커 글로벌 제출."""
        from baldur.audit.integrity.cross_cluster_linker import (
            ClusterDailyAnchor,
            CrossClusterAuditLinker,
        )

        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # 기존 글로벌 앵커 없음
        mock_redis.set.return_value = True
        mock_redis.zadd.return_value = 1

        linker = CrossClusterAuditLinker(
            local_redis=mock_redis,
            global_redis=mock_redis,
        )

        anchor = ClusterDailyAnchor(
            cluster_id="seoul-prod-01",
            anchor_date=date(2026, 1, 18),
            final_sequence=1000,
            final_hash="abc123",
            entry_count=1000,
        )

        result = linker.submit_to_global(anchor)

        assert result is True
        mock_redis.set.assert_called()
        mock_redis.zadd.assert_called()

    def test_verify_global_integrity_not_found(self):
        """글로벌 앵커 없을 때 검증."""
        from baldur.audit.integrity.cross_cluster_linker import (
            CrossClusterAuditLinker,
        )

        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        linker = CrossClusterAuditLinker(
            local_redis=mock_redis,
            global_redis=mock_redis,
        )

        result = linker.verify_global_integrity(date(2026, 1, 18))

        assert result["valid"] is False
        assert "not found" in result["error"].lower()

    def test_singleton(self):
        """싱글톤 패턴 테스트."""
        from baldur.audit.integrity.cross_cluster_linker import (
            get_cross_cluster_audit_linker,
            reset_cross_cluster_audit_linker,
        )

        reset_cross_cluster_audit_linker()

        l1 = get_cross_cluster_audit_linker()
        l2 = get_cross_cluster_audit_linker()

        assert l1 is l2
