"""
Audit 모듈 Settings 연동 테스트.

Step 3 리팩토링 검증:
- hash_chain_safety.py: AtomicMergeSwap, ShardedDateLock, IntegrityAuditTrail
- cascade_auditor.py: CascadeEventAuditor
- anchor.py: DailyHashAnchor
- buffer.py: InMemoryAuditBuffer
- cross_cluster_linker.py: CrossClusterAuditLinker
- health_score.py: IntegrityHealthScore
- config.py: get_recommended_retention
- s3_worm.py: S3WORMBackend
"""

from unittest.mock import MagicMock


class TestHashChainSafetySettingsIntegration:
    """hash_chain_safety.py settings 연동 테스트."""

    def test_atomic_merge_swap_uses_settings_defaults(self, monkeypatch):
        """AtomicMergeSwap이 HashChainSettings의 기본값을 사용하는지 확인."""
        # Settings 캐시 리셋
        from baldur.settings import hash_chain

        hash_chain.reset_hash_chain_settings()

        # 환경변수 설정
        monkeypatch.setenv("BALDUR_HASH_CHAIN_MERGE_SWAP_TIMEOUT_SECONDS", "400")
        monkeypatch.setenv(
            "BALDUR_HASH_CHAIN_MERGE_SWAP_BLOCKING_TIMEOUT_SECONDS", "15.0"
        )

        # 새 인스턴스 생성
        from baldur.audit.hash_chain_safety import AtomicMergeSwap

        mock_redis = MagicMock()

        swap = AtomicMergeSwap(redis_client=mock_redis)

        assert swap._timeout == 400
        assert swap._blocking_timeout == 15.0

        # 정리
        hash_chain.reset_hash_chain_settings()

    def test_atomic_merge_swap_explicit_override(self, monkeypatch):
        """AtomicMergeSwap에 명시적 값이 settings를 오버라이드하는지 확인."""
        from baldur.settings import hash_chain

        hash_chain.reset_hash_chain_settings()

        monkeypatch.setenv("BALDUR_HASH_CHAIN_MERGE_SWAP_TIMEOUT_SECONDS", "400")

        from baldur.audit.hash_chain_safety import AtomicMergeSwap

        mock_redis = MagicMock()

        # 명시적 값 전달
        swap = AtomicMergeSwap(
            redis_client=mock_redis, timeout_seconds=500, blocking_timeout=20.0
        )

        assert swap._timeout == 500
        assert swap._blocking_timeout == 20.0

        hash_chain.reset_hash_chain_settings()

    def test_sharded_date_lock_uses_settings_defaults(self, monkeypatch):
        """ShardedDateLock이 HashChainSettings의 기본값을 사용하는지 확인."""
        from baldur.settings import hash_chain

        hash_chain.reset_hash_chain_settings()

        monkeypatch.setenv("BALDUR_HASH_CHAIN_DATE_LOCK_TIMEOUT_SECONDS", "180")
        monkeypatch.setenv(
            "BALDUR_HASH_CHAIN_DATE_LOCK_BLOCKING_TIMEOUT_SECONDS", "8.0"
        )

        from baldur.audit.hash_chain_safety import ShardedDateLock

        mock_redis = MagicMock()

        lock = ShardedDateLock(redis_client=mock_redis, date="2026-01-25")

        assert lock._timeout == 180
        assert lock._blocking_timeout == 8.0

        hash_chain.reset_hash_chain_settings()

    def test_integrity_audit_trail_uses_settings_defaults(self, monkeypatch):
        """IntegrityAuditTrail이 HashChainSettings의 기본값을 사용하는지 확인."""
        from baldur.settings import hash_chain

        hash_chain.reset_hash_chain_settings()

        monkeypatch.setenv(
            "BALDUR_HASH_CHAIN_INTEGRITY_TRAIL_MAX_REDIS_ENTRIES", "2000"
        )

        from baldur.audit.hash_chain_safety import IntegrityAuditTrail

        trail = IntegrityAuditTrail()

        assert trail._max_redis_entries == 2000

        hash_chain.reset_hash_chain_settings()


