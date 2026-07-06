"""
Cold Storage 및 Integrity Health Score 테스트.

이 테스트는 다음 기능을 검증합니다:
1. AnchorColdStorage: Redis TTL 만료 전 앵커 Cold Storage 아카이브
   - 법적/금융 감사용 5-7년 장기 보관 지원 (SOC2, HIPAA, PCI-DSS)
   - JSONL + gzip 압축 저장, SHA256 무결성 검증
2. IntegrityHealthScore: 실시간 무결성 건강 지수 모니터링
   - Prometheus 지표 연동, 복구 이벤트 추적
   - Dashboard 요약 API 제공
"""

from __future__ import annotations

import gzip
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ============================================================================
# Cold Storage Tests
# ============================================================================


class TestLocalFileColdStorage:
    """Tests for LocalFileColdStorage backend."""

    def test_write_creates_compressed_archive(self, tmp_path: Path):
        """Write should create gzip-compressed JSONL file."""
        from baldur.audit.integrity.cold_storage import LocalFileColdStorage

        storage = LocalFileColdStorage(base_dir=tmp_path)

        data = json.dumps({"date": "2026-01-15", "hash": "abc123"}).encode("utf-8")
        key = "anchors/2026/01/2026-01-15"

        result = storage.write(key, data)

        assert result is True

        # Verify file exists
        archive_path = (
            tmp_path
            / "cold_storage"
            / "anchors"
            / "2026"
            / "01"
            / "anchors_2026-01.jsonl.gz"
        )
        assert archive_path.exists()

        # Verify content is compressed
        with gzip.open(archive_path, "rb") as f:
            content = f.read()

        assert b"2026-01-15" in content
        assert b"abc123" in content

    def test_write_creates_checksum(self, tmp_path: Path):
        """Write should create SHA256 checksum file."""
        from baldur.audit.integrity.cold_storage import LocalFileColdStorage

        storage = LocalFileColdStorage(base_dir=tmp_path)

        data = b'{"date": "2026-01-15"}'
        key = "anchors/2026/01/2026-01-15"

        storage.write(key, data)

        # Verify checksum file exists
        checksum_path = (
            tmp_path
            / "cold_storage"
            / "anchors"
            / "2026"
            / "01"
            / "anchors_2026-01.jsonl.gz.sha256"
        )
        assert checksum_path.exists()

        # Checksum should be 64 characters (SHA256 hex)
        with open(checksum_path) as f:
            checksum = f.read().strip()

        assert len(checksum) == 64

    def test_verify_integrity_passes(self, tmp_path: Path):
        """Integrity verification should pass for valid archives."""
        from baldur.audit.integrity.cold_storage import LocalFileColdStorage

        storage = LocalFileColdStorage(base_dir=tmp_path)

        data = b'{"date": "2026-01-15"}'
        key = "anchors/2026/01/2026-01-15"
        storage.write(key, data)

        assert storage.verify_integrity(2026, 1) is True

    def test_verify_integrity_fails_on_tamper(self, tmp_path: Path):
        """Integrity verification should fail if archive is tampered."""
        from baldur.audit.integrity.cold_storage import LocalFileColdStorage

        storage = LocalFileColdStorage(base_dir=tmp_path)

        data = b'{"date": "2026-01-15"}'
        key = "anchors/2026/01/2026-01-15"
        storage.write(key, data)

        # Tamper with checksum
        checksum_path = (
            tmp_path
            / "cold_storage"
            / "anchors"
            / "2026"
            / "01"
            / "anchors_2026-01.jsonl.gz.sha256"
        )
        with open(checksum_path, "w") as f:
            f.write("tampered_checksum_value")

        assert storage.verify_integrity(2026, 1) is False

    def test_read_returns_data(self, tmp_path: Path):
        """Read should return decompressed data."""
        from baldur.audit.integrity.cold_storage import LocalFileColdStorage

        storage = LocalFileColdStorage(base_dir=tmp_path)

        data = b'{"date": "2026-01-15"}'
        key = "anchors/2026/01/2026-01-15"
        storage.write(key, data)

        result = storage.read(key)

        assert result is not None
        assert b"2026-01-15" in result

    def test_read_nonexistent_returns_none(self, tmp_path: Path):
        """Read should return None for nonexistent keys."""
        from baldur.audit.integrity.cold_storage import LocalFileColdStorage

        storage = LocalFileColdStorage(base_dir=tmp_path)

        result = storage.read("anchors/2099/12/2099-12-31")

        assert result is None

    def test_list_keys_returns_archives(self, tmp_path: Path):
        """List keys should return all archive files."""
        from baldur.audit.integrity.cold_storage import LocalFileColdStorage

        storage = LocalFileColdStorage(base_dir=tmp_path)

        # Write to multiple months
        storage.write("anchors/2026/01/2026-01-15", b'{"date": "2026-01-15"}')
        storage.write("anchors/2026/02/2026-02-15", b'{"date": "2026-02-15"}')

        keys = storage.list_keys()

        assert len(keys) == 2


