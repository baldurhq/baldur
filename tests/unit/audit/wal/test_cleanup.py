"""
WAL 정리 유틸리티 단위 테스트.

대상: packages/baldur-python/src/baldur/audit/wal/_cleanup.py
설계 문서: docs/baldur/middleware_system/306_AUDIT_WAL_CONSOLIDATION.md
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from baldur.audit.wal._cleanup import (
    atomic_rewrite,
    cleanup_by_age,
    cleanup_by_namespace,
    cleanup_by_sequence,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# wal_dir, wal_file fixtures are in conftest.py (shared with test_jsonl.py)


# ===========================================================================
# atomic_rewrite Tests
# ===========================================================================


class TestAtomicRewriteBehavior:
    """atomic_rewrite() 동작 검증 (doc §Phase 3, D7)."""

    def test_atomic_rewrite_replaces_file_content(self, wal_file: Path):
        """파일 내용이 원자적으로 교체된다."""
        wal_file.write_text("old_line_1\nold_line_2\n", encoding="utf-8")

        new_lines = ["new_line_1\n", "new_line_2\n", "new_line_3\n"]
        atomic_rewrite(wal_file, new_lines)

        result = wal_file.read_text(encoding="utf-8")
        assert result == "new_line_1\nnew_line_2\nnew_line_3\n"

    def test_atomic_rewrite_no_tmp_file_remains(self, wal_file: Path):
        """교체 후 .tmp 임시 파일이 남아있지 않다."""
        wal_file.write_text("original\n", encoding="utf-8")
        atomic_rewrite(wal_file, ["replaced\n"])

        tmp_files = list(wal_file.parent.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_atomic_rewrite_directory_fsync_oserror_ignored_on_windows(
        self, wal_file: Path
    ):
        """디렉토리 fsync에서 OSError 발생 시 무시된다 (Windows 호환)."""
        wal_file.write_text("data\n", encoding="utf-8")

        with patch(
            "baldur.audit.wal._cleanup.os.open",
            side_effect=OSError("not supported"),
        ):
            # OSError가 발생해도 교체는 정상 완료되어야 함
            atomic_rewrite(wal_file, ["new_data\n"])

        assert wal_file.read_text(encoding="utf-8") == "new_data\n"

    def test_atomic_rewrite_empty_lines_creates_empty_file(self, wal_file: Path):
        """빈 라인 리스트로 호출 시 빈 파일이 생성된다."""
        wal_file.write_text("existing\n", encoding="utf-8")
        atomic_rewrite(wal_file, [])
        assert wal_file.read_text(encoding="utf-8") == ""


# ===========================================================================
# cleanup_by_sequence Tests
# ===========================================================================


class TestCleanupBySequenceBehavior:
    """cleanup_by_sequence() 동작 검증."""

    def test_keep_after_seq_zero_removes_seq_zero_entries(self, wal_file: Path):
        """keep_after_seq=0 시 seq=0인 엔트리가 제거된다."""
        lines = [
            json.dumps({"seq": 0, "data": "old"}) + "\n",
            json.dumps({"seq": 1, "data": "new"}) + "\n",
        ]
        wal_file.write_text("".join(lines), encoding="utf-8")

        removed = cleanup_by_sequence(wal_file, keep_after_seq=0)

        assert removed == 1
        remaining = wal_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining) == 1
        assert json.loads(remaining[0])["seq"] == 1

    def test_all_entries_removed_when_all_below_threshold(self, wal_file: Path):
        """모든 엔트리가 임계값 이하이면 모두 제거된다."""
        lines = [json.dumps({"seq": i}) + "\n" for i in range(5)]
        wal_file.write_text("".join(lines), encoding="utf-8")

        removed = cleanup_by_sequence(wal_file, keep_after_seq=10)

        assert removed == 5

    def test_no_entries_removed_when_all_above_threshold(self, wal_file: Path):
        """모든 엔트리가 임계값 초과이면 제거되지 않는다."""
        lines = [json.dumps({"seq": i}) + "\n" for i in range(10, 15)]
        wal_file.write_text("".join(lines), encoding="utf-8")

        removed = cleanup_by_sequence(wal_file, keep_after_seq=5)

        assert removed == 0
        remaining = wal_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining) == 5

    def test_malformed_lines_preserved(self, wal_file: Path):
        """JSON 파싱 실패 라인은 보존된다."""
        wal_file.write_text(
            '{"seq": 1}\nNOT_JSON\n{"seq": 5}\n',
            encoding="utf-8",
        )

        removed = cleanup_by_sequence(wal_file, keep_after_seq=3)

        assert removed == 1  # seq=1만 제거
        remaining = wal_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining) == 2
        # NOT_JSON과 seq=5가 남아야 함
        assert remaining[0] == "NOT_JSON"
        assert json.loads(remaining[1])["seq"] == 5

    def test_missing_file_returns_zero(self, tmp_path: Path):
        """존재하지 않는 파일에서는 0을 반환한다."""
        missing = tmp_path / "nonexistent.jsonl"
        assert cleanup_by_sequence(missing, keep_after_seq=5) == 0


# ===========================================================================
# cleanup_by_age Tests
# ===========================================================================


class TestCleanupByAgeBehavior:
    """cleanup_by_age() 동작 검증."""

    def test_old_files_deleted_by_date_in_filename(self, wal_dir: Path):
        """파일명의 날짜가 cutoff보다 이전이면 삭제된다."""
        # 10일 전 날짜
        old_date = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y%m%d")
        old_file = wal_dir / f"wal_{old_date}.jsonl"
        old_file.write_text("{}\n", encoding="utf-8")

        # 오늘 날짜
        today = datetime.now(UTC).strftime("%Y%m%d")
        new_file = wal_dir / f"wal_{today}.jsonl"
        new_file.write_text("{}\n", encoding="utf-8")

        removed = cleanup_by_age(wal_dir, pattern="wal_*.jsonl", max_age_days=7)

        assert removed == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_file_at_exact_cutoff_date_is_deleted(self, wal_dir: Path):
        """cutoff 날짜 파일은 삭제된다 (strptime midnight < now - 7 days)."""
        cutoff_date = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y%m%d")
        cutoff_file = wal_dir / f"wal_{cutoff_date}.jsonl"
        cutoff_file.write_text("{}\n", encoding="utf-8")

        removed = cleanup_by_age(wal_dir, pattern="wal_*.jsonl", max_age_days=7)

        assert removed == 1
        assert not cutoff_file.exists()

    def test_no_matching_files_returns_zero(self, wal_dir: Path):
        """매칭 파일이 없으면 0을 반환한다."""
        removed = cleanup_by_age(wal_dir, pattern="wal_*.jsonl", max_age_days=7)
        assert removed == 0

    def test_unparseable_date_filename_skipped(self, wal_dir: Path):
        """날짜를 파싱할 수 없는 파일명은 건너뛴다."""
        bad_file = wal_dir / "wal_notadate.jsonl"
        bad_file.write_text("{}\n", encoding="utf-8")

        removed = cleanup_by_age(wal_dir, pattern="wal_*.jsonl", max_age_days=1)

        assert removed == 0
        assert bad_file.exists()


# ===========================================================================
# cleanup_by_namespace Tests
# ===========================================================================


class TestCleanupByNamespaceBehavior:
    """cleanup_by_namespace() 동작 검증."""

    def test_removes_entries_with_matching_namespace(self, wal_file: Path):
        """지정 namespace의 엔트리만 제거된다."""
        lines = [
            json.dumps({"namespace": "orders", "seq": 1}) + "\n",
            json.dumps({"namespace": "payments", "seq": 2}) + "\n",
            json.dumps({"namespace": "orders", "seq": 3}) + "\n",
        ]
        wal_file.write_text("".join(lines), encoding="utf-8")

        removed = cleanup_by_namespace(wal_file, namespace="orders")

        assert removed == 2
        remaining = wal_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining) == 1
        assert json.loads(remaining[0])["namespace"] == "payments"

    def test_empty_result_deletes_file(self, wal_file: Path):
        """모든 엔트리가 제거되면 파일이 삭제된다."""
        lines = [
            json.dumps({"namespace": "orders", "seq": 1}) + "\n",
            json.dumps({"namespace": "orders", "seq": 2}) + "\n",
        ]
        wal_file.write_text("".join(lines), encoding="utf-8")

        removed = cleanup_by_namespace(wal_file, namespace="orders")

        assert removed == 2
        assert not wal_file.exists()

    def test_mixed_namespaces_preserves_others(self, wal_file: Path):
        """다른 namespace의 엔트리는 보존된다."""
        lines = [
            json.dumps({"namespace": "a", "data": 1}) + "\n",
            json.dumps({"namespace": "b", "data": 2}) + "\n",
            json.dumps({"namespace": "c", "data": 3}) + "\n",
        ]
        wal_file.write_text("".join(lines), encoding="utf-8")

        removed = cleanup_by_namespace(wal_file, namespace="b")

        assert removed == 1
        remaining = wal_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining) == 2
        namespaces = [json.loads(line)["namespace"] for line in remaining]
        assert "b" not in namespaces
        assert "a" in namespaces
        assert "c" in namespaces

    def test_no_matching_namespace_returns_zero(self, wal_file: Path):
        """매칭 namespace가 없으면 0을 반환하고 파일은 변경되지 않는다."""
        original = json.dumps({"namespace": "orders", "seq": 1}) + "\n"
        wal_file.write_text(original, encoding="utf-8")

        removed = cleanup_by_namespace(wal_file, namespace="nonexistent")

        assert removed == 0
        assert wal_file.read_text(encoding="utf-8") == original

    def test_missing_file_returns_zero(self, tmp_path: Path):
        """존재하지 않는 파일에서는 0을 반환한다."""
        missing = tmp_path / "nonexistent.jsonl"
        assert cleanup_by_namespace(missing, namespace="test") == 0

    def test_malformed_lines_preserved_during_cleanup(self, wal_file: Path):
        """JSON 파싱 실패 라인은 namespace 정리 시에도 보존된다."""
        wal_file.write_text(
            '{"namespace": "orders", "seq": 1}\n'
            "NOT_JSON\n"
            '{"namespace": "payments", "seq": 2}\n',
            encoding="utf-8",
        )

        removed = cleanup_by_namespace(wal_file, namespace="orders")

        assert removed == 1
        remaining = wal_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining) == 2
        assert remaining[0] == "NOT_JSON"