class TestCascadeAuditorSettingsIntegration:
    """cascade_auditor.py settings 연동 테스트."""

    def test_cascade_event_auditor_uses_settings_defaults(self, monkeypatch):
        """CascadeEventAuditor가 CascadeRetentionSettings의 기본값을 사용하는지 확인."""
        from baldur.settings import cascade_retention

        cascade_retention.reset_cascade_retention_settings()

        monkeypatch.setenv("BALDUR_CASCADE_RETENTION_MAX_CASCADE_INDEX_SIZE", "20000")

        from baldur.audit.cascade_auditor import CascadeEventAuditor

        auditor = CascadeEventAuditor()

        assert auditor._max_index_size == 20000

        cascade_retention.reset_cascade_retention_settings()


class TestAnchorSettingsIntegration:
    """anchor.py settings 연동 테스트."""

    def test_daily_hash_anchor_uses_settings_defaults(self, monkeypatch):
        """DailyHashAnchor가 AuditIntegritySettings의 기본값을 사용하는지 확인."""
        from baldur.settings import audit_integrity

        audit_integrity.reset_audit_integrity_settings()

        monkeypatch.setenv("BALDUR_AUDIT_INTEGRITY_ANCHOR_RETENTION_DAYS", "120")

        from baldur.audit.integrity.anchor import DailyHashAnchor

        mock_redis = MagicMock()

        anchor = DailyHashAnchor(redis_client=mock_redis)

        assert anchor._retention_days == 120

        audit_integrity.reset_audit_integrity_settings()


class TestBufferSettingsIntegration:
    """buffer.py settings 연동 테스트."""

    def test_inmemory_audit_buffer_uses_settings_defaults(self, monkeypatch):
        """InMemoryAuditBuffer가 ResilientRecorderSettings의 기본값을 사용하는지 확인."""
        from baldur.settings import resilient_recorder

        resilient_recorder.reset_resilient_recorder_settings()
        from baldur.audit.resilience.buffer import InMemoryAuditBuffer

        InMemoryAuditBuffer.reset_instance()

        monkeypatch.setenv(
            "BALDUR_RESILIENT_RECORDER_MEMORY_BUFFER_MAX_ENTRIES", "5000"
        )
        monkeypatch.setenv(
            "BALDUR_RESILIENT_RECORDER_MEMORY_BUFFER_FLUSH_INTERVAL", "60.0"
        )

        buffer = InMemoryAuditBuffer()

        assert buffer._max_entries == 5000
        assert buffer._flush_interval_seconds == 60.0

        resilient_recorder.reset_resilient_recorder_settings()
        InMemoryAuditBuffer.reset_instance()

    def test_inmemory_audit_buffer_stats_uses_instance_values(self, monkeypatch):
        """InMemoryAuditBuffer.get_stats()가 인스턴스 값을 사용하는지 확인."""
        from baldur.settings import resilient_recorder

        resilient_recorder.reset_resilient_recorder_settings()
        from baldur.audit.resilience.buffer import InMemoryAuditBuffer

        InMemoryAuditBuffer.reset_instance()

        monkeypatch.setenv(
            "BALDUR_RESILIENT_RECORDER_MEMORY_BUFFER_MAX_ENTRIES", "8000"
        )
        monkeypatch.setenv(
            "BALDUR_RESILIENT_RECORDER_MEMORY_BUFFER_FLUSH_INTERVAL", "45.0"
        )

        buffer = InMemoryAuditBuffer()
        stats = buffer.get_stats()

        assert stats["max_entries"] == 8000
        assert stats["flush_interval_seconds"] == 45.0

        resilient_recorder.reset_resilient_recorder_settings()
        InMemoryAuditBuffer.reset_instance()


