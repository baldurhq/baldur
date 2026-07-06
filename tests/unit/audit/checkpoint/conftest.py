"""Checkpoint/audit storage isolation for this directory's tests.

The file-based checkpoint storages default ``base_path`` to ``/var/log/audit``
on Linux when ``BALDUR_AUDIT_PATH`` is unset, and ``mkdir(parents=True)`` it at
construction. That directory is unwritable on the unprivileged GitHub runner the
public OSS mirror CI uses, so every checkpoint test that constructs a default
storage raises ``PermissionError`` there. On Windows the default already falls to
``tempfile.gettempdir()``, which is why the monorepo Windows run never hit it.

Redirecting ``BALDUR_AUDIT_PATH`` to a per-test ``tmp_path`` subdir isolates all
three storage sites (``audit/checkpoint/file_storage.py``,
``kafka_redis_storage.py``, and ``audit/checkpoint_manager.py`` each read
``BALDUR_AUDIT_PATH`` first). No checkpoint test asserts the OS-default path, so
the override clobbers no intended assertion; ``monkeypatch`` auto-reverts after
each test and the per-test ``tmp_path`` scopes the writes.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_audit_path(monkeypatch, tmp_path):
    """Redirect ``BALDUR_AUDIT_PATH`` to a writable per-test directory."""
    monkeypatch.setenv("BALDUR_AUDIT_PATH", str(tmp_path / "audit"))
