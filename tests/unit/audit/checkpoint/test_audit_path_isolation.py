"""Unit test for the checkpoint audit-path isolation fixture (663 D8).

The autouse ``_isolate_audit_path`` fixture in this directory's ``conftest.py``
redirects ``BALDUR_AUDIT_PATH`` to a per-test ``tmp_path`` subdir so the file-based
checkpoint storages do not ``mkdir`` the production ``/var/log/audit`` default —
unwritable on the unprivileged GitHub runner the public OSS mirror CI uses (the
default falls to ``tempfile.gettempdir()`` on Windows, which is why the monorepo
Windows run never hit it). This smoke test asserts the redirect is in effect, so a
checkpoint test that constructs a default storage writes under ``tmp_path``.

Test plan source: 663 `## Test Assessment`.
"""

from __future__ import annotations

import os
from pathlib import Path


class TestAuditPathIsolation:
    """663 D8 — the autouse fixture points BALDUR_AUDIT_PATH under tmp_path."""

    def test_audit_path_redirected_under_tmp(self, tmp_path):
        # The autouse fixture and this test share the same function-scoped
        # tmp_path, so the env var must resolve to <tmp_path>/audit.
        value = os.environ.get("BALDUR_AUDIT_PATH")
        assert value is not None, "the autouse _isolate_audit_path fixture did not run"
        assert Path(value) == tmp_path / "audit"
        assert Path(value).is_relative_to(tmp_path)