class TestCrossClusterLinkerSettingsIntegration:
    """cross_cluster_linker.py settings 연동 테스트."""

    def test_cross_cluster_linker_uses_settings_defaults(self, monkeypatch):
        """CrossClusterAuditLinker가 AuditIntegritySettings의 기본값을 사용하는지 확인."""
        from baldur.settings import audit_integrity

        audit_integrity.reset_audit_integrity_settings()

        monkeypatch.setenv("BALDUR_AUDIT_INTEGRITY_CROSS_CLUSTER_LOCAL_TTL_DAYS", "60")
        monkeypatch.setenv(
            "BALDUR_AUDIT_INTEGRITY_CROSS_CLUSTER_GLOBAL_TTL_DAYS", "400"
        )

        from baldur.audit.integrity.cross_cluster_linker import (
            CrossClusterAuditLinker,
        )

        linker = CrossClusterAuditLinker()

        assert linker._local_anchor_ttl == 60
        assert linker._global_anchor_ttl == 400

        audit_integrity.reset_audit_integrity_settings()


class TestHealthScoreSettingsIntegration:
    """health_score.py settings 연동 테스트."""

    def test_integrity_health_score_uses_settings_defaults(self, monkeypatch):
        """IntegrityHealthScore가 AuditIntegritySettings의 기본값을 사용하는지 확인."""
        from baldur.settings import audit_integrity

        audit_integrity.reset_audit_integrity_settings()

        monkeypatch.setenv("BALDUR_AUDIT_INTEGRITY_HEALTH_HEALTHY_THRESHOLD", "90.0")
        monkeypatch.setenv("BALDUR_AUDIT_INTEGRITY_HEALTH_WARNING_THRESHOLD", "70.0")
        monkeypatch.setenv("BALDUR_AUDIT_INTEGRITY_HEALTH_CRITICAL_THRESHOLD", "40.0")

        from baldur.audit.integrity.health_score import IntegrityHealthScore

        health = IntegrityHealthScore()

        assert health._healthy_threshold == 90.0
        assert health._warning_threshold == 70.0
        assert health._critical_threshold == 40.0

        audit_integrity.reset_audit_integrity_settings()


class TestConfigSettingsIntegration:
    """config.py settings 연동 테스트."""

    def test_get_recommended_retention_uses_settings_default(self, monkeypatch):
        """get_recommended_retention이 AuditSettings의 기본값을 사용하는지 확인."""
        from baldur.settings import audit as audit_settings

        audit_settings.reset_audit_settings()

        monkeypatch.setenv("BALDUR_AUDIT_COMPLIANCE_MAX_RETENTION_DAYS", "730")

        from baldur.audit.config import get_recommended_retention

        # 규정 없이 호출 - settings 기본값 사용
        result = get_recommended_retention([])

        assert result == 730

        audit_settings.reset_audit_settings()

    def test_get_recommended_retention_with_compliance_standards(self, monkeypatch):
        """get_recommended_retention이 컴플라이언스 표준에 따라 값을 조정하는지 확인."""
        from baldur.settings import audit as audit_settings

        audit_settings.reset_audit_settings()

        monkeypatch.setenv("BALDUR_AUDIT_COMPLIANCE_MAX_RETENTION_DAYS", "365")

        from baldur.audit.config import get_recommended_retention

        # DORA는 5년 (1825일)
        result = get_recommended_retention(["DORA"])

        assert result == 365 * 5  # 1825

        audit_settings.reset_audit_settings()


class TestS3WORMBackendSettingsIntegration:
    """s3_worm.py settings 연동 테스트."""

    # 416 Part 6: S3WORMBackend was a Stage 3 stub in the deleted
    # audit/backends/ package with zero production callers. Tests that
    # exercised it have been removed. When/if a real S3 WORM adapter is
    # introduced under adapters/audit/, add equivalent tests there.


