"""
Metric Reliability System Unit Tests.

메트릭 신뢰성 시스템 테스트:
- SafeGauge 동기화 상태 추적
- L1 스냅샷 저장/복원
- 보수적 폴백 전략
"""

import json
import tempfile
import time
from unittest.mock import MagicMock, patch

# =============================================================================
# SyncInfo Tests
# =============================================================================


class TestSyncInfo:
    """SyncInfo 클래스 테스트."""

    def test_initial_state_is_unknown(self):
        """초기 상태는 UNKNOWN."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        info = SyncInfo()
        assert info.status == SyncStatus.UNKNOWN
        assert info.is_synced is False

    def test_mark_synced_updates_status(self):
        """mark_synced 호출 시 상태 업데이트."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        info = SyncInfo()
        info.mark_synced("push")

        # UNKNOWN → RECOVERING (안정화 기간 시작)
        assert info.status == SyncStatus.RECOVERING
        assert info.last_sync_source == "push"
        assert info.last_sync_time is not None

    def test_staleness_detection(self):
        """staleness 자동 감지 - SYNCED 상태에서만 동작."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        info = SyncInfo(
            staleness_threshold=0.1, stabilization_duration=0.01
        )  # 빠른 안정화
        info.mark_synced("push")

        # UNKNOWN → RECOVERING 전환됨
        assert info.status == SyncStatus.RECOVERING

        # RECOVERING 상태에서는 check_staleness가 False 반환 (정상 동작)
        assert info.check_staleness() is False

        # 안정화 기간 대기 후 다시 sync → SYNCED
        time.sleep(0.02)
        info.mark_synced("push")
        assert info.status == SyncStatus.SYNCED

        # SYNCED 상태에서 시간 경과 후 stale
        time.sleep(0.15)
        assert info.check_staleness() is True
        assert info.status == SyncStatus.STALE

    def test_age_seconds(self):
        """age_seconds 계산."""
        from baldur.metrics.safe_gauge import SyncInfo

        info = SyncInfo()
        assert info.age_seconds is None

        info.mark_synced("test")
        time.sleep(0.05)

        age = info.age_seconds
        assert age is not None
        assert age >= 0.05

    def test_recovery_progress(self):
        """복구 진행률 계산."""
        from baldur.metrics.safe_gauge import SyncInfo, SyncStatus

        info = SyncInfo(stabilization_duration=0.2)  # 0.2초
        info.mark_synced("test")

        assert info.status == SyncStatus.RECOVERING
        assert info.recovery_progress >= 0.0

        time.sleep(0.25)
        assert info.recovery_progress >= 1.0


class TestSafeGaugeChildSyncStatus:
    """SafeGaugeChild 동기화 상태 테스트."""

    def _create_mock_gauge_child(self):
        """Mock Prometheus Gauge child 생성."""
        mock = MagicMock()
        mock._value = MagicMock()
        mock._value.get.return_value = 0
        return mock

    def test_inc_updates_sync_status(self):
        """inc() 호출 시 동기화 상태 업데이트."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = self._create_mock_gauge_child()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        assert safe.is_synced is False

        safe.inc()

        # 첫 동기화 후 복구 중 상태
        assert safe.last_sync_time is not None

    def test_dec_updates_sync_status(self):
        """dec() 호출 시 동기화 상태 업데이트."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = self._create_mock_gauge_child()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        safe.inc()  # 먼저 초기화
        safe.dec()

        assert safe.last_sync_time is not None

    def test_set_updates_sync_status_with_source(self):
        """set() 호출 시 소스 정보 포함."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = self._create_mock_gauge_child()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        safe.set(10, source="hydration")

        assert safe.sync_info.last_sync_source == "hydration"

    def test_get_reliability_info(self):
        """신뢰도 정보 조회."""
        from baldur.metrics.safe_gauge import SafeGaugeChild

        mock_child = self._create_mock_gauge_child()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        safe.inc()
        info = safe.get_reliability_info()

        assert "is_synced" in info
        assert "status" in info
        assert "last_sync_time" in info
        assert "shadow_value" in info
        assert info["labels"] == {"domain": "payment"}

    def test_mark_stale(self):
        """수동 stale 마킹."""
        from baldur.metrics.safe_gauge import SafeGaugeChild, SyncStatus

        mock_child = self._create_mock_gauge_child()
        safe = SafeGaugeChild(mock_child, {"domain": "payment"})

        safe.inc()
        safe.mark_stale("test_reason")

        assert safe.sync_info.status == SyncStatus.STALE


# =============================================================================
# Snapshot Storage Tests
# =============================================================================


