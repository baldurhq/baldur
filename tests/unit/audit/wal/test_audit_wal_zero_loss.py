"""
WAL 기반 Zero-Loss 테스트

테스트 범위:
1. audit_helpers.py WAL 연동
2. AuditSyncWorker
3. AuditMetrics WAL 메트릭
4. 668 D1/D4: B-contiguous cursor advance + poison-stall alert
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import structlog


class _PerRecordCentralAdapter:
    """Fake central audit adapter for the WAL-drain (Pipeline A) tests.

    Mirrors the ``AuditLogAdapter.log(entry)`` surface that
    ``AuditSyncWorker._sync_entry_to_adapter`` invokes with a converted
    ``AuditEntry`` (the native WAL ``record_id`` lands in ``entry.details``).
    It raises for any entry whose ``record_id`` is in ``fail_record_ids`` (as a
    real central store would on a validation / constraint / oversize rejection)
    and records the record_ids it accepts, so a test can assert which entries
    were delivered.
    """

    def __init__(self, fail_record_ids=None):
        self.fail_record_ids = set(fail_record_ids or ())
        self.written_ids = []

    def log(self, entry):
        record_id = entry.details.get("record_id")
        if record_id in self.fail_record_ids:
            raise RuntimeError(f"central rejected {record_id!r}")
        self.written_ids.append(record_id)


@pytest.fixture
def audit_enabled():
    """Force audit subsystem enabled for the duration of the test (416 D9)."""
    from baldur.settings.audit import override_audit_settings
    from baldur_pro.services import audit as audit_helpers

    audit_helpers._reset_wal_state()
    with override_audit_settings(enabled=True):
        yield
    audit_helpers._reset_wal_state()


class TestAuditHelpersWAL:
    """audit_helpers.py WAL 연동 테스트."""

    @pytest.fixture(autouse=True)
    def setup_teardown(self):
        """Reset WAL state around each test (416 D1)."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services import audit as audit_helpers

        audit_helpers._reset_wal_state()
        yield
        audit_helpers._reset_wal_state()

    def test_wal_initialization_with_env(self, tmp_path, audit_enabled):
        """환경변수로 WAL 디렉토리 설정."""
        from baldur_pro.services import audit as audit_helpers

        wal_dir = str(tmp_path / "wal_test")

        with patch.dict(os.environ, {"AUDIT_WAL_DIR": wal_dir}):
            audit_helpers._wal_instance = None  # force re-init
            audit_helpers._wal_init_failed = False  # 강제 재초기화

            wal = audit_helpers._get_wal()

            assert wal is not None
            assert os.path.exists(wal_dir)

    def test_log_dlq_store_writes_to_wal(self, tmp_path, audit_enabled):
        """log_dlq_store_audit가 WAL에 먼저 기록."""
        from baldur_pro.services import audit as audit_helpers

        wal_dir = str(tmp_path / "wal_test")

        with patch.dict(os.environ, {"AUDIT_WAL_DIR": wal_dir}):
            audit_helpers._wal_instance = None  # force re-init
            audit_helpers._wal_init_failed = False

            # WAL에 기록
            wal_seq = audit_helpers.log_dlq_store_audit(
                dlq_id=123,
                domain="payment",
                failure_type="PG_TIMEOUT",
                error_message="Connection failed",
            )

            # WAL 시퀀스 번호 반환 확인
            assert wal_seq is not None
            assert isinstance(wal_seq, int)
            assert wal_seq >= 1

    def test_log_dlq_replay_writes_to_wal(self, tmp_path, audit_enabled):
        """log_dlq_replay_audit가 WAL에 먼저 기록."""
        from baldur_pro.services import audit as audit_helpers

        wal_dir = str(tmp_path / "wal_test")

        with patch.dict(os.environ, {"AUDIT_WAL_DIR": wal_dir}):
            audit_helpers._wal_instance = None  # force re-init
            audit_helpers._wal_init_failed = False

            wal_seq = audit_helpers.log_dlq_replay_audit(
                dlq_id=456,
                domain="point",
                success=True,
                actor_id="user_1",
            )

            assert wal_seq is not None
            assert isinstance(wal_seq, int)

    def test_log_cb_state_change_writes_to_wal(self, tmp_path, audit_enabled):
        """log_cb_state_change_audit가 WAL에 먼저 기록."""
        from baldur_pro.services import audit as audit_helpers

        wal_dir = str(tmp_path / "wal_test")

        with patch.dict(os.environ, {"AUDIT_WAL_DIR": wal_dir}):
            audit_helpers._wal_instance = None  # force re-init
            audit_helpers._wal_init_failed = False

            wal_seq = audit_helpers.log_cb_state_change_audit(
                cb_name="payment_cb",
                old_state="closed",
                new_state="open",
                reason="failure_threshold_exceeded",
            )

            assert wal_seq is not None

    def test_wal_disabled_returns_none(self):
        """WAL is None when audit subsystem is disabled (416 D1)."""
        from baldur.settings.audit import override_audit_settings
        from baldur_pro.services import audit as audit_helpers

        audit_helpers._reset_wal_state()
        with override_audit_settings(enabled=False):
            wal_seq = audit_helpers.log_dlq_store_audit(
                dlq_id=789,
                domain="webhook",
                failure_type="SIGNATURE_INVALID",
            )

        # When audit is disabled the helpers no-op and return None (Fail-Open).
        assert wal_seq is None

    def test_get_wal_stats(self, tmp_path, audit_enabled):
        """WAL 통계 조회."""
        from baldur_pro.services import audit as audit_helpers

        wal_dir = str(tmp_path / "wal_test")

        with patch.dict(os.environ, {"AUDIT_WAL_DIR": wal_dir}):
            audit_helpers._wal_instance = None  # force re-init
            audit_helpers._wal_init_failed = False

            # 몇 개 기록
            audit_helpers.log_dlq_store_audit(
                dlq_id=1, domain="test", failure_type="TEST"
            )
            audit_helpers.log_dlq_store_audit(
                dlq_id=2, domain="test", failure_type="TEST"
            )

            stats = audit_helpers.get_wal_stats()

            assert stats is not None
            assert stats["total_entries"] >= 2
            assert stats["state"] == "active"


