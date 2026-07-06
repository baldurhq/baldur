"""
Tests for Hash Chain Verifier CLI Tool.

Tests:
- HashChainVerifier 기능 테스트
- AuditIntegrityVerifier 테스트
- CLI 출력 형식 테스트
- WAL 검증 테스트
- 엣지 케이스 테스트
"""

from __future__ import annotations

import json
import struct
import tempfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from baldur.audit.integrity import (
    HashChainManager,
    HashChainVerifier,
    compute_hash,
    verify_audit_log_integrity,
)
from baldur.audit.verify_audit_integrity import (
    AuditIntegrityVerifier,
    VerificationResult,
    VerificationSummary,
    format_json_output,
    format_summary_output,
    format_text_output,
)


class TestComputeHash:
    """compute_hash 함수 테스트."""

    def test_compute_hash_deterministic(self):
        """동일 데이터는 동일 해시."""
        data = {"key": "value", "number": 42}
        hash1 = compute_hash(data)
        hash2 = compute_hash(data)
        assert hash1 == hash2

    def test_compute_hash_different_data(self):
        """다른 데이터는 다른 해시."""
        data1 = {"key": "value1"}
        data2 = {"key": "value2"}
        assert compute_hash(data1) != compute_hash(data2)

    def test_compute_hash_key_order_independent(self):
        """키 순서에 관계없이 동일 해시."""
        data1 = {"a": 1, "b": 2, "c": 3}
        data2 = {"c": 3, "a": 1, "b": 2}
        assert compute_hash(data1) == compute_hash(data2)

    def test_compute_hash_returns_hex_string(self):
        """SHA-256 해시 형식."""
        data = {"test": "data"}
        hash_value = compute_hash(data)
        assert len(hash_value) == 64  # SHA-256 hex
        assert all(c in "0123456789abcdef" for c in hash_value)


