"""Shared fixtures for the WAL unit tests.

``wal_dir`` / ``wal_file`` are shared by ``test_jsonl.py`` and
``test_cleanup.py``; ``capturing_adapter`` is shared by
``test_wal_meta_event_delivery.py`` and ``test_wal_corruption_reporting.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.factories.audit_adapters import CapturingAuditAdapter


@pytest.fixture
def wal_dir(tmp_path: Path) -> Path:
    """Temporary directory for WAL files."""
    d = tmp_path / "wal"
    d.mkdir()
    return d


@pytest.fixture
def wal_file(wal_dir: Path) -> Path:
    """Path of the WAL JSONL file."""
    return wal_dir / "test.jsonl"


@pytest.fixture
def capturing_adapter() -> CapturingAuditAdapter:
    """A fresh capturing audit adapter per test."""
    return CapturingAuditAdapter()
