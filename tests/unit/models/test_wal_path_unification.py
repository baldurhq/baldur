"""
WAL 경로 통일 단위 테스트.

테스트 대상:
- cascade_auditor.py WAL 경로 상수 및 메서드
- cascade_cleanup_tasks.py WAL 경로 상수 및 함수
- 하위 호환성 별칭 (fallback → WAL)

이 테스트는 Django 설정 없이 실행 가능합니다.

Reference:
    audit/cascade_auditor.py
    tasks/cascade_cleanup_tasks.py
"""


class TestWALPathUnification:
    """WAL 경로 통일 테스트."""

    def test_cascade_auditor_uses_wal_path(self):
        """cascade_auditor.py가 WAL 경로 사용."""
        from baldur.audit.cascade_auditor import (
            LOCAL_CASCADE_FALLBACK_PATH,
            LOCAL_CASCADE_WAL_DIR,
            LOCAL_CASCADE_WAL_PATH,
        )

        # WAL 디렉토리 경로 확인
        assert LOCAL_CASCADE_WAL_DIR == "/var/log/baldur/cascade_wal"

        # WAL 파일 경로 확인
        assert "cascade_wal" in LOCAL_CASCADE_WAL_PATH
        assert LOCAL_CASCADE_WAL_PATH.endswith(".jsonl")

        # 하위 호환성 별칭 확인
        assert LOCAL_CASCADE_FALLBACK_PATH == LOCAL_CASCADE_WAL_PATH

    def test_cascade_cleanup_tasks_uses_wal_path(self):
        """cascade_cleanup_tasks.py가 WAL 경로 사용."""
        from baldur.tasks.cascade_cleanup_tasks import (
            LOCAL_CASCADE_WAL_DIR,
            LOCAL_CASCADE_WAL_PATH,
            LOCAL_FALLBACK_PATH,
        )

        # WAL 디렉토리 경로 확인
        assert "cascade_wal" in str(LOCAL_CASCADE_WAL_DIR)

        # 하위 호환성 별칭 확인
        assert LOCAL_FALLBACK_PATH == LOCAL_CASCADE_WAL_PATH

    def test_auditor_wal_methods_exist(self):
        """CascadeEventAuditor에 WAL 메서드 존재."""
        from baldur.audit.cascade_auditor import CascadeEventAuditor

        auditor = CascadeEventAuditor(enable_load_shedding=False)

        # WAL 메서드 확인
        assert hasattr(auditor, "_save_to_local_wal")
        assert hasattr(auditor, "_record_dropped_to_wal")
        assert hasattr(auditor, "recover_from_local_wal")
        assert hasattr(auditor, "_remove_namespace_from_wal")

        # 하위 호환성 별칭 확인
        assert hasattr(auditor, "_save_to_local_fallback")
        assert hasattr(auditor, "recover_from_local_fallback")
        assert auditor._save_to_local_fallback == auditor._save_to_local_wal
        assert auditor.recover_from_local_fallback == auditor.recover_from_local_wal

    def test_cleanup_tasks_wal_functions_exist(self):
        """cascade_cleanup_tasks에 WAL 함수 존재."""
        from baldur.tasks.cascade_cleanup_tasks import (
            _remove_namespace_from_fallback,
            _remove_namespace_from_wal,
            recover_cascade_from_fallback,
            recover_cascade_from_wal,
        )

        # WAL 함수 확인
        assert callable(recover_cascade_from_wal)
        assert callable(_remove_namespace_from_wal)

        # 하위 호환성 별칭 확인
        assert recover_cascade_from_fallback == recover_cascade_from_wal
        assert _remove_namespace_from_fallback == _remove_namespace_from_wal