class TestHashChainManager:
    """HashChainManager 테스트."""

    def test_add_integrity_basic(self):
        """기본 무결성 정보 추가."""
        manager = HashChainManager()
        entry = {"event": "test", "data": "value"}

        result = manager.add_integrity(entry)

        assert "integrity" in result
        assert result["integrity"]["sequence"] == 1
        assert result["integrity"]["previous_hash"] == "GENESIS"
        assert "current_hash" in result["integrity"]
        assert "timestamp" in result["integrity"]

    def test_add_integrity_chain(self):
        """해시 체인 연결."""
        manager = HashChainManager()

        entry1 = manager.add_integrity({"event": "first"})
        entry2 = manager.add_integrity({"event": "second"})

        assert entry1["integrity"]["sequence"] == 1
        assert entry2["integrity"]["sequence"] == 2
        assert (
            entry2["integrity"]["previous_hash"] == entry1["integrity"]["current_hash"]
        )

    def test_add_integrity_multiple_entries(self):
        """다중 엔트리 체인."""
        manager = HashChainManager()
        entries = []

        for i in range(10):
            entry = manager.add_integrity({"event": f"event_{i}"})
            entries.append(entry)

        # 모든 시퀀스 확인
        for i, entry in enumerate(entries):
            assert entry["integrity"]["sequence"] == i + 1

        # 체인 연결 확인
        for i in range(1, len(entries)):
            assert (
                entries[i]["integrity"]["previous_hash"]
                == entries[i - 1]["integrity"]["current_hash"]
            )

    def test_get_state(self):
        """상태 조회."""
        manager = HashChainManager()
        manager.add_integrity({"event": "test"})

        state = manager.get_state()

        assert state["sequence"] == 1
        assert "previous_hash" in state

    def test_reset(self):
        """상태 리셋."""
        manager = HashChainManager()
        manager.add_integrity({"event": "test"})
        manager.reset()

        state = manager.get_state()
        assert state["sequence"] == 0

    def test_persistence(self):
        """State persistence across manager instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "chain_state.json"

            # First manager: add entries
            manager1 = HashChainManager(state_file=state_file)
            for i in range(15):
                manager1.add_integrity({"event": f"event_{i}"})

            # Second manager: load state from file
            manager2 = HashChainManager(state_file=state_file)
            state = manager2.get_state()

            # Multi-writer mode (D22) is the default since 416: every write
            # is persisted under the cross-process file lock to guarantee
            # the state file matches the in-memory sequence on every commit.
            assert state["sequence"] == 15


class TestHashChainVerifier:
    """HashChainVerifier 테스트."""

    def _create_valid_chain(self, count: int = 5) -> list[dict[str, Any]]:
        """유효한 해시 체인 생성."""
        manager = HashChainManager()
        return [
            manager.add_integrity({"event": f"event_{i}", "data": f"data_{i}"})
            for i in range(count)
        ]

    def test_verify_chain_valid(self):
        """유효한 체인 검증."""
        entries = self._create_valid_chain(5)
        verifier = HashChainVerifier()

        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True
        assert error is None

    def test_verify_chain_empty(self):
        """빈 체인 검증."""
        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain([])

        assert is_valid is True
        assert error is None

    def test_verify_chain_modified_entry(self):
        """변조된 엔트리 감지."""
        entries = self._create_valid_chain(5)
        # 중간 엔트리 변조
        entries[2]["data"] = "TAMPERED"

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is False
        assert "hash mismatch" in error.lower()

    def test_verify_chain_missing_entry(self):
        """누락된 엔트리 감지."""
        entries = self._create_valid_chain(5)
        # 중간 엔트리 삭제
        del entries[2]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is False
        assert "missing" in error.lower() or "expected sequence" in error.lower()

    def test_verify_chain_broken_link(self):
        """끊어진 체인 감지."""
        entries = self._create_valid_chain(5)
        # previous_hash 변조
        entries[3]["integrity"]["previous_hash"] = "FAKE_HASH"

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is False
        assert "broken" in error.lower() or "mismatch" in error.lower()

    def test_find_tampering_all_issues(self):
        """모든 문제 찾기."""
        entries = self._create_valid_chain(10)

        # 여러 문제 생성
        entries[2]["data"] = "TAMPERED"  # 변조
        entries[5]["integrity"]["previous_hash"] = "FAKE"  # 체인 끊김
        del entries[7]  # 삭제 (시퀀스 8)

        verifier = HashChainVerifier()
        issues = verifier.find_tampering(entries)

        assert len(issues) >= 2  # 최소 2개 이상의 문제
        issue_types = [i["type"] for i in issues]
        assert "entry_modified" in issue_types or "chain_broken" in issue_types


class TestAuditIntegrityVerifier:
    """AuditIntegrityVerifier 테스트."""

    def _create_valid_audit_file(
        self, tmpdir: str, filename: str = "audit.jsonl", count: int = 5
    ) -> Path:
        """유효한 감사 로그 파일 생성."""
        manager = HashChainManager()
        file_path = Path(tmpdir) / filename

        with open(file_path, "w") as f:
            for i in range(count):
                entry = manager.add_integrity({"event": f"event_{i}"})
                f.write(json.dumps(entry) + "\n")

        return file_path

    def test_verify_file_valid(self):
        """유효한 파일 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = self._create_valid_audit_file(tmpdir)
            verifier = AuditIntegrityVerifier()

            result = verifier.verify_file(file_path)

            assert result.is_valid is True
            assert result.total_entries == 5
            assert len(result.issues) == 0

    def test_verify_file_not_found(self):
        """존재하지 않는 파일."""
        verifier = AuditIntegrityVerifier()
        result = verifier.verify_file(Path("/nonexistent/file.jsonl"))

        assert result.is_valid is False
        assert "not found" in result.error.lower()

    def test_verify_file_tampered(self):
        """변조된 파일 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = self._create_valid_audit_file(tmpdir, count=5)

            # 파일 읽고 변조
            with open(file_path) as f:
                lines = f.readlines()

            entry = json.loads(lines[2])
            entry["data"] = "TAMPERED"
            lines[2] = json.dumps(entry) + "\n"

            with open(file_path, "w") as f:
                f.writelines(lines)

            verifier = AuditIntegrityVerifier()
            result = verifier.verify_file(file_path)

            assert result.is_valid is False
            assert len(result.issues) > 0

    def test_verify_directory(self):
        """디렉토리 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 여러 파일 생성
            self._create_valid_audit_file(tmpdir, "audit1.jsonl", 3)
            self._create_valid_audit_file(tmpdir, "audit2.jsonl", 5)
            self._create_valid_audit_file(tmpdir, "audit3.jsonl", 2)

            verifier = AuditIntegrityVerifier()
            summary = verifier.verify_directory(Path(tmpdir))

            assert summary.total_files == 3
            assert summary.valid_files == 3
            assert summary.invalid_files == 0
            assert summary.total_entries == 10

    def test_verify_directory_recursive(self):
        """재귀 디렉토리 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 하위 디렉토리 생성
            subdir = Path(tmpdir) / "subdir"
            subdir.mkdir()

            self._create_valid_audit_file(tmpdir, "audit1.jsonl", 2)
            self._create_valid_audit_file(str(subdir), "audit2.jsonl", 3)

            verifier = AuditIntegrityVerifier()

            # 비재귀
            summary_flat = verifier.verify_directory(Path(tmpdir), recursive=False)
            assert summary_flat.total_files == 1

            # 재귀
            summary_recursive = verifier.verify_directory(Path(tmpdir), recursive=True)
            assert summary_recursive.total_files == 2

    def test_verify_directory_with_pattern(self):
        """패턴 매칭 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._create_valid_audit_file(tmpdir, "audit1.jsonl", 2)
            self._create_valid_audit_file(tmpdir, "audit2.jsonl", 2)
            # 다른 확장자 파일
            (Path(tmpdir) / "other.txt").write_text("not audit")

            verifier = AuditIntegrityVerifier()
            summary = verifier.verify_directory(Path(tmpdir), pattern="*.jsonl")

            assert summary.total_files == 2