class TestAnchorColdStorage:
    """Tests for AnchorColdStorage."""

    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis client."""
        redis = MagicMock()
        redis.scan_iter.return_value = []
        redis.ttl.return_value = -1
        redis.hgetall.return_value = {}
        return redis

    def test_find_expiring_anchors_empty(self, mock_redis, tmp_path: Path):
        """Should return empty list when no expiring anchors."""
        from baldur.audit.integrity.cold_storage import AnchorColdStorage

        storage = AnchorColdStorage(
            redis_client=mock_redis,
            base_dir=tmp_path,
        )

        result = storage.find_expiring_anchors()

        assert result == []

    def test_find_expiring_anchors_detects_near_expiry(
        self, mock_redis, tmp_path: Path
    ):
        """Should detect anchors with TTL < threshold."""
        from baldur.audit.integrity.cold_storage import AnchorColdStorage

        # Mock anchor with 5 days TTL (below 7-day threshold)
        mock_redis.scan_iter.return_value = [
            b"baldur:audit:hash_chain:anchor:2026-01-15"
        ]
        mock_redis.ttl.return_value = 5 * 86400  # 5 days in seconds
        mock_redis.hgetall.return_value = {
            b"date": b"2026-01-15",
            b"sequence": b"1000",
            b"hash": b"abc123",
        }

        storage = AnchorColdStorage(
            redis_client=mock_redis,
            base_dir=tmp_path,
        )

        result = storage.find_expiring_anchors()

        assert len(result) == 1
        assert result[0]["date"] == "2026-01-15"
        assert result[0]["_ttl_days"] == 5.0

    def test_archive_anchor_success(self, mock_redis, tmp_path: Path):
        """Should successfully archive an anchor."""
        from baldur.audit.integrity.cold_storage import AnchorColdStorage

        storage = AnchorColdStorage(
            redis_client=mock_redis,
            base_dir=tmp_path,
        )

        anchor_data = {
            "date": "2026-01-15",
            "sequence": "1000",
            "hash": "abc123",
            "created_at": "2026-01-15T00:00:00Z",
        }

        result = storage.archive_anchor(anchor_data)

        assert result is True

        # Verify file was created
        archive_path = (
            tmp_path
            / "cold_storage"
            / "anchors"
            / "2026"
            / "01"
            / "anchors_2026-01.jsonl.gz"
        )
        assert archive_path.exists()

    def test_archive_expiring_anchors_returns_result(self, mock_redis, tmp_path: Path):
        """Should return ArchiveResult with statistics."""
        from baldur.audit.integrity.cold_storage import AnchorColdStorage

        # Mock two expiring anchors
        mock_redis.scan_iter.return_value = [
            b"baldur:audit:hash_chain:anchor:2026-01-15",
            b"baldur:audit:hash_chain:anchor:2026-01-16",
        ]
        mock_redis.ttl.return_value = 5 * 86400
        mock_redis.hgetall.side_effect = [
            {b"date": b"2026-01-15", b"sequence": b"1000", b"hash": b"abc1"},
            {b"date": b"2026-01-16", b"sequence": b"2000", b"hash": b"abc2"},
        ]

        storage = AnchorColdStorage(
            redis_client=mock_redis,
            base_dir=tmp_path,
        )

        result = storage.archive_expiring_anchors()

        assert result.archived_count == 2
        assert result.failed_count == 0
        assert "2026-01-15" in result.archived_dates
        assert "2026-01-16" in result.archived_dates

    def test_retrieve_archived_anchor(self, mock_redis, tmp_path: Path):
        """Should retrieve archived anchor from cold storage."""
        from baldur.audit.integrity.cold_storage import AnchorColdStorage

        storage = AnchorColdStorage(
            redis_client=mock_redis,
            base_dir=tmp_path,
        )

        # Archive an anchor first
        anchor_data = {
            "date": "2026-01-15",
            "sequence": "1000",
            "hash": "abc123",
            "created_at": "2026-01-15T00:00:00Z",
        }
        storage.archive_anchor(anchor_data)

        # Retrieve it
        retrieved = storage.retrieve_archived_anchor("2026-01-15")

        assert retrieved is not None
        assert retrieved["date"] == "2026-01-15"
        assert retrieved["hash"] == "abc123"

    def test_retrieve_nonexistent_returns_none(self, mock_redis, tmp_path: Path):
        """Should return None for nonexistent archived anchor."""
        from baldur.audit.integrity.cold_storage import AnchorColdStorage

        storage = AnchorColdStorage(
            redis_client=mock_redis,
            base_dir=tmp_path,
        )

        result = storage.retrieve_archived_anchor("2099-12-31")

        assert result is None


# ============================================================================
# Integrity Health Score Tests
# ============================================================================


class TestIntegrityHealthScore:
    """Tests for IntegrityHealthScore."""

    @pytest.fixture
    def health_score(self):
        """Create fresh IntegrityHealthScore instance."""
        from baldur.audit.integrity.health_score import (
            IntegrityHealthScore,
            reset_integrity_health_score,
        )

        reset_integrity_health_score()
        return IntegrityHealthScore()

    def test_initial_health_is_100_percent(self, health_score):
        """Initial health score should be 100% (no issues)."""
        metrics = health_score.get_current_metrics()

        assert metrics.health_score == 100.0
        assert metrics.is_healthy is True

    def test_record_recovery_increments_count(self, health_score):
        """Recording recovery should increment today's count."""
        health_score.record_recovery(
            event_type="reconcile",
            sequences_affected=5,
            recovery_time_ms=150.0,
        )

        metrics = health_score.get_current_metrics()

        assert metrics.recoveries_today == 1
        assert metrics.reconciled_sequences == 5

    def test_record_multiple_recoveries(self, health_score):
        """Multiple recoveries should be counted."""
        health_score.record_recovery("reconcile", 5, 100.0)
        health_score.record_recovery("startup_sync", 3, 200.0)
        health_score.record_recovery("watchdog_cleanup", 2, 50.0)

        metrics = health_score.get_current_metrics()

        assert metrics.recoveries_today == 3
        assert metrics.reconciled_sequences == 10  # 5 + 3 + 2

    def test_avg_recovery_time_calculated(self, health_score):
        """Average recovery time should be calculated correctly."""
        health_score.record_recovery("reconcile", 1, 100.0)
        health_score.record_recovery("reconcile", 1, 200.0)
        health_score.record_recovery("reconcile", 1, 300.0)

        metrics = health_score.get_current_metrics()

        assert metrics.avg_recovery_time_ms == 200.0  # (100 + 200 + 300) / 3
        assert metrics.max_recovery_time_ms == 300.0

    def test_get_health_status_healthy(self, health_score):
        """Status should be HEALTHY when score >= 95."""
        status = health_score.get_health_status()
        assert status == "HEALTHY"

    def test_get_health_status_warning(self, health_score):
        """Status should be WARNING when score is 80-95."""
        # Manually set degraded state
        health_score._cached_metrics = None

        with patch.object(health_score, "_get_chain_state") as mock_state:
            mock_state.return_value = {
                "sequence": 100,
                "degraded_count": 10,  # 10% degraded
                "orphaned_count": 0,
            }

            metrics = health_score.get_current_metrics(force_refresh=True)

        assert metrics.health_score == 90.0
        assert health_score.get_health_status() == "WARNING"

    def test_get_dashboard_summary(self, health_score):
        """Dashboard summary should contain all required fields."""
        health_score.record_recovery("reconcile", 5, 150.0)

        summary = health_score.get_dashboard_summary()

        assert "status" in summary
        assert "health_score" in summary
        assert "summary" in summary
        assert "recovery" in summary
        assert "message" in summary
        assert summary["recovery"]["recoveries_today"] == 1

    def test_status_message_includes_recovery_count(self, health_score):
        """Status message should mention today's recoveries."""
        health_score.record_recovery("reconcile", 5, 150.0)
        health_score.record_recovery("startup_sync", 3, 100.0)

        summary = health_score.get_dashboard_summary()

        assert "2 auto-recoveries" in summary["message"]

    def test_get_recent_events(self, health_score):
        """Should return recent events in reverse chronological order."""
        health_score.record_recovery("event1", 1, 100.0)
        health_score.record_recovery("event2", 2, 200.0)
        health_score.record_recovery("event3", 3, 300.0)

        events = health_score.get_recent_events(limit=2)

        assert len(events) == 2
        assert events[0]["event_type"] == "event3"  # Most recent first
        assert events[1]["event_type"] == "event2"

    def test_record_chain_break_resets_streak(self, health_score):
        """Recording chain break should reset days counter."""
        # Set initial streak
        health_score._days_since_break = 100

        health_score.record_chain_break()

        metrics = health_score.get_current_metrics()
        assert metrics.days_since_last_break == 0

    def test_events_trimmed_to_24h(self, health_score):
        """Old events should be trimmed (only last 24h kept)."""
        from baldur.audit.integrity.health_score import IntegrityRecoveryEvent

        # Add old event (manually, bypassing normal recording)
        old_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        old_event = IntegrityRecoveryEvent(
            event_type="old",
            sequences_affected=1,
            recovery_time_ms=100.0,
            timestamp=old_time,
        )

        health_score._recovery_events.append(old_event)

        # Record new event (triggers trimming)
        health_score.record_recovery("new", 1, 100.0)

        events = health_score.get_recent_events(limit=10)

        # Old event should be trimmed
        assert all(e["event_type"] != "old" for e in events)

    def test_thread_safety(self, health_score):
        """Recording from multiple threads should be thread-safe."""
        errors = []

        def record_events():
            try:
                for i in range(10):
                    health_score.record_recovery(
                        f"thread_{threading.current_thread().name}", 1, float(i)
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_events) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        metrics = health_score.get_current_metrics()
        assert metrics.recoveries_today == 50  # 5 threads * 10 events

    def test_cache_invalidation_on_record(self, health_score):
        """Cache should be invalidated when recording events."""
        # Prime cache
        metrics1 = health_score.get_current_metrics()

        assert metrics1.recoveries_today == 0

        # Record event
        health_score.record_recovery("reconcile", 5, 100.0)

        # Get metrics again
        metrics2 = health_score.get_current_metrics()

        # Should reflect new event (proves cache was invalidated)
        assert metrics2.recoveries_today == 1
        assert metrics2.reconciled_sequences == 5