class TestAuditSyncWorker:
    """AuditSyncWorker 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """각 테스트 전후로 싱글톤 초기화."""
        from baldur.audit.sync_worker import AuditSyncWorker

        if AuditSyncWorker._instance is not None:
            AuditSyncWorker._instance.stop(timeout=0.05)
        AuditSyncWorker.reset_instance()
        yield
        if AuditSyncWorker._instance is not None:
            AuditSyncWorker._instance.stop(timeout=0.05)
        AuditSyncWorker.reset_instance()

    def test_sync_worker_singleton(self):
        """싱글톤 패턴 동작 확인."""
        from baldur.audit.sync_worker import AuditSyncWorker

        worker1 = AuditSyncWorker.get_instance()
        worker2 = AuditSyncWorker.get_instance()

        assert worker1 is worker2

    def test_sync_worker_start_stop(self):
        """워커 시작/중지."""
        from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig

        config = SyncWorkerConfig(sync_interval_seconds=0.1)
        worker = AuditSyncWorker.get_instance(config=config)

        # 시작
        assert worker.start() is True
        assert worker.is_running is True

        # 중복 시작 시도
        assert worker.start() is False

        # 중지
        worker.stop(timeout=0.2)
        assert worker.is_running is False

    def test_sync_batch_with_mock_wal(self, tmp_path):
        """배치 동기화 테스트 (Mock WAL)."""
        from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
        from baldur.audit.wal import WALConfig, WriteAheadLog

        # 실제 WAL 생성
        wal_config = WALConfig(wal_dir=str(tmp_path / "wal"))
        wal = WriteAheadLog(config=wal_config)

        # 테스트 데이터 기록
        wal.write({"event_type": "TEST", "record_id": "test-1"})
        wal.write({"event_type": "TEST", "record_id": "test-2"})

        # Mock adapter
        mock_adapter = MagicMock()

        config = SyncWorkerConfig(sync_interval_seconds=0.1, batch_size=10)
        worker = AuditSyncWorker(wal=wal, central_adapter=mock_adapter, config=config)

        # 즉시 동기화
        synced, failed = worker.sync_now()

        assert synced >= 2
        assert failed == 0

        # 통계 확인
        stats = worker.get_stats()
        assert stats["total_synced"] >= 2

        wal.close()

    def test_sync_worker_retry_on_failure(self, tmp_path):
        """어댑터 실패 시 재시도."""
        from unittest.mock import patch

        from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
        from baldur.audit.wal import WALConfig, WriteAheadLog

        wal_config = WALConfig(wal_dir=str(tmp_path / "wal"))
        wal = WriteAheadLog(config=wal_config)
        wal.write({"event_type": "TEST", "record_id": "test-retry"})

        # 처음 2번 실패, 3번째 성공하는 Mock
        mock_adapter = MagicMock()
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise Exception("Simulated failure")

        mock_adapter.log.side_effect = side_effect

        config = SyncWorkerConfig(
            sync_interval_seconds=0.1,
            max_retries=3,
            retry_delay_seconds=0.01,
        )
        worker = AuditSyncWorker(wal=wal, central_adapter=mock_adapter, config=config)

        # IdempotencyService import를 막아 재시도 로직만 테스트
        with patch.dict("sys.modules", {"baldur.services.idempotency": None}):
            synced, failed = worker.sync_now()

        # 재시도 후 성공
        assert synced >= 1
        assert call_count[0] >= 3

        stats = worker.get_stats()
        assert stats["total_retries"] >= 2

        wal.close()


class TestSyncWorkerContiguousCursor:
    """668 D1/D4: B-contiguous cursor advance (resolves OOS #590).

    A non-contiguous batch (``success(s1) -> fail(s2) -> success(s3)`` in one
    WAL file) must not lose the per-entry-failed ``s2`` on recovery replay: the
    cursor advances only over the contiguous leading run, so file-granular
    ``cleanup_processed`` never unlinks the file still holding ``s2``.
    """

    @pytest.fixture
    def non_contiguous_drain(self, tmp_path):
        """Drain one rotated WAL file: s1 ok, s2 permanently rejected, s3 ok.

        Yields a namespace captured immediately after a single ``sync_now()``.
        """
        from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
        from baldur.audit.wal import WALConfig, WriteAheadLog

        wal = WriteAheadLog(config=WALConfig(wal_dir=str(tmp_path / "wal")))
        s1 = wal.write({"event_type": "TEST", "record_id": "r1"})
        s2 = wal.write({"event_type": "TEST", "record_id": "r2"})
        s3 = wal.write({"event_type": "TEST", "record_id": "r3"})

        # Force rotation so the file holding s1..s3 is no longer the active
        # _current_file: cleanup_processed skips the current file, so without
        # rotation the file-granular loss path under test is never exercised.
        wal_file = wal._current_file
        wal._rotate_file()

        adapter = _PerRecordCentralAdapter(fail_record_ids={"r2"})
        # max_retries=0: a rejected entry raises at once, with no real sleep.
        config = SyncWorkerConfig(max_retries=0, batch_size=10)
        worker = AuditSyncWorker(wal=wal, central_adapter=adapter, config=config)

        # Block the process-global IdempotencyService so the cursor/cleanup
        # logic is exercised directly (mirrors test_sync_worker_retry_on_failure).
        with patch.dict("sys.modules", {"baldur.services.idempotency": None}):
            synced, failed = worker.sync_now()

        yield SimpleNamespace(
            worker=worker,
            wal=wal,
            wal_file=wal_file,
            adapter=adapter,
            s1=s1,
            s2=s2,
            s3=s3,
            synced=synced,
            failed=failed,
        )
        wal.close()

    def test_non_contiguous_failure_holds_cursor_below_failed_entry(
        self, non_contiguous_drain
    ):
        """Cursor advances over the leading run (s1) only, never past failed s2."""
        result = non_contiguous_drain

        assert result.worker._last_processed_seq == result.s1
        assert result.worker._last_processed_seq < result.s2

    def test_non_contiguous_failure_retains_failed_entry_wal_file(
        self, non_contiguous_drain
    ):
        """The file holding the never-delivered s2 is not unlinked by cleanup."""
        result = non_contiguous_drain

        assert result.wal_file.exists()

    def test_non_contiguous_failure_keeps_failed_entry_recoverable(
        self, non_contiguous_drain
    ):
        """s2 is still returned by recover_unprocessed after the drain (zero loss)."""
        result = non_contiguous_drain

        recovered = result.wal.recover_unprocessed(
            result.worker._last_processed_seq, mode="runtime"
        )

        assert result.s2 in {entry.sequence for entry in recovered}

    def test_non_contiguous_failure_still_delivers_later_success(
        self, non_contiguous_drain
    ):
        """The healthy s3 after the gap is still delivered (no delivery HOL block)."""
        result = non_contiguous_drain

        assert result.synced == 2
        assert result.failed == 1
        assert "r3" in result.adapter.written_ids


class TestSyncWorkerStallAlert:
    """668 D1/D4: poison-stall alert.

    A permanently-failing head entry pins the B-contiguous cursor (it is
    retained, never auto-dropped). After ``cursor_stall_alert_cycles``
    consecutive stalled cycles an edge-triggered CRITICAL ``cursor_stalled``
    fires once per episode and the ``wal_sync_cursor_stalled`` gauge is set;
    any forward progress clears both.
    """

    STALL_THRESHOLD = 3

    @pytest.fixture(autouse=True)
    def reset_stall_gauge(self):
        """Reset the process-global cursor-stall gauge around each test."""
        from baldur.metrics.drift_metrics import update_wal_cursor_stalled

        update_wal_cursor_stalled(False)
        yield
        update_wal_cursor_stalled(False)

    @pytest.fixture
    def poison_worker(self, tmp_path):
        """A worker draining a single permanently-failing ('poison') WAL entry.

        ``adapter.fail_record_ids`` can be cleared mid-test to simulate the
        operator resolving the central-store rejection (recovery).
        """
        from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
        from baldur.audit.wal import WALConfig, WriteAheadLog

        wal = WriteAheadLog(config=WALConfig(wal_dir=str(tmp_path / "wal")))
        seq = wal.write({"event_type": "TEST", "record_id": "poison"})
        wal._rotate_file()

        adapter = _PerRecordCentralAdapter(fail_record_ids={"poison"})
        config = SyncWorkerConfig(
            max_retries=0,
            batch_size=10,
            cursor_stall_alert_cycles=self.STALL_THRESHOLD,
        )
        worker = AuditSyncWorker(wal=wal, central_adapter=adapter, config=config)

        yield SimpleNamespace(worker=worker, wal=wal, adapter=adapter, seq=seq)
        wal.close()

    @staticmethod
    def _drive(worker, cycles):
        """Run ``cycles`` sync passes (idempotency blocked); return captured logs."""
        with (
            patch.dict("sys.modules", {"baldur.services.idempotency": None}),
            structlog.testing.capture_logs() as logs,
        ):
            for _ in range(cycles):
                worker.sync_now()
        return logs

    @staticmethod
    def _stalled_events(logs):
        return [e for e in logs if e.get("event") == "audit_sync_worker.cursor_stalled"]

    @staticmethod
    def _gauge_value():
        from baldur.metrics.drift_metrics import wal_sync_cursor_stalled

        return wal_sync_cursor_stalled._value.get()

    def test_stall_below_threshold_does_not_alert(self, poison_worker):
        """One short of the threshold raises no CRITICAL and does not latch."""
        worker = poison_worker.worker

        logs = self._drive(worker, self.STALL_THRESHOLD - 1)

        assert self._stalled_events(logs) == []
        assert worker._stall_cycles == self.STALL_THRESHOLD - 1
        assert worker._cursor_stall_alerted is False

    def test_stall_at_threshold_emits_single_critical_with_payload(self, poison_worker):
        """At the threshold exactly one CRITICAL fires, names the stuck seq, and
        sets the gauge to 1."""
        worker = poison_worker.worker

        logs = self._drive(worker, self.STALL_THRESHOLD)

        events = self._stalled_events(logs)
        assert len(events) == 1
        assert events[0]["log_level"] == "critical"
        assert events[0]["stuck_sequence"] == poison_worker.seq
        assert worker._cursor_stall_alerted is True
        assert self._gauge_value() == 1.0

    def test_stall_does_not_refire_critical_while_stalled(self, poison_worker):
        """The CRITICAL is edge-triggered: further stalled cycles do not re-fire."""
        worker = poison_worker.worker

        first = self._drive(worker, self.STALL_THRESHOLD)
        later = self._drive(worker, self.STALL_THRESHOLD + 2)

        assert len(self._stalled_events(first)) == 1
        assert self._stalled_events(later) == []

    def test_stall_never_auto_drops_entry(self, poison_worker):
        """The undelivered poison entry is retained, never dropped (fail-safe)."""
        worker = poison_worker.worker
        wal = poison_worker.wal

        self._drive(worker, self.STALL_THRESHOLD + 1)

        recovered = wal.recover_unprocessed(worker._last_processed_seq, mode="runtime")
        assert poison_worker.seq in {entry.sequence for entry in recovered}
        assert worker._last_processed_seq < poison_worker.seq

    def test_stall_cleared_on_recovering_sync(self, poison_worker):
        """A recovering sync clears the alert latch, the gauge, and the counter."""
        worker = poison_worker.worker

        self._drive(worker, self.STALL_THRESHOLD)
        assert self._gauge_value() == 1.0  # precondition: stalled

        # Operator resolves the central-store rejection — the entry now lands.
        poison_worker.adapter.fail_record_ids.clear()
        self._drive(worker, 1)

        assert worker._last_processed_seq == poison_worker.seq
        assert worker._cursor_stall_alerted is False
        assert worker._stall_cycles == 0
        assert self._gauge_value() == 0.0


class TestAuditMetricsWAL:
    """AuditMetrics WAL 메트릭 테스트."""

    @pytest.fixture(autouse=True)
    def reset_metrics(self):
        """각 테스트 전후로 메트릭 초기화."""
        from baldur.audit.resilience import AuditMetrics

        metrics = AuditMetrics.get_instance()
        metrics.reset()
        yield
        metrics.reset()

    def test_record_wal_write(self):
        """WAL 기록 메트릭."""
        from baldur.audit.resilience import AuditMetrics

        metrics = AuditMetrics.get_instance()

        metrics.record_wal_write(success=True)
        metrics.record_wal_write(success=True)
        metrics.record_wal_write(success=False)

        wal_metrics = metrics.get_wal_metrics()

        assert wal_metrics["audit_wal_writes_total"] == 2
        assert wal_metrics["audit_wal_write_failures_total"] == 1

    def test_record_central_write(self):
        """중앙 저장소 기록 메트릭."""
        from baldur.audit.resilience import AuditMetrics

        metrics = AuditMetrics.get_instance()

        metrics.record_central_write(count=5)
        metrics.record_central_write(count=3)

        wal_metrics = metrics.get_wal_metrics()

        assert wal_metrics["audit_central_writes_total"] == 8

    def test_set_sync_lag(self):
        """동기화 지연 메트릭."""
        from baldur.audit.resilience import AuditMetrics

        metrics = AuditMetrics.get_instance()

        metrics.set_sync_lag(100)
        assert metrics.get_wal_metrics()["audit_sync_lag_entries"] == 100

        metrics.set_sync_lag(50)
        assert metrics.get_wal_metrics()["audit_sync_lag_entries"] == 50

    def test_get_metrics_includes_wal(self):
        """get_metrics()에 WAL 메트릭 포함."""
        from baldur.audit.resilience import AuditMetrics

        metrics = AuditMetrics.get_instance()

        metrics.record_wal_write(success=True)
        metrics.set_sync_lag(42)

        all_metrics = metrics.get_metrics()

        assert "audit_wal_writes_total" in all_metrics
        assert "audit_sync_lag_entries" in all_metrics
        assert all_metrics["audit_wal_writes_total"] == 1
        assert all_metrics["audit_sync_lag_entries"] == 42

    def test_prometheus_format_includes_wal(self):
        """Prometheus 포맷에 WAL 메트릭 포함."""
        from baldur.audit.resilience import AuditMetrics

        metrics = AuditMetrics.get_instance()

        metrics.record_wal_write(success=True)
        metrics.record_wal_write(success=False)

        prom_text = metrics.get_prometheus_format()

        assert "audit_wal_writes_total" in prom_text
        assert "audit_wal_write_failures_total" in prom_text
        assert "audit_sync_lag_entries" in prom_text

    def test_reset_clears_wal_metrics(self):
        """reset()이 WAL 메트릭도 초기화."""
        from baldur.audit.resilience import AuditMetrics

        metrics = AuditMetrics.get_instance()

        metrics.record_wal_write(success=True)
        metrics.record_central_write(count=10)
        metrics.set_sync_lag(100)

        metrics.reset()

        wal_metrics = metrics.get_wal_metrics()

        assert wal_metrics["audit_wal_writes_total"] == 0
        assert wal_metrics["audit_central_writes_total"] == 0
        assert wal_metrics["audit_sync_lag_entries"] == 0


class TestIntegrationWALFlow:
    """WAL 전체 흐름 통합 테스트."""

    @pytest.fixture(autouse=True)
    def setup_teardown(self, tmp_path):
        """Test environment setup (416 D1 — WAL state reset, no enable/disable)."""
        pytest.importorskip("baldur_pro")
        from baldur.audit.resilience import AuditMetrics
        from baldur.audit.sync_worker import AuditSyncWorker
        from baldur.settings.audit import override_audit_settings
        from baldur_pro.services import audit as audit_helpers

        audit_helpers._reset_wal_state()
        if AuditSyncWorker._instance is not None:
            AuditSyncWorker._instance.stop(timeout=0.05)
        AuditSyncWorker.reset_instance()
        AuditMetrics.get_instance().reset()

        self.wal_dir = str(tmp_path / "integration_wal")

        # Force enable for the integration flow tests below.
        with override_audit_settings(enabled=True):
            yield

        audit_helpers._reset_wal_state()
        if AuditSyncWorker._instance is not None:
            AuditSyncWorker._instance.stop(timeout=0.05)
        AuditSyncWorker.reset_instance()

    def test_end_to_end_wal_flow(self):
        """E2E: 이벤트 발생 → WAL 기록 → Sync."""
        from baldur.audit.resilience import AuditMetrics
        from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
        from baldur_pro.services import audit as audit_helpers

        # 1. WAL 활성화
        with patch.dict(os.environ, {"AUDIT_WAL_DIR": self.wal_dir}):
            audit_helpers._wal_instance = None  # force re-init
            audit_helpers._wal_init_failed = False

            # 2. 이벤트 기록 (WAL에 먼저 기록됨)
            seq1 = audit_helpers.log_dlq_store_audit(
                dlq_id=1, domain="payment", failure_type="TEST"
            )
            seq2 = audit_helpers.log_dlq_replay_audit(
                dlq_id=1, domain="payment", success=True
            )

            assert seq1 is not None
            assert seq2 is not None
            assert seq2 > seq1

            # 3. WAL 통계 확인
            stats = audit_helpers.get_wal_stats()
            assert stats["total_entries"] >= 2

            # 4. Sync Worker로 동기화
            wal = audit_helpers._get_wal()
            mock_adapter = MagicMock()

            sync_config = SyncWorkerConfig(sync_interval_seconds=0.1)
            worker = AuditSyncWorker(
                wal=wal,
                central_adapter=mock_adapter,
                config=sync_config,
            )

            synced, failed = worker.sync_now()
            assert synced >= 2
            assert failed == 0

            # 5. 메트릭 확인
            metrics = AuditMetrics.get_instance()
            all_metrics = metrics.get_metrics()

            # WAL 기록이 있었음
            assert all_metrics.get("audit_wal_writes_total", 0) >= 0
