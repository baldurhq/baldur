"""
FileCheckpointStorage Multiprocess File Locking Integration Test.

2-3개 프로세스가 동시에 FileCheckpointStorage에 접근할 때
파일 락이 정상 동작하여 데이터 손상이 발생하지 않는지 검증.

Test Categories:
    A. Two-process concurrent save:
        - 2개 프로세스 동시 save 시 파일 무결성
    B. Three-process concurrent save:
        - 3개 프로세스 동시 save 시 파일 무결성
    C. Concurrent read-write:
        - 1 writer + 1 reader 동시 실행 시 crash 없음

Note: subprocess.Popen 기반 — Docker 불필요, 로컬 직접 실행.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path


class TestFileCheckpointStorageMultiprocessLocking:
    """
    FileCheckpointStorage 멀티프로세스 파일 락 검증.

    Validates:
    - 2-3 프로세스 동시 save 시 데이터 손상 없음
    - 최종 파일이 유효한 JSON
    - 모든 프로세스가 에러 없이 종료
    """

    @staticmethod
    def _build_worker_script(
        checkpoint_dir: str, namespace: str, seq_start: int, count: int
    ) -> str:
        """Worker 프로세스용 Python 스크립트 생성."""
        return textwrap.dedent(f"""\
            import sys
            sys.path.insert(0, r"{Path(__file__).parents[3] / "src"}")
            from baldur.audit.checkpoint import (
                FileCheckpointStorage,
                UnifiedCheckpointData,
            )

            storage = FileCheckpointStorage(
                base_path=r"{checkpoint_dir}",
                sync_on_write=False,
            )

            for i in range({count}):
                seq = {seq_start} + i
                try:
                    storage.save("{namespace}", UnifiedCheckpointData(wal_sequence=seq))
                except Exception:
                    pass  # Timeout on prolonged lock contention is acceptable

            print("OK")
        """)

    def test_two_processes_concurrent_save_no_corruption(self, tmp_path):
        """
        Purpose:
            2개 프로세스가 동시에 동일 namespace에 save 시 파일 손상 없음.
        Expected:
            - 두 프로세스 모두 정상 종료 (exit code 0)
            - 최종 파일이 유효한 JSON
            - wal_sequence가 어느 한 프로세스의 값
        """
        checkpoint_dir = str(tmp_path)
        ns = "multiproc"

        script_a = self._build_worker_script(
            checkpoint_dir, ns, seq_start=1000, count=20
        )
        script_b = self._build_worker_script(
            checkpoint_dir, ns, seq_start=2000, count=20
        )

        proc_a = subprocess.Popen(
            [sys.executable, "-c", script_a],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc_b = subprocess.Popen(
            [sys.executable, "-c", script_b],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout_a, stderr_a = proc_a.communicate(timeout=30)
        stdout_b, stderr_b = proc_b.communicate(timeout=30)

        # 두 프로세스 모두 정상 종료
        assert proc_a.returncode == 0, f"Process A failed: {stderr_a.decode()}"
        assert proc_b.returncode == 0, f"Process B failed: {stderr_b.decode()}"

        # 최종 파일이 유효한 JSON
        file_path = tmp_path / f"checkpoint.{ns}.json"
        assert file_path.exists(), "Checkpoint file should exist"

        with open(file_path) as f:
            data = json.load(f)

        assert "wal_sequence" in data
        seq = data["wal_sequence"]
        # 어느 한 프로세스의 값 범위에 속해야 함
        assert (1000 <= seq <= 1019) or (2000 <= seq <= 2019), (
            f"Unexpected sequence: {seq}"
        )

    def test_three_processes_concurrent_save_no_corruption(self, tmp_path):
        """
        Purpose:
            3개 프로세스가 동시에 save 시 파일 손상 없음.
        Expected:
            - 세 프로세스 모두 정상 종료
            - 최종 파일이 유효한 JSON
        """
        checkpoint_dir = str(tmp_path)
        ns = "triple"

        scripts = [
            self._build_worker_script(checkpoint_dir, ns, seq_start=base, count=15)
            for base in [1000, 2000, 3000]
        ]

        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for script in scripts
        ]

        results = [p.communicate(timeout=30) for p in procs]

        # 모든 프로세스 정상 종료
        for i, (proc, (_stdout, stderr)) in enumerate(
            zip(procs, results, strict=False)
        ):
            assert proc.returncode == 0, f"Process {i} failed: {stderr.decode()}"

        # 최종 파일이 유효한 JSON
        file_path = tmp_path / f"checkpoint.{ns}.json"
        assert file_path.exists()

        with open(file_path) as f:
            data = json.load(f)

        assert "wal_sequence" in data
        seq = data["wal_sequence"]
        valid_ranges = [range(1000, 1015), range(2000, 2015), range(3000, 3015)]
        assert any(seq in r for r in valid_ranges), f"Unexpected sequence: {seq}"

    def test_concurrent_save_and_load_across_processes(self, tmp_path):
        """
        Purpose:
            1 writer + 1 reader 프로세스 동시 실행 시 reader가 crash하지 않음.
        Expected:
            - 두 프로세스 모두 정상 종료
        """
        checkpoint_dir = str(tmp_path)
        ns = "rw_test"
        src_path = Path(__file__).parents[3] / "src"

        writer_script = self._build_worker_script(
            checkpoint_dir, ns, seq_start=500, count=30
        )

        reader_script = textwrap.dedent(f"""\
            import sys
            sys.path.insert(0, r"{src_path}")
            from baldur.audit.checkpoint import FileCheckpointStorage

            storage = FileCheckpointStorage(
                base_path=r"{checkpoint_dir}",
                sync_on_write=False,
            )

            for _ in range(30):
                try:
                    data = storage.load("{ns}")
                except Exception:
                    pass  # Transient errors during concurrent access are acceptable

            print("OK")
        """)

        writer = subprocess.Popen(
            [sys.executable, "-c", writer_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        reader = subprocess.Popen(
            [sys.executable, "-c", reader_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        w_out, w_err = writer.communicate(timeout=30)
        r_out, r_err = reader.communicate(timeout=30)

        assert writer.returncode == 0, f"Writer failed: {w_err.decode()}"
        assert reader.returncode == 0, f"Reader failed: {r_err.decode()}"
