"""
WAL path unification unit tests.

Test targets:
- cascade_auditor.py WAL path constants and methods
- backward-compatibility aliases (fallback -> WAL)

These tests run without Django settings.

Reference:
    audit/cascade_auditor.py
"""


class TestWALPathUnification:
    """WAL path unification tests."""

    def test_cascade_auditor_uses_wal_path(self):
        """cascade_auditor.py uses the WAL path."""
        from baldur.audit.cascade_auditor import (
            LOCAL_CASCADE_FALLBACK_PATH,
            LOCAL_CASCADE_WAL_DIR,
            LOCAL_CASCADE_WAL_PATH,
        )

        # WAL directory path
        assert LOCAL_CASCADE_WAL_DIR == "/var/log/baldur/cascade_wal"

        # WAL file path
        assert "cascade_wal" in LOCAL_CASCADE_WAL_PATH
        assert LOCAL_CASCADE_WAL_PATH.endswith(".jsonl")

        # Backward-compatibility alias
        assert LOCAL_CASCADE_FALLBACK_PATH == LOCAL_CASCADE_WAL_PATH

    def test_auditor_wal_methods_exist(self):
        """CascadeEventAuditor exposes the WAL methods."""
        from baldur.audit.cascade_auditor import CascadeEventAuditor

        auditor = CascadeEventAuditor(enable_load_shedding=False)

        # WAL methods
        assert hasattr(auditor, "_save_to_local_wal")
        assert hasattr(auditor, "_record_dropped_to_wal")
        assert hasattr(auditor, "recover_from_local_wal")
        assert hasattr(auditor, "_remove_namespace_from_wal")

        # Backward-compatibility aliases
        assert hasattr(auditor, "_save_to_local_fallback")
        assert hasattr(auditor, "recover_from_local_fallback")
        assert auditor._save_to_local_fallback == auditor._save_to_local_wal
        assert auditor.recover_from_local_fallback == auditor.recover_from_local_wal
