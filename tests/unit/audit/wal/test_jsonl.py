"""
JSONLWriter / JSONLReader / CommitMarker 단위 테스트.

대상: packages/baldur-python/src/baldur/audit/wal/_jsonl.py
"""

from __future__ import annotations

import json
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.wal._jsonl import CommitMarker, JSONLReader, JSONLWriter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# wal_dir, wal_file fixtures are in conftest.py (shared with test_cleanup.py)


@pytest.fixture
def writer(wal_file: Path) -> Generator[JSONLWriter, None, None]:
    """기본 JSONLWriter (fsync=True, 로테이션 없음)."""
    w = JSONLWriter(file_path=wal_file, fsync=True)
    yield w
    w.close()


@pytest.fixture
def writer_no_fsync(wal_file: Path) -> Generator[JSONLWriter, None, None]:
    """fsync=False JSONLWriter."""
    w = JSONLWriter(file_path=wal_file, fsync=False)
    yield w
    w.close()


# ===========================================================================
# JSONLWriter Tests
# ===========================================================================


class TestJSONLWriterAppendBehavior:
    """JSONLWriter.append() 동작 검증."""

    def test_append_empty_dict_writes_valid_jsonl(
        self, writer: JSONLWriter, wal_file: Path
    ):
        """빈 딕셔너리 append 시 유효한 JSONL 라인이 기록된다."""
        writer.append({})

        lines = wal_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {}

    def test_append_large_entry_writes_complete_line(
        self, writer: JSONLWriter, wal_file: Path
    ):
        """큰 엔트리도 한 줄로 완전하게 기록된다."""
        large_entry = {"data": "x" * 100_000, "seq": 1}
        writer.append(large_entry)

        lines = wal_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert len(parsed["data"]) == 100_000

    def test_append_with_fsync_true_calls_os_fsync(self, wal_file: Path):
        """fsync=True 시 os.fsync가 호출된다."""
        w = JSONLWriter(file_path=wal_file, fsync=True)
        with patch("baldur.audit.wal._jsonl.os.fsync") as mock_fsync:
            w.append({"key": "value"})
            mock_fsync.assert_called_once()
        w.close()

    def test_append_with_fsync_false_skips_os_fsync(self, wal_file: Path):
        """fsync=False 시 os.fsync가 호출되지 않는다."""
        w = JSONLWriter(file_path=wal_file, fsync=False)
        with patch("baldur.audit.wal._jsonl.os.fsync") as mock_fsync:
            w.append({"key": "value"})
            mock_fsync.assert_not_called()
        w.close()

    def test_append_concurrent_writes_no_data_loss(self, wal_file: Path):
        """10개 스레드가 동시에 append해도 데이터 유실 없음."""
        w = JSONLWriter(file_path=wal_file, fsync=False)
        n_threads = 10
        n_writes_per_thread = 50
        errors: list[Exception] = []

        def worker(tid: int) -> None:
            try:
                for i in range(n_writes_per_thread):
                    w.append({"tid": tid, "i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        w.close()

        assert len(errors) == 0
        lines = wal_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == n_threads * n_writes_per_thread

    def test_append_multiple_entries_creates_multiline_file(
        self, writer: JSONLWriter, wal_file: Path
    ):
        """복수 엔트리 append 시 각각 개별 라인으로 기록된다."""
        writer.append({"seq": 1})
        writer.append({"seq": 2})
        writer.append({"seq": 3})

        lines = wal_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        seqs = [json.loads(line)["seq"] for line in lines]
        assert seqs == [1, 2, 3]


class TestJSONLWriterMaybeRotateBehavior:
    """JSONLWriter._maybe_rotate() 동작 검증."""

    def test_rotate_triggers_at_exact_max_size(self, wal_dir: Path):
        """현재 크기가 정확히 max_size일 때 로테이션이 발생한다."""
        wal_file = wal_dir / "rotate_exact.jsonl"
        # 작은 max_size로 설정하여 1회 append로 초과하도록
        w = JSONLWriter(file_path=wal_file, fsync=False, max_size_bytes=10)
        # 10 bytes 이상의 엔트리 작성
        w.append({"data": "long_value"})

        # 로테이션 후 새 파일이 열려야 함 (기존 파일은 .timestamp.jsonl로 이동)
        rotated_files = list(wal_dir.glob("*.*.jsonl"))
        assert len(rotated_files) >= 1
        w.close()

    def test_rotate_triggers_over_max_size(self, wal_dir: Path):
        """현재 크기가 max_size를 초과하면 로테이션이 발생한다."""
        wal_file = wal_dir / "rotate_over.jsonl"
        # 충분히 큰 max_size로 1회만 로테이션 트리거
        w = JSONLWriter(file_path=wal_file, fsync=False, max_size_bytes=100)

        # 100 바이트 이상 되도록 엔트리 작성
        w.append({"seq": 0, "data": "x" * 120})

        rotated_files = list(wal_dir.glob("*.*.jsonl"))
        assert len(rotated_files) >= 1
        w.close()

    def test_no_rotation_when_max_size_none(self, writer: JSONLWriter, wal_dir: Path):
        """max_size_bytes=None 시 로테이션이 발생하지 않는다."""
        for i in range(100):
            writer.append({"seq": i, "data": "x" * 100})

        rotated_files = list(wal_dir.glob("*.*.jsonl"))
        assert len(rotated_files) == 0

    def test_rotate_swaps_file_handle(self, wal_dir: Path):
        """로테이션 시 파일 핸들이 교체되고 _current_size가 0으로 초기화된다."""
        wal_file = wal_dir / "rotate_swap.jsonl"
        w = JSONLWriter(file_path=wal_file, fsync=False, max_size_bytes=20)
        w.append({"data": "trigger_rotation_xx"})

        # 로테이션 후 _current_size는 0이어야 함
        assert w._current_size == 0
        assert w._handle is not None
        w.close()


class TestJSONLWriterEnsureOpenBehavior:
    """JSONLWriter.ensure_open() 동작 검증."""

    def test_ensure_open_idempotency(self, writer: JSONLWriter):
        """ensure_open()을 연속 2회 호출해도 동일한 핸들을 유지한다."""
        writer.ensure_open()
        handle_1 = writer._handle
        writer.ensure_open()
        handle_2 = writer._handle
        assert handle_1 is handle_2

    def test_ensure_open_creates_parent_directory(self, tmp_path: Path):
        """부모 디렉토리가 없을 때 자동 생성된다."""
        deep_path = tmp_path / "a" / "b" / "c" / "test.jsonl"
        w = JSONLWriter(file_path=deep_path)
        w.ensure_open()

        assert deep_path.parent.exists()
        assert w._handle is not None
        w.close()

    def test_ensure_open_after_close_reopens(self, writer: JSONLWriter):
        """close() 후 ensure_open() 호출 시 새 핸들이 열린다."""
        writer.ensure_open()
        writer.close()
        assert writer._handle is None

        writer.ensure_open()
        assert writer._handle is not None


# ===========================================================================
# JSONLReader Tests
# ===========================================================================


class TestJSONLReaderIterEntriesBehavior:
    """JSONLReader.iter_entries() 동작 검증."""

    def test_corrupted_line_skipped_with_warning_log(self, wal_file: Path):
        """손상된 라인은 경고 로그 후 건너뛴다 (doc §5.1.2)."""
        wal_file.write_text(
            '{"seq": 1}\nNOT_VALID_JSON\n{"seq": 2}\n',
            encoding="utf-8",
        )

        with patch("baldur.audit.wal._jsonl.logger") as mock_logger:
            entries = list(JSONLReader.iter_entries(wal_file))

        assert len(entries) == 2
        assert entries[0]["seq"] == 1
        assert entries[1]["seq"] == 2
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert "corrupted_line_skipped" in call_args[0][0]

    def test_empty_file_yields_nothing(self, wal_file: Path):
        """빈 파일에서는 엔트리가 없다."""
        wal_file.write_text("", encoding="utf-8")
        entries = list(JSONLReader.iter_entries(wal_file))
        assert entries == []

    def test_missing_file_yields_nothing(self, tmp_path: Path):
        """존재하지 않는 파일에서는 엔트리가 없다."""
        missing = tmp_path / "nonexistent.jsonl"
        entries = list(JSONLReader.iter_entries(missing))
        assert entries == []

    def test_mixed_valid_and_invalid_lines(self, wal_file: Path):
        """유효/무효 라인이 혼재된 파일에서 유효 라인만 반환한다."""
        wal_file.write_text(
            '{"seq": 1}\nGARBAGE\n\n{"seq": 2}\n{broken json\n{"seq": 3}\n',
            encoding="utf-8",
        )

        with patch("baldur.audit.wal._jsonl.logger"):
            entries = list(JSONLReader.iter_entries(wal_file))

        assert len(entries) == 3
        assert [e["seq"] for e in entries] == [1, 2, 3]

    def test_corrupted_line_increments_metric(self, wal_file: Path):
        """손상 라인 발견 시 record_wal_corrupted_line 메트릭이 호출된다."""
        wal_file.write_text("BAD_LINE\n", encoding="utf-8")

        with patch("baldur.audit.wal._jsonl.logger"):
            with patch(
                "baldur.audit.wal._jsonl.record_wal_corrupted_line",
                create=True,
            ) as mock_metric:
                # iter_entries에서 lazy import하므로 모듈 내부를 패치
                with patch.dict(
                    "sys.modules",
                    {
                        "baldur.metrics.drift_metrics": MagicMock(
                            record_wal_corrupted_line=mock_metric
                        )
                    },
                ):
                    list(JSONLReader.iter_entries(wal_file))

                    mock_metric.assert_called_once()


class TestJSONLReaderParseWithCommittedFilterBehavior:
    """JSONLReader.parse_with_committed_filter() 동작 검증."""

    def test_commit_marker_detection_via_marker_field(self, wal_file: Path):
        """_marker=COMMIT 필드를 가진 엔트리를 커밋으로 인식한다."""
        lines = [
            json.dumps({"seq": 1, "data": "pending_entry"}) + "\n",
            json.dumps(
                {"_marker": "COMMIT", "wal_sequence": 1, "timestamp": "2026-01-01"}
            )
            + "\n",
            json.dumps({"seq": 2, "data": "another_pending"}) + "\n",
        ]
        wal_file.write_text("".join(lines), encoding="utf-8")

        entries, committed = JSONLReader.parse_with_committed_filter(wal_file)

        # COMMIT 마커의 wal_sequence=1이 committed에 포함
        assert 1 in committed
        assert 2 not in committed
        # seq=1 pending + seq=2 pending이 entries에 남음 (마커는 entries에 포함 안 됨)
        assert len(entries) == 2
        assert entries[0]["seq"] == 1
        assert entries[1]["seq"] == 2

    def test_commit_marker_detection_via_status_field(self, wal_file: Path):
        """status=COMMITTED 필드를 가진 엔트리를 커밋으로 인식한다."""
        lines = [
            json.dumps({"seq": 1, "status": "PENDING"}) + "\n",
            json.dumps({"seq": 2, "status": "COMMITTED"}) + "\n",
            json.dumps({"seq": 3, "status": "PENDING"}) + "\n",
        ]
        wal_file.write_text("".join(lines), encoding="utf-8")

        entries, committed = JSONLReader.parse_with_committed_filter(wal_file)

        assert 2 in committed
        assert 1 not in committed
        assert 3 not in committed
        # PENDING 엔트리만 entries에 포함
        assert len(entries) == 2

    def test_sequence_tracking_with_wal_sequence_key(self, wal_file: Path):
        """wal_sequence 키를 통한 시퀀스 추적이 동작한다."""
        lines = [
            json.dumps({"wal_sequence": 10, "status": "COMMITTED"}) + "\n",
            json.dumps({"wal_sequence": 11, "data": "pending"}) + "\n",
        ]
        wal_file.write_text("".join(lines), encoding="utf-8")

        entries, committed = JSONLReader.parse_with_committed_filter(wal_file)

        assert 10 in committed
        assert len(entries) == 1
        assert entries[0]["wal_sequence"] == 11

    def test_custom_commit_field_and_value(self, wal_file: Path):
        """커스텀 commit_field/commit_value로 필터링이 동작한다."""
        lines = [
            json.dumps({"seq": 1, "state": "DONE"}) + "\n",
            json.dumps({"seq": 2, "state": "PENDING"}) + "\n",
        ]
        wal_file.write_text("".join(lines), encoding="utf-8")

        entries, committed = JSONLReader.parse_with_committed_filter(
            wal_file,
            commit_field="state",
            commit_value="DONE",
        )

        assert 1 in committed
        assert len(entries) == 1

    def test_empty_file_returns_empty_results(self, wal_file: Path):
        """빈 파일에서는 빈 결과를 반환한다."""
        wal_file.write_text("", encoding="utf-8")
        entries, committed = JSONLReader.parse_with_committed_filter(wal_file)
        assert entries == []
        assert committed == set()


# ===========================================================================
# CommitMarker Tests
# ===========================================================================


class TestCommitMarkerContract:
    """CommitMarker TypedDict 구조 계약 검증 (doc §5.1.6)."""

    def test_commit_marker_has_required_keys(self):
        """CommitMarker는 _marker, wal_sequence, timestamp 키를 가진다."""
        annotations = CommitMarker.__annotations__
        assert "_marker" in annotations
        assert "wal_sequence" in annotations
        assert "timestamp" in annotations

    def test_commit_marker_field_count_is_three(self):
        """CommitMarker는 정확히 3개 필드를 가진다."""
        assert len(CommitMarker.__annotations__) == 3

    def test_commit_marker_marker_field_type_is_literal_commit(self):
        """_marker 필드의 타입은 Literal['COMMIT']이다."""
        # get_type_hints resolves ForwardRef from __future__ annotations
        import typing

        hints = typing.get_type_hints(CommitMarker)
        assert hints["_marker"] == Literal["COMMIT"]

    def test_commit_marker_valid_instance(self):
        """유효한 CommitMarker 인스턴스를 생성할 수 있다."""
        marker: CommitMarker = {
            "_marker": "COMMIT",
            "wal_sequence": 42,
            "timestamp": "2026-03-06T10:00:00Z",
        }
        assert marker["_marker"] == "COMMIT"
        assert marker["wal_sequence"] == 42
        assert isinstance(marker["timestamp"], str)