class TestBackwardCompatibility:
    """하위 호환성 테스트 - 레거시 클래스 상수 접근."""

    def test_atomic_merge_swap_legacy_constant_accessible(self):
        """AtomicMergeSwap.DEFAULT_TIMEOUT_SECONDS 레거시 상수 접근 가능."""
        from baldur.audit.hash_chain_safety import AtomicMergeSwap

        assert hasattr(AtomicMergeSwap, "DEFAULT_TIMEOUT_SECONDS")
        assert AtomicMergeSwap.DEFAULT_TIMEOUT_SECONDS == 300

    def test_sharded_date_lock_legacy_constant_accessible(self):
        """ShardedDateLock.DEFAULT_TIMEOUT_SECONDS 레거시 상수 접근 가능."""
        from baldur.audit.hash_chain_safety import ShardedDateLock

        assert hasattr(ShardedDateLock, "DEFAULT_TIMEOUT_SECONDS")
        assert ShardedDateLock.DEFAULT_TIMEOUT_SECONDS == 120

    def test_integrity_audit_trail_legacy_constant_accessible(self):
        """IntegrityAuditTrail.MAX_REDIS_ENTRIES 레거시 상수 접근 가능."""
        from baldur.audit.hash_chain_safety import IntegrityAuditTrail

        assert hasattr(IntegrityAuditTrail, "MAX_REDIS_ENTRIES")
        assert IntegrityAuditTrail.MAX_REDIS_ENTRIES == 1000

    def test_cascade_event_auditor_legacy_constant_accessible(self):
        """CascadeEventAuditor.MAX_INDEX_SIZE 레거시 상수 접근 가능."""
        from baldur.audit.cascade_auditor import CascadeEventAuditor

        assert hasattr(CascadeEventAuditor, "MAX_INDEX_SIZE")
        assert CascadeEventAuditor.MAX_INDEX_SIZE == 10000

    def test_daily_hash_anchor_legacy_constant_accessible(self):
        """DailyHashAnchor.DEFAULT_RETENTION_DAYS 레거시 상수 접근 가능."""
        from baldur.audit.integrity.anchor import DailyHashAnchor

        assert hasattr(DailyHashAnchor, "DEFAULT_RETENTION_DAYS")
        assert DailyHashAnchor.DEFAULT_RETENTION_DAYS == 90

    def test_inmemory_audit_buffer_legacy_constants_accessible(self):
        """InMemoryAuditBuffer 레거시 상수 접근 가능."""
        from baldur.audit.resilience.buffer import InMemoryAuditBuffer

        assert hasattr(InMemoryAuditBuffer, "MAX_ENTRIES")
        assert InMemoryAuditBuffer.MAX_ENTRIES == 10_000
        assert hasattr(InMemoryAuditBuffer, "FLUSH_INTERVAL_SECONDS")
        assert InMemoryAuditBuffer.FLUSH_INTERVAL_SECONDS == 30.0

    def test_cross_cluster_linker_legacy_constants_accessible(self):
        """CrossClusterAuditLinker 레거시 상수 접근 가능."""
        from baldur.audit.integrity.cross_cluster_linker import (
            CrossClusterAuditLinker,
        )

        assert hasattr(CrossClusterAuditLinker, "LOCAL_ANCHOR_TTL_DAYS")
        assert CrossClusterAuditLinker.LOCAL_ANCHOR_TTL_DAYS == 90
        assert hasattr(CrossClusterAuditLinker, "GLOBAL_ANCHOR_TTL_DAYS")
        assert CrossClusterAuditLinker.GLOBAL_ANCHOR_TTL_DAYS == 365

    def test_integrity_health_score_legacy_constants_accessible(self):
        """IntegrityHealthScore 레거시 상수 접근 가능."""
        from baldur.audit.integrity.health_score import IntegrityHealthScore

        assert hasattr(IntegrityHealthScore, "HEALTHY_THRESHOLD")
        assert IntegrityHealthScore.HEALTHY_THRESHOLD == 95.0
        assert hasattr(IntegrityHealthScore, "WARNING_THRESHOLD")
        assert IntegrityHealthScore.WARNING_THRESHOLD == 80.0
        assert hasattr(IntegrityHealthScore, "CRITICAL_THRESHOLD")
        assert IntegrityHealthScore.CRITICAL_THRESHOLD == 50.0
