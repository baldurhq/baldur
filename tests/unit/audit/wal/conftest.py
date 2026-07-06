"""
WAL 단위 테스트 공통 fixtures.

test_jsonl.py, test_cleanup.py에서 공유하는 fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def wal_dir(tmp_path: Path) -> Path:
    """WAL 파일용 임시 디렉토리."""
    d = tmp_path / "wal"
    d.mkdir()
    return d


@pytest.fixture
def wal_file(wal_dir: Path) -> Path:
    """WAL JSONL 파일 경로."""
    return wal_dir / "test.jsonl"