class TestWALVerification:
    """WAL 검증 테스트."""

    def _create_valid_wal_file(
        self, tmpdir: str, filename: str = "test.wal", count: int = 5
    ) -> Path:
        """유효한 WAL 파일 생성."""
        file_path = Path(tmpdir) / filename

        with open(file_path, "wb") as f:
            for i in range(count):
                entry = {
                    "seq": i + 1,
                    "ts": datetime.now(UTC).timestamp(),
                    "data": {"event": f"event_{i}"},
                }
                entry_bytes = json.dumps(entry, separators=(",", ":")).encode("utf-8")
                checksum = zlib.crc32(entry_bytes) & 0xFFFFFFFF
                checksum_str = f"{checksum:08x}"

                # Format: [4-byte length][checksum:8][entry_bytes]
                record = (
                    struct.pack(">I", len(entry_bytes))
                    + checksum_str.encode("ascii")
                    + entry_bytes
                )
                f.write(record)

        return file_path

    def test_verify_wal_valid(self):
        """유효한 WAL 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            wal_file = self._create_valid_wal_file(tmpdir, count=5)
            verifier = AuditIntegrityVerifier()

            result = verifier._verify_wal_file(wal_file)

            assert result.is_valid is True
            assert result.total_entries == 5

    def test_verify_wal_corrupted_checksum(self):
        """체크섬 손상된 WAL 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            wal_file = self._create_valid_wal_file(tmpdir, count=3)

            # 체크섬 손상
            with open(wal_file, "r+b") as f:
                f.seek(4)  # checksum 위치
                f.write(b"BADCHECK")  # 잘못된 체크섬

            verifier = AuditIntegrityVerifier()
            result = verifier._verify_wal_file(wal_file)

            assert result.is_valid is False
            assert any(i["type"] == "checksum_mismatch" for i in result.issues)

    def test_verify_wal_directory(self):
        """WAL 디렉토리 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._create_valid_wal_file(tmpdir, "wal_001.wal", 3)
            self._create_valid_wal_file(tmpdir, "wal_002.wal", 5)

            verifier = AuditIntegrityVerifier()
            result = verifier.verify_wal_directory(Path(tmpdir))

            assert result.is_valid is True
            assert result.total_entries == 8


class TestOutputFormats:
    """출력 형식 테스트."""

    def _create_test_summary(self) -> VerificationSummary:
        """테스트용 요약 생성."""
        return VerificationSummary(
            total_files=3,
            valid_files=2,
            invalid_files=1,
            error_files=0,
            total_entries=100,
            total_issues=2,
            results=[
                VerificationResult(
                    file_path="/var/log/audit1.jsonl",
                    is_valid=True,
                    total_entries=50,
                ),
                VerificationResult(
                    file_path="/var/log/audit2.jsonl",
                    is_valid=True,
                    total_entries=30,
                ),
                VerificationResult(
                    file_path="/var/log/audit3.jsonl",
                    is_valid=False,
                    total_entries=20,
                    issues=[
                        {
                            "type": "entry_modified",
                            "sequence": 5,
                            "message": "Hash mismatch",
                        },
                        {
                            "type": "chain_broken",
                            "sequence": 10,
                            "message": "Previous hash mismatch",
                        },
                    ],
                ),
            ],
        )

    def test_format_text_output(self):
        """텍스트 형식 출력."""
        summary = self._create_test_summary()
        output = format_text_output(summary)

        assert "Audit Log Integrity Verification Report" in output
        assert "Total Files:   3" in output
        assert "Valid:         2" in output
        assert "Invalid:       1" in output
        assert "Total Entries: 100" in output

    def test_format_text_output_verbose(self):
        """상세 텍스트 출력."""
        summary = self._create_test_summary()
        output = format_text_output(summary, verbose=True)

        assert "audit1.jsonl" in output
        assert "audit2.jsonl" in output
        assert "audit3.jsonl" in output
        assert "Hash mismatch" in output

    def test_format_json_output(self):
        """JSON 형식 출력."""
        summary = self._create_test_summary()
        output = format_json_output(summary)

        data = json.loads(output)
        assert data["summary"]["total_files"] == 3
        assert data["summary"]["valid_files"] == 2
        assert data["summary"]["is_valid"] is False
        assert len(data["results"]) == 3

    def test_format_summary_output(self):
        """요약 형식 출력."""
        summary = self._create_test_summary()
        output = format_summary_output(summary)

        assert "FAIL" in output
        assert "2/3 valid" in output
        assert "100 entries" in output
        assert "2 issues" in output

    def test_format_summary_output_pass(self):
        """통과 요약 출력."""
        summary = VerificationSummary(
            total_files=2,
            valid_files=2,
            invalid_files=0,
            error_files=0,
            total_entries=50,
            total_issues=0,
        )
        output = format_summary_output(summary)

        assert "PASS" in output


class TestVerifyAuditLogIntegrity:
    """verify_audit_log_integrity 함수 테스트."""

    def test_verify_valid_file(self):
        """유효한 파일 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "audit.jsonl"
            manager = HashChainManager()

            with open(file_path, "w") as f:
                for i in range(5):
                    entry = manager.add_integrity({"event": f"event_{i}"})
                    f.write(json.dumps(entry) + "\n")

            is_valid, issues = verify_audit_log_integrity(file_path)

            assert is_valid is True
            assert issues == []

    def test_verify_nonexistent_file(self):
        """존재하지 않는 파일."""
        is_valid, issues = verify_audit_log_integrity(Path("/nonexistent/file.jsonl"))

        assert is_valid is True  # 파일 없으면 valid로 처리
        assert issues == []

    def test_verify_invalid_json(self):
        """잘못된 JSON 파일."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "audit.jsonl"
            file_path.write_text("not valid json\n")

            is_valid, issues = verify_audit_log_integrity(file_path)

            assert is_valid is False
            assert len(issues) > 0
            assert issues[0]["type"] == "read_error"


class TestEdgeCases:
    """엣지 케이스 테스트."""

    def test_single_entry_chain(self):
        """단일 엔트리 체인."""
        manager = HashChainManager()
        entries = [manager.add_integrity({"event": "single"})]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True

    def test_large_chain(self):
        """대용량 체인."""
        manager = HashChainManager()
        entries = [
            manager.add_integrity({"event": f"event_{i}", "data": "x" * 100})
            for i in range(1000)
        ]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True

    def test_unicode_data(self):
        """유니코드 데이터."""
        manager = HashChainManager()
        entries = [
            manager.add_integrity({"event": "한글 테스트", "emoji": "🔒🔐"}),
            manager.add_integrity({"event": "日本語テスト", "data": "中文测试"}),
        ]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True

    def test_nested_data(self):
        """중첩 데이터 구조."""
        manager = HashChainManager()
        entries = [
            manager.add_integrity(
                {
                    "event": "nested",
                    "data": {
                        "level1": {"level2": {"level3": [1, 2, 3, {"key": "value"}]}}
                    },
                }
            )
        ]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True