class TestIntegrityHealthScoreSingleton:
    """Tests for singleton pattern."""

    def test_get_returns_same_instance(self):
        """get_integrity_health_score should return same instance."""
        from baldur.audit.integrity.health_score import (
            get_integrity_health_score,
            reset_integrity_health_score,
        )

        reset_integrity_health_score()

        instance1 = get_integrity_health_score()
        instance2 = get_integrity_health_score()

        assert instance1 is instance2

    def test_reset_clears_singleton(self):
        """reset_integrity_health_score should clear singleton."""
        from baldur.audit.integrity.health_score import (
            get_integrity_health_score,
            reset_integrity_health_score,
        )

        instance1 = get_integrity_health_score()
        reset_integrity_health_score()
        instance2 = get_integrity_health_score()

        assert instance1 is not instance2


class TestIntegrityHealthMetrics:
    """Tests for IntegrityHealthMetrics dataclass."""

    def test_default_values(self):
        """Default values should indicate healthy state."""
        from baldur.audit.integrity.health_score import IntegrityHealthMetrics

        metrics = IntegrityHealthMetrics()

        assert metrics.is_healthy is True
        assert metrics.health_score == 100.0
        assert metrics.recoveries_today == 0


# ============================================================================
# Integration Tests
# ============================================================================


class TestColdStorageIntegration:
    """Integration tests for cold storage with reconciler."""

    def test_reconciler_triggers_health_score_update(self, tmp_path: Path):
        """Reconciler should update health score after recovery."""
        from baldur.audit.integrity.health_score import (
            get_integrity_health_score,
            reset_integrity_health_score,
        )

        reset_integrity_health_score()
        health_score = get_integrity_health_score()

        # Simulate reconciler reporting recovery
        health_score.record_recovery(
            event_type="reconcile",
            sequences_affected=10,
            recovery_time_ms=250.0,
            details={"source": "redis_recovery"},
        )

        summary = health_score.get_dashboard_summary()

        assert summary["recovery"]["recoveries_today"] == 1
        assert summary["recovery"]["sequences_recovered"] == 10