class TestMetricSnapshotStorage:
    """MetricSnapshotStorage 테스트."""

    def setup_method(self):
        """테스트 전 임시 디렉토리 생성."""
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        """테스트 후 정리."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_load_value(self):
        """값 저장 및 로드."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(self.temp_dir)

        storage.save_value("dlq_pending", "payment", 5, immediate=True)
        value = storage.load_value("dlq_pending", "payment")

        assert value == 5

    def test_save_bulk(self):
        """일괄 저장."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(self.temp_dir)

        values = {
            "dlq_pending": {"payment": 5, "point": 3},
            "circuit_breaker": {"toss": "closed"},
        }
        storage.save_bulk(values, source="bulk_test")

        assert storage.load_value("dlq_pending", "payment") == 5
        assert storage.load_value("dlq_pending", "point") == 3
        assert storage.load_value("circuit_breaker", "toss") == "closed"

    def test_atomic_write(self):
        """원자적 쓰기 (임시 파일 → rename)."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(self.temp_dir)

        # 여러 번 저장
        for i in range(5):
            storage.save_value("counter", "test", i, immediate=True)

        # 파일이 정상적으로 존재
        assert storage.file_path.exists()

        # 내용 확인
        with open(storage.file_path) as f:
            data = json.load(f)

        assert data["values"]["counter"]["test"] == 4

    def test_age_tracking(self):
        """스냅샷 나이 추적."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(self.temp_dir)
        storage.save_value("test", "key", "value", immediate=True)

        age = storage.get_snapshot_age()
        assert age is not None
        assert age < 1.0  # 1초 이내

    def test_max_age_enforcement(self):
        """최대 나이 초과 시 기본값 반환."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(self.temp_dir, max_age_seconds=0.2)
        storage.save_value("test", "key", "value", immediate=True)

        # 즉시 로드 - 성공
        assert storage.load_value("test", "key") == "value"

        # 시간 경과 후 - 기본값
        time.sleep(0.3)
        assert storage.load_value("test", "key", default="fallback") == "fallback"

    def test_persistence_across_instances(self):
        """인스턴스 재생성 시에도 유지."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage1 = MetricSnapshotStorage(self.temp_dir)
        storage1.save_value("persist", "test", "persistent_value", immediate=True)

        # 새 인스턴스 생성
        storage2 = MetricSnapshotStorage(self.temp_dir)

        assert storage2.load_value("persist", "test") == "persistent_value"

    def test_get_snapshot_info(self):
        """스냅샷 정보 조회."""
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        storage = MetricSnapshotStorage(self.temp_dir)
        storage.save_value("test", "key", "value", immediate=True)

        info = storage.get_snapshot_info()

        assert info["exists"] is True
        assert "age_seconds" in info
        assert "categories" in info
        assert "test" in info["categories"]


# =============================================================================
# Reliability Manager Tests
# =============================================================================


class TestMetricReliabilityManager:
    """MetricReliabilityManager 테스트."""

    def test_initial_state_is_strict(self):
        """초기 상태는 STRICT 모드."""
        from baldur.metrics.reliability_manager import (
            MetricReliabilityManager,
            OperatingMode,
            ReliabilityLevel,
        )

        manager = MetricReliabilityManager()
        state = manager.get_reliability_state("payment")

        assert state.operating_mode == OperatingMode.STRICT
        assert state.reliability_level == ReliabilityLevel.UNKNOWN

    def test_sync_success_improves_reliability(self):
        """동기화 성공 시 신뢰도 향상."""
        from baldur.metrics.reliability_manager import (
            MetricReliabilityManager,
            ReliabilityLevel,
        )

        manager = MetricReliabilityManager()
        state = manager.report_sync_success("payment", "push", value=5)

        assert state.reliability_level == ReliabilityLevel.HIGH
        assert state.current_value == 5

    def test_sync_failure_degrades_reliability(self):
        """동기화 실패 시 연속 카운터 리셋."""
        from baldur.metrics.reliability_manager import MetricReliabilityManager

        manager = MetricReliabilityManager()

        # 성공
        manager.report_sync_success("payment", "push", 5)
        manager.report_sync_success("payment", "push", 6)

        state = manager.get_reliability_state("payment")
        assert state.consecutive_successful_syncs == 2

        # 실패
        state = manager.report_sync_failure("payment", "push", "timeout")
        assert state.consecutive_successful_syncs == 0

    def test_gradual_stabilization(self):
        """점진적 안정화 (STRICT → CAUTIOUS → NORMAL)."""
        from baldur.metrics.reliability_manager import (
            MetricReliabilityManager,
            OperatingMode,
            ReliabilityThresholds,
        )

        # 짧은 안정화 기간으로 테스트
        thresholds = ReliabilityThresholds(
            stabilization_duration=0.1,
            consecutive_syncs_for_normal=2,
        )
        manager = MetricReliabilityManager(thresholds=thresholds)

        # 초기: STRICT
        state = manager.get_reliability_state("payment")
        assert state.operating_mode == OperatingMode.STRICT

        # 첫 동기화: STRICT → CAUTIOUS
        manager.report_sync_success("payment", "push", 1)
        state = manager.get_reliability_state("payment")
        assert state.operating_mode == OperatingMode.CAUTIOUS

        # 안정화 기간 대기 + 추가 동기화
        time.sleep(0.15)
        manager.report_sync_success("payment", "push", 2)
        manager.report_sync_success("payment", "push", 3)

        state = manager.get_reliability_state("payment")
        assert state.operating_mode == OperatingMode.NORMAL

    def test_mode_listener(self):
        """모드 변경 리스너."""
        from baldur.metrics.reliability_manager import MetricReliabilityManager

        manager = MetricReliabilityManager()

        changes = []
        manager.register_mode_listener(
            lambda domain, mode: changes.append((domain, mode))
        )

        manager.report_sync_success("payment", "push", 5)

        assert len(changes) > 0
        assert changes[0][0] == "payment"

    def test_force_strict_mode(self):
        """강제 엄격 모드."""
        from baldur.metrics.reliability_manager import (
            MetricReliabilityManager,
            OperatingMode,
        )

        manager = MetricReliabilityManager()

        # 정상화
        manager.report_sync_success("payment", "push", 5)

        # 강제 엄격 모드
        manager.force_strict_mode("payment", "manual_test")

        state = manager.get_reliability_state("payment")
        assert state.operating_mode == OperatingMode.STRICT
        assert state.consecutive_successful_syncs == 0

    def test_get_global_health(self):
        """전체 건강 상태."""
        from baldur.metrics.reliability_manager import MetricReliabilityManager

        manager = MetricReliabilityManager()

        manager.report_sync_success("payment", "push", 5)
        manager.report_sync_success("point", "push", 3)

        health = manager.get_global_health()

        assert health["status"] in ("healthy", "degraded", "unhealthy")
        assert health["domains"] == 2

    def test_get_effective_value_with_safe_defaults(self):
        """효과적인 값 (Safe Defaults 포함)."""
        from baldur.metrics.reliability_manager import (
            MetricReliabilityManager,
            ReliabilityLevel,
        )

        def safe_defaults(domain):
            return {"payment": 0, "point": 0}.get(domain, 0)

        manager = MetricReliabilityManager(safe_defaults_provider=safe_defaults)

        # 데이터 없음 → Safe Default
        value, source, level = manager.get_effective_value("payment")
        assert source == "default"
        assert level == ReliabilityLevel.UNKNOWN


# =============================================================================
# Integration Tests
# =============================================================================


class TestMetricReliabilityIntegration:
    """신뢰도 시스템 통합 테스트."""

    def setup_method(self):
        """테스트 전 설정."""
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        """테스트 후 정리."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fallback_hierarchy(self):
        """Fallback 계층 테스트: Push → Snapshot → Default."""
        from baldur.metrics.reliability_manager import (
            MetricReliabilityManager,
            ReliabilityLevel,
        )
        from baldur.metrics.snapshot_storage import MetricSnapshotStorage

        # 스냅샷 준비
        storage = MetricSnapshotStorage(self.temp_dir)
        storage.save_value("default", "payment", 10, immediate=True)

        def safe_defaults(domain):
            return 0

        manager = MetricReliabilityManager(safe_defaults_provider=safe_defaults)

        # 1. 데이터 없음 → Default
        with patch("baldur.metrics.snapshot_storage.get_snapshot_storage") as mock:
            mock.return_value.load_value.return_value = None
            mock.return_value.get_snapshot_age.return_value = None

            value, source, level = manager.get_effective_value("unknown_domain")
            assert source == "default"

        # 2. 동기화 성공 → Push 값 사용
        manager.report_sync_success("payment", "push", 5)
        value, source, level = manager.get_effective_value("payment")
        assert value == 5
        assert source == "push"
        assert level == ReliabilityLevel.HIGH

    def test_conservative_mode_on_data_loss(self):
        """데이터 유실 시 보수적 모드 전환."""
        from baldur.metrics.reliability_manager import (
            MetricReliabilityManager,
            ReliabilityLevel,
            ReliabilityThresholds,
        )

        thresholds = ReliabilityThresholds(
            high_max_age=0.1,
            medium_max_age=0.2,
        )
        manager = MetricReliabilityManager(thresholds=thresholds)

        # 동기화 성공
        manager.report_sync_success("payment", "push", 5)

        # 시간 경과 (데이터 stale) → medium_max_age(0.2) 초과 → LOW
        time.sleep(0.25)

        state = manager.get_reliability_state("payment")
        assert state.reliability_level == ReliabilityLevel.LOW
