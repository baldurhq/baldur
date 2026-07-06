"""Unit tests for multi-worker audit-WAL drain correctness (#588).

#588 eliminates the multi-worker audit-WAL drain data-loss path by:

- **D1** — ``AuditSyncWorker`` drains via ``mode="runtime"`` so each gunicorn
  worker reads/deletes only its own-PID WAL files: ``_post_sync_cleanup`` →
  ``cleanup_processed(mode="runtime")`` (G3 — no peer file deletion),
  ``_sync_batch`` → ``recover_unprocessed(mode="runtime")`` (G2/G4 — no peer
  over-replay, coherent per-worker cursor), and ``get_lag`` likewise.
- **D2** — ``WALReaderMixin.recover_orphans()`` (non-own-PID, read-only) +
  ``AuditSyncWorker.absorb_orphans()`` (idempotent one-shot startup absorption
  of a crashed peer's orphan entries; no cursor advance, no cross-PID cleanup),
  wired into ``_start_sync_worker()`` before ``start()``, surfaced via the new
  ``record_wal_orphans_absorbed()`` SLI counter.

The WAL-level ``mode`` glob semantics are covered by
``tests/unit/audit/wal/test_multi_worker_isolation.py`` (#470 D7); this
module covers the new #588 surface — ``recover_orphans``, ``absorb_orphans``,
the ``AuditSyncWorker`` D1 runtime wiring, and the orphan-absorbed metric.

Verification techniques per UNIT_TEST_GUIDELINES §8:
- Set membership: self-vs-peer PID file partitioning
- Boundary analysis: ``last_processed_seq`` lower bound; count=0 metric no-op
- Side effects: file deletion, adapter writes, metric increment, log event
- State invariant: ``_last_processed_seq`` cursor unchanged after absorption
- Dependency interaction: ``recover_unprocessed``/``cleanup_processed`` call args
- Negative side effect: no ``WAL_RECOVERED`` event, no cross-PID file deletion

Testbed: a real ``WriteAheadLog`` over ``tmp_path`` + a mock central adapter,
multi-PID WAL files stamped via raw on-disk writes (no ``baldur_pro`` import).
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from baldur.audit.sync_worker import AuditSyncWorker
from baldur.audit.wal import WriteAheadLog
from baldur.audit.wal._models import WALConfig
from baldur.interfaces.audit_adapter import AuditEntry
from baldur.metrics import drift_metrics

WAL_PREFIX = "test_wal"


# =============================================================================
# Fixtures & raw-WAL helpers (multi-PID stamping mirrors #470 test)
# =============================================================================


def _create_raw_wal_file(filepath: Path, entries: list[dict]) -> None:
    """Write a raw WAL file in the on-disk format ``_read_wal_file``
    understands (``AWAL`` header + length-prefixed JSON records with CRC32
    hex checksum). Bypasses ``WriteAheadLog`` so the filename can carry an
    arbitrary PID suffix — the only way to simulate a peer/orphan worker's
    file in a single test process.
    """
    with open(filepath, "wb") as f:
        f.write(b"AWAL")
        f.write(struct.pack(">I", 1))
        for entry_dict in entries:
            data = json.dumps(entry_dict).encode("utf-8")
            checksum = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
            f.write(struct.pack(">I", len(data)))
            f.write(checksum.encode("ascii"))
            f.write(data)


def _self_pid_filename(suffix: str = "001") -> str:
    return f"{WAL_PREFIX}_{suffix}_{os.getpid()}.wal"


def _peer_pid_filename(suffix: str = "001") -> str:
    """A PID this process is guaranteed not to own. A large offset also
    avoids a lexicographic-glob accident where a peer file sorts adjacent
    to a self-PID file.
    """
    peer = os.getpid() + 99999
    return f"{WAL_PREFIX}_{suffix}_{peer}.wal"


@pytest.fixture
def wal_config(tmp_path):
    return WALConfig(
        wal_dir=str(tmp_path),
        max_file_size_mb=1,
        sync_on_write=False,
        max_files=10,
        file_prefix=WAL_PREFIX,
    )


@pytest.fixture
def wal(wal_config):
    """A real WAL over ``tmp_path``. Constructed empty (no own-PID files
    yet), so ``_init_or_recover`` leaves ``_sequence == 0``; tests stamp
    raw multi-PID files afterward and the reader globs them at call time.
    """
    w = WriteAheadLog(config=wal_config)
    yield w
    w.close()


@pytest.fixture
def wal_dir(wal_config) -> Path:
    return Path(wal_config.wal_dir)


def _make_worker(wal_instance, adapter=None):
    """Build a directly-constructed (non-singleton) worker so tests never
    pollute ``AuditSyncWorker._instance``.
    """
    return AuditSyncWorker(wal=wal_instance, central_adapter=adapter or MagicMock())


# =============================================================================
# Behavior: WALReaderMixin.recover_orphans (D2a)
# =============================================================================


class TestWALRecoverOrphansBehavior:
    """``recover_orphans()`` returns entries from non-own-PID (peer/dead-PID)
    files only — disjoint from this worker's own runtime drain.
    """

    def test_recover_orphans_returns_non_own_pid_entries_only(self, wal, wal_dir):
        # Given: one own-PID file and one peer-PID file in the shared dir.
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("001"),
            [{"seq": 1, "ts": 1.0, "data": {"e": "own"}}],
        )
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("002"),
            [
                {"seq": 3, "ts": 3.0, "data": {"e": "peer_a"}},
                {"seq": 4, "ts": 4.0, "data": {"e": "peer_b"}},
            ],
        )

        # When
        entries = wal.recover_orphans()

        # Then: only peer-PID entries, own-PID excluded.
        assert sorted(e.sequence for e in entries) == [3, 4]
        payloads = {e.data.get("e") for e in entries}
        assert payloads == {"peer_a", "peer_b"}
        assert "own" not in payloads

    def test_recover_orphans_excludes_own_pid_files_entirely(self, wal, wal_dir):
        """Even a low-sequence own-PID file is never returned — the own
        file set and the orphan file set are disjoint by construction.
        """
        own_file = wal_dir / _self_pid_filename("001")
        _create_raw_wal_file(own_file, [{"seq": 1, "ts": 1.0, "data": {"e": "own"}}])

        entries = wal.recover_orphans()

        assert entries == []
        # Data immutability: read-only — the own file survives untouched.
        assert own_file.exists()

    def test_recover_orphans_empty_when_no_files(self, wal):
        """Cold start (fresh ``emptyDir``) — nothing to absorb."""
        assert wal.recover_orphans() == []

    def test_recover_orphans_sorted_by_sequence_across_files(self, wal, wal_dir):
        # Given: two peer files whose entries interleave out of order.
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("001"),
            [{"seq": 5, "ts": 5.0, "data": {}}],
        )
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("002"),
            [
                {"seq": 2, "ts": 2.0, "data": {}},
                {"seq": 8, "ts": 8.0, "data": {}},
            ],
        )

        entries = wal.recover_orphans()

        assert [e.sequence for e in entries] == [2, 5, 8]

    def test_recover_orphans_respects_last_processed_seq_lower_bound(
        self, wal, wal_dir
    ):
        """Boundary: only ``sequence > last_processed_seq`` is returned —
        seq == bound is excluded (just-before fail), seq > bound included.
        """
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("001"),
            [
                {"seq": 1, "ts": 1.0, "data": {}},
                {"seq": 2, "ts": 2.0, "data": {}},
                {"seq": 3, "ts": 3.0, "data": {}},
            ],
        )

        entries = wal.recover_orphans(last_processed_seq=2)

        assert [e.sequence for e in entries] == [3]

    def test_recover_orphans_emits_no_wal_recovered_event_no_recovered_advance(
        self, wal, wal_dir
    ):
        """Negative side effect: ``recover_orphans`` reads via
        ``_read_file_entries`` directly — it must NOT emit the
        ``WAL_RECOVERED`` audit event nor advance ``_recovered_entries``
        (the caller owns its own summary event).
        """
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("001"),
            [{"seq": 9, "ts": 9.0, "data": {}}],
        )

        with patch.object(wal, "_record_audit_event") as mock_event:
            recovered_before = wal._recovered_entries
            entries = wal.recover_orphans()

        assert len(entries) == 1
        mock_event.assert_not_called()
        assert wal._recovered_entries == recovered_before


# =============================================================================
# Behavior: AuditSyncWorker.absorb_orphans (D2b)
# =============================================================================


class TestAbsorbOrphansBehavior:
    """``absorb_orphans()`` drains a crashed peer's orphan entries once at
    startup through the idempotent ``_sync_entry_to_adapter`` path, without
    advancing the steady cursor or deleting cross-PID files.
    """

    def test_absorb_orphans_routes_each_orphan_through_sync_path(self, wal, wal_dir):
        """Each orphan entry is routed through ``_sync_entry_to_adapter``
        (the idempotent consumer). Spying the sync path is robust to the
        adapter-write dedup that happens inside it.
        """
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("001"),
            [
                {"seq": 1, "ts": 1.0, "data": {"e": "o1"}},
                {"seq": 2, "ts": 2.0, "data": {"e": "o2"}},
            ],
        )
        worker = _make_worker(wal)

        with patch.object(
            worker, "_sync_entry_to_adapter", wraps=worker._sync_entry_to_adapter
        ) as spy:
            absorbed = worker.absorb_orphans()

        assert absorbed == 2
        assert spy.call_count == 2
        synced_seqs = sorted(c.args[1].sequence for c in spy.call_args_list)
        assert synced_seqs == [1, 2]

    def test_absorb_orphans_writes_orphan_entries_to_central_adapter(
        self, wal, wal_dir
    ):
        """End-to-end: with the idempotency consumer bypassed (no Redis
        dedup), each orphan entry reaches ``adapter.log`` as a converted
        ``AuditEntry`` (not the raw WAL dict).
        """
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("001"),
            [
                {"seq": 1, "ts": 1.0, "data": {"e": "o1"}},
                {"seq": 2, "ts": 2.0, "data": {"e": "o2"}},
            ],
        )
        adapter = MagicMock()
        worker = _make_worker(wal, adapter=adapter)

        # A non-duplicate IdempotencyResult lets the write loop run (the "not yet
        # processed" branch) — exercises the real check() contract, not the
        # inverted check()->None mock the pre-#590 gate assumed.
        from baldur.services.idempotency.models import IdempotencyResult

        with patch("baldur.services.idempotency.IdempotencyService") as MockIdem:
            MockIdem.return_value.check.return_value = IdempotencyResult(
                is_duplicate=False
            )
            absorbed = worker.absorb_orphans()

        assert absorbed == 2
        assert adapter.log.call_count == 2
        logged = [c.args[0] for c in adapter.log.call_args_list]
        assert all(isinstance(e, AuditEntry) for e in logged)
        assert {e.details.get("e") for e in logged} == {"o1", "o2"}

    def test_absorb_orphans_does_not_advance_cursor(self, wal, wal_dir):
        """State invariant: orphan seqs live in a foreign sequence space —
        absorbing them must NOT move ``_last_processed_seq`` (advancing
        would re-introduce the G4 cursor incoherence).
        """
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("001"),
            [
                {"seq": 50, "ts": 1.0, "data": {}},
                {"seq": 51, "ts": 2.0, "data": {}},
            ],
        )
        worker = _make_worker(wal)
        assert worker._last_processed_seq == 0

        worker.absorb_orphans()

        assert worker._last_processed_seq == 0

    def test_absorb_orphans_does_not_delete_orphan_files(self, wal, wal_dir):
        """Negative side effect: no cross-PID ``cleanup_processed`` — the
        orphan file survives the absorption (reclaimed only by WAL
        retention).
        """
        orphan_file = wal_dir / _peer_pid_filename("001")
        _create_raw_wal_file(orphan_file, [{"seq": 1, "ts": 1.0, "data": {}}])
        worker = _make_worker(wal)

        worker.absorb_orphans()

        assert orphan_file.exists()

    def test_absorb_orphans_returns_absorbed_count(self, wal, wal_dir):
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("001"),
            [{"seq": i, "ts": float(i), "data": {}} for i in range(1, 4)],
        )
        worker = _make_worker(wal)

        assert worker.absorb_orphans() == 3

    def test_absorb_orphans_returns_zero_when_no_orphans(self, wal, wal_dir):
        """Only an own-PID file exists → nothing to absorb."""
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("001"),
            [{"seq": 1, "ts": 1.0, "data": {}}],
        )
        worker = _make_worker(wal)

        assert worker.absorb_orphans() == 0

    def test_absorb_orphans_returns_zero_when_wal_is_none(self):
        """Guard: no WAL resolvable → 0, no crash."""
        worker = AuditSyncWorker(wal=None, central_adapter=MagicMock())
        with patch.object(worker, "_get_wal", return_value=None):
            assert worker.absorb_orphans() == 0
        assert worker._last_processed_seq == 0

    def test_absorb_orphans_returns_zero_when_wal_lacks_recover_orphans(self):
        """Guard: a WAL implementation without ``recover_orphans`` (older
        WAL, or a non-WAL stub) → 0, no crash.
        """
        legacy_wal = Mock(
            spec=[]
        )  # no attributes → hasattr(..., "recover_orphans") False
        worker = AuditSyncWorker(wal=legacy_wal, central_adapter=MagicMock())

        assert worker.absorb_orphans() == 0

    def test_absorb_orphans_returns_zero_when_recover_orphans_raises(self, wal_dir):
        """Guard: ``recover_orphans`` raising is swallowed fail-open → 0."""
        wal_stub = Mock()
        wal_stub.recover_orphans.side_effect = OSError("disk error")
        worker = AuditSyncWorker(wal=wal_stub, central_adapter=MagicMock())

        assert worker.absorb_orphans() == 0

    def test_absorb_orphans_no_adapter_is_noop(self, wal, wal_dir):
        """Guard (#590 D3): no central adapter wired — ``absorb_orphans`` is a
        no-op. It absorbs nothing (no false count), advances no cursor, retains
        the orphan file, and emits no false ``record_wal_orphans_absorbed`` SLI
        signal. Pre-#590 it counted un-delivered entries as ``absorbed`` (the
        same false-count class as the ``synced_count`` bug D3 removes).
        """
        orphan_file = wal_dir / _peer_pid_filename("001")
        _create_raw_wal_file(
            orphan_file,
            [{"seq": 1, "ts": 1.0, "data": {}}, {"seq": 2, "ts": 2.0, "data": {}}],
        )
        worker = _make_worker(wal)

        with (
            patch.object(worker, "_get_adapter", return_value=None),
            patch(
                "baldur.metrics.drift_metrics.record_wal_orphans_absorbed"
            ) as mock_metric,
        ):
            absorbed = worker.absorb_orphans()

        assert absorbed == 0
        assert worker._last_processed_seq == 0
        assert orphan_file.exists()
        mock_metric.assert_not_called()

    def test_absorb_orphans_increments_metric_when_absorbed(self, wal, wal_dir):
        """Side effect: ``record_wal_orphans_absorbed`` is invoked with the
        absorbed count when > 0 (SRE-visible crash/orphan frequency SLI).
        """
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("001"),
            [{"seq": 1, "ts": 1.0, "data": {}}, {"seq": 2, "ts": 2.0, "data": {}}],
        )
        worker = _make_worker(wal)

        with patch(
            "baldur.metrics.drift_metrics.record_wal_orphans_absorbed"
        ) as mock_metric:
            worker.absorb_orphans()

        mock_metric.assert_called_once_with(2)

    def test_absorb_orphans_no_metric_when_nothing_absorbed(self, wal, wal_dir):
        """Negative side effect: zero orphans → no metric, no log event."""
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("001"),
            [{"seq": 1, "ts": 1.0, "data": {}}],
        )
        worker = _make_worker(wal)

        with patch(
            "baldur.metrics.drift_metrics.record_wal_orphans_absorbed"
        ) as mock_metric:
            absorbed = worker.absorb_orphans()

        assert absorbed == 0
        mock_metric.assert_not_called()


# =============================================================================
# Behavior: AuditSyncWorker D1 runtime drain wiring (G1/G2/G3/G4)
# =============================================================================


class TestSyncWorkerRuntimeDrain:
    """The ``AuditSyncWorker`` drain methods pass ``mode="runtime"`` so each
    worker reads/deletes only its own-PID files.
    """

    def test_post_sync_cleanup_preserves_peer_pid_file(self, wal, wal_dir):
        """G3 (data loss): a peer worker's still-active WAL file must
        survive this worker's drain cycle, even when its ``max_seq`` is
        below this worker's cursor. Under the pre-#588 ``startup`` default
        the peer file (max_seq=1 ≤ cursor=2) would be deleted.
        """
        own_file = wal_dir / _self_pid_filename("001")
        peer_file = wal_dir / _peer_pid_filename("002")
        _create_raw_wal_file(
            own_file,
            [
                {"seq": 1, "ts": 1.0, "data": {"e": "own_a"}},
                {"seq": 2, "ts": 2.0, "data": {"e": "own_b"}},
            ],
        )
        _create_raw_wal_file(peer_file, [{"seq": 1, "ts": 1.0, "data": {"e": "peer"}}])
        worker = _make_worker(wal)

        # When: a full drain cycle runs (advances cursor to 2, then cleans).
        synced, _ = worker.sync_now()

        # Then: own file drained+removed, peer file survives.
        assert synced == 2
        assert worker._last_processed_seq == 2
        assert peer_file.exists()
        assert not own_file.exists()

    def test_post_sync_cleanup_calls_cleanup_processed_runtime_mode(self, wal, wal_dir):
        """Dependency interaction: ``_post_sync_cleanup`` passes
        ``mode="runtime"`` to ``cleanup_processed`` (the D1 wiring).
        """
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("001"),
            [{"seq": 1, "ts": 1.0, "data": {}}],
        )
        worker = _make_worker(wal)

        with patch.object(wal, "cleanup_processed", wraps=wal.cleanup_processed) as spy:
            worker.sync_now()

        spy.assert_called_once()
        assert spy.call_args.kwargs.get("mode") == "runtime"

    def test_sync_batch_recovers_own_pid_entries_only(self, wal, wal_dir):
        """G2/G4: ``_sync_batch`` recovers only own-PID entries — a peer's
        entries are never replayed (no duplicate central-store records).
        """
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("001"),
            [
                {"seq": 1, "ts": 1.0, "data": {"e": "own_a"}},
                {"seq": 3, "ts": 3.0, "data": {"e": "own_b"}},
            ],
        )
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("002"),
            [{"seq": 2, "ts": 2.0, "data": {"e": "peer"}}],
        )
        worker = _make_worker(wal)

        synced, failed = worker.sync_now()

        # Only the 2 own-PID entries synced; the peer entry was filtered out.
        assert (synced, failed) == (2, 0)
        assert worker.get_stats()["current_lag_entries"] == 2

    def test_sync_batch_calls_recover_unprocessed_runtime_mode(self, wal, wal_dir):
        """Dependency interaction: ``_sync_batch`` passes ``mode="runtime"``
        to ``recover_unprocessed``.
        """
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("001"),
            [{"seq": 1, "ts": 1.0, "data": {}}],
        )
        worker = _make_worker(wal)

        with patch.object(
            wal, "recover_unprocessed", wraps=wal.recover_unprocessed
        ) as spy:
            worker.sync_now()

        assert spy.call_args.kwargs.get("mode") == "runtime"

    def test_get_lag_counts_own_pid_entries_only(self, wal, wal_dir):
        """``get_lag`` returns own-PID lag only — coherent with the
        per-worker cursor. With a cross-PID glob it would return 8.
        """
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("001"),
            [{"seq": i, "ts": float(i), "data": {}} for i in (1, 2, 3)],
        )
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("002"),
            [{"seq": i, "ts": float(i), "data": {}} for i in (1, 2, 3, 4, 5)],
        )
        worker = _make_worker(wal)

        assert worker.get_lag() == 3

    def test_get_lag_calls_recover_unprocessed_runtime_mode(self, wal, wal_dir):
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("001"),
            [{"seq": 1, "ts": 1.0, "data": {}}],
        )
        worker = _make_worker(wal)

        with patch.object(
            wal, "recover_unprocessed", wraps=wal.recover_unprocessed
        ) as spy:
            worker.get_lag()

        assert spy.call_args.kwargs.get("mode") == "runtime"

    def test_single_process_drains_own_wal_unchanged(self, wal, wal_dir):
        """Regression: with a single PID (sole-PID == own-PID) the runtime
        glob equals the full glob — the worker drains its WAL exactly as
        before, no behavior change for the OSS single-process deployment.
        """
        own_file = wal_dir / _self_pid_filename("001")
        _create_raw_wal_file(
            own_file,
            [
                {"seq": 1, "ts": 1.0, "data": {}},
                {"seq": 2, "ts": 2.0, "data": {}},
            ],
        )
        worker = _make_worker(wal)

        synced, failed = worker.sync_now()

        assert (synced, failed) == (2, 0)
        assert worker._last_processed_seq == 2
        # Fully-synced sole file is reclaimed (max_seq=2 ≤ cursor=2).
        assert not own_file.exists()


# =============================================================================
# Wiring: _start_sync_worker absorbs orphans before starting (D2c)
# =============================================================================


class TestStartSyncWorkerWiringBehavior:
    """``_start_sync_worker()`` runs the one-shot orphan absorption before
    the steady drain loop starts.
    """

    def test_absorb_orphans_called_before_start(self):
        from baldur.audit import async_audit_lifecycle

        instance = Mock()
        with patch.object(AuditSyncWorker, "get_instance", return_value=instance):
            async_audit_lifecycle._start_sync_worker()

        # Order matters: absorb_orphans() must precede start().
        assert instance.mock_calls == [call.absorb_orphans(), call.start()]


# =============================================================================
# Contract: record_wal_orphans_absorbed metric (D2 SLI)
# =============================================================================


class TestDriftMetricsContract:
    """``record_wal_orphans_absorbed`` increments the orphan-absorbed
    counter — the SRE-visible, alertable surface for multi-worker crash
    frequency (a log line alone is not alertable).
    """

    @pytest.fixture(autouse=True)
    def _skip_when_prometheus_unavailable(self):
        if not drift_metrics.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

    def test_counter_and_helper_are_exported(self):
        assert "wal_orphans_absorbed_total" in drift_metrics.__all__
        assert "record_wal_orphans_absorbed" in drift_metrics.__all__
        assert drift_metrics.wal_orphans_absorbed_total is not None

    def test_counter_registered_with_canonical_name(self):
        from prometheus_client import REGISTRY

        assert "baldur_wal_orphans_absorbed_total" in REGISTRY._names_to_collectors

    def test_record_increments_counter_by_count(self):
        before = drift_metrics.wal_orphans_absorbed_total._value.get()
        drift_metrics.record_wal_orphans_absorbed(5)
        after = drift_metrics.wal_orphans_absorbed_total._value.get()

        assert after - before == 5

    def test_record_count_zero_is_noop(self):
        before = drift_metrics.wal_orphans_absorbed_total._value.get()
        drift_metrics.record_wal_orphans_absorbed(0)
        after = drift_metrics.wal_orphans_absorbed_total._value.get()

        assert after - before == 0

    def test_record_is_noop_when_counter_unavailable(self):
        """Boundary: ``prometheus_client`` absent → counter is ``None`` →
        the recorder is a silent no-op (no ``AttributeError``).
        """
        with patch.object(drift_metrics, "wal_orphans_absorbed_total", None):
            drift_metrics.record_wal_orphans_absorbed(3)  # must not raise
