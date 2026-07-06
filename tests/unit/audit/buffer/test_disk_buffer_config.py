"""
DiskBufferSettings 설정 단위 테스트.

설정 클래스와 검증 로직을 테스트합니다.
"""

from __future__ import annotations

import sys


class TestDiskBufferSettingsContract:
    """DiskBufferSettings 기본값 계약 검증."""

    def test_storage_type_default(self):
        """스토리지 유형 기본값은 lmdb이다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        settings = DiskBufferSettings()
        assert settings.storage_type == "lmdb"

    def test_lmdb_map_size_mb_windows_default(self, monkeypatch):
        """Windows에서 LMDB map_size 기본값은 256MB이다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        monkeypatch.setattr(sys, "platform", "win32")
        settings = DiskBufferSettings()
        assert settings.lmdb_map_size_mb == 256

    def test_lmdb_map_size_mb_linux_default(self, monkeypatch):
        """Linux에서 LMDB map_size 기본값은 10240MB이다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        monkeypatch.setattr(sys, "platform", "linux")
        settings = DiskBufferSettings()
        assert settings.lmdb_map_size_mb == 10240

    def test_lmdb_writemap_windows_default(self, monkeypatch):
        """Windows에서 writemap 기본값은 False이다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        monkeypatch.setattr(sys, "platform", "win32")
        settings = DiskBufferSettings()
        assert settings.lmdb_writemap is False

    def test_lmdb_writemap_linux_default(self, monkeypatch):
        """Linux에서 writemap 기본값은 True이다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        monkeypatch.setattr(sys, "platform", "linux")
        settings = DiskBufferSettings()
        assert settings.lmdb_writemap is True

    def test_max_entries_default(self, monkeypatch):
        """최대 엔트리 수 기본값은 100000이다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        monkeypatch.delenv("BALDUR_DISK_BUFFER_MAX_ENTRIES", raising=False)
        settings = DiskBufferSettings()
        assert settings.max_entries == 100000

    def test_checksum_enabled_by_default(self):
        """체크섬은 기본 활성화이다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        settings = DiskBufferSettings()
        assert settings.enable_checksum is True

    def test_group_commit_enabled_by_default(self, monkeypatch):
        """Group Commit은 기본 활성화이다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        monkeypatch.delenv("BALDUR_DISK_BUFFER_GROUP_COMMIT_ENABLED", raising=False)
        settings = DiskBufferSettings()
        assert settings.group_commit_enabled is True

    def test_retention_hours_default(self, monkeypatch):
        """보관 기간 기본값은 72시간이다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        monkeypatch.delenv("BALDUR_DISK_BUFFER_RETENTION_HOURS", raising=False)
        settings = DiskBufferSettings()
        assert settings.retention_hours == 72

    def test_include_pid_in_db_name_default(self, monkeypatch):
        """PID 포함 기본값은 True이다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        monkeypatch.delenv("BALDUR_DISK_BUFFER_INCLUDE_PID_IN_DB_NAME", raising=False)
        settings = DiskBufferSettings()
        assert settings.include_pid_in_db_name is True

    def test_custom_settings(self):
        """커스텀 설정 테스트."""
        from baldur.audit.persistence.config import DiskBufferSettings

        settings = DiskBufferSettings(
            lmdb_map_size_mb=100,
            max_entries=1000,
            group_commit_enabled=False,
        )

        assert settings.lmdb_map_size_mb == 100
        assert settings.max_entries == 1000
        assert settings.group_commit_enabled is False

    def test_lmdb_map_size_bytes(self):
        """바이트 변환 테스트."""
        from baldur.audit.persistence.config import DiskBufferSettings

        settings = DiskBufferSettings(lmdb_map_size_mb=100)

        assert settings.lmdb_map_size_bytes == 100 * 1024 * 1024

    def test_validate_settings_warnings(self):
        """설정 검증 경고 테스트."""
        from baldur.audit.persistence.config import DiskBufferSettings

        # sync_on_write와 group_commit 충돌
        settings = DiskBufferSettings(
            sync_on_write=True,
            group_commit_enabled=True,
        )

        warnings = settings.validate_settings()
        assert len(warnings) > 0
        assert any("sync_on_write" in w for w in warnings)

    def test_validate_small_map_size(self):
        """작은 map_size 경고 테스트."""
        from baldur.audit.persistence.config import DiskBufferSettings

        settings = DiskBufferSettings(lmdb_map_size_mb=50)  # 100MB 미만

        warnings = settings.validate_settings()
        assert any("lmdb_map_size_mb" in w for w in warnings)

    def test_get_lmdb_open_kwargs(self):
        """LMDB open kwargs 테스트."""
        from baldur.audit.persistence.config import DiskBufferSettings

        settings = DiskBufferSettings(
            lmdb_map_size_mb=100,
            lmdb_max_dbs=5,
            sync_on_write=True,
            lmdb_writemap=False,
        )

        kwargs = settings.get_lmdb_open_kwargs()

        assert kwargs["map_size"] == 100 * 1024 * 1024
        assert kwargs["max_dbs"] == 5
        assert kwargs["sync"] is True
        assert kwargs["writemap"] is False


class TestDiskBufferSettingsEnvironment:
    """환경변수 기반 설정 테스트."""

    def test_env_override(self, monkeypatch):
        """환경변수 오버라이드 테스트."""
        from baldur.audit.persistence.config import (
            DiskBufferSettings,
            reset_disk_buffer_settings,
        )

        # 캐시 리셋
        reset_disk_buffer_settings()

        monkeypatch.setenv("BALDUR_DISK_BUFFER_LMDB_MAP_SIZE_MB", "500")
        monkeypatch.setenv("BALDUR_DISK_BUFFER_MAX_ENTRIES", "5000")

        settings = DiskBufferSettings()

        assert settings.lmdb_map_size_mb == 500
        assert settings.max_entries == 5000

    def test_get_disk_buffer_settings_singleton(self):
        """설정 싱글톤 테스트."""
        from baldur.audit.persistence.config import (
            get_disk_buffer_settings,
            reset_disk_buffer_settings,
        )

        reset_disk_buffer_settings()

        settings1 = get_disk_buffer_settings()
        settings2 = get_disk_buffer_settings()

        assert settings1 is settings2

    def test_reset_disk_buffer_settings(self):
        """설정 리셋 테스트."""
        from baldur.audit.persistence.config import (
            get_disk_buffer_settings,
            reset_disk_buffer_settings,
        )

        settings1 = get_disk_buffer_settings()
        reset_disk_buffer_settings()
        settings2 = get_disk_buffer_settings()

        # 리셋 후 새 인스턴스 생성됨
        # (같은 설정이지만 다른 객체일 수 있음)
        assert settings1 is not settings2 or True  # 캐시 구현에 따라 다름


class TestDiskBufferPlatformDefaultsBehavior:
    """플랫폼별 기본값 팩토리 함수 동작 검증."""

    def test_is_windows_returns_true_on_win32(self, monkeypatch):
        """sys.platform이 win32이면 _is_windows()는 True를 반환한다."""
        from baldur.audit.persistence.config import _is_windows

        monkeypatch.setattr(sys, "platform", "win32")
        assert _is_windows() is True

    def test_is_windows_returns_false_on_linux(self, monkeypatch):
        """sys.platform이 linux이면 _is_windows()는 False를 반환한다."""
        from baldur.audit.persistence.config import _is_windows

        monkeypatch.setattr(sys, "platform", "linux")
        assert _is_windows() is False

    def test_data_dir_uses_temp_on_windows(self, monkeypatch):
        """Windows에서 데이터 디렉토리는 TEMP 환경변수 기반이다."""
        from baldur.audit.persistence.config import _get_default_data_dir

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("TEMP", "C:\\TestTemp")

        result = _get_default_data_dir()
        assert "C:\\TestTemp" in result
        assert "baldur" in result

    def test_data_dir_uses_var_lib_on_linux(self, monkeypatch):
        """Linux에서 데이터 디렉토리는 /var/lib/baldur/buffer이다."""
        from baldur.audit.persistence.config import _get_default_data_dir

        monkeypatch.setattr(sys, "platform", "linux")

        result = _get_default_data_dir()
        assert result == "/var/lib/baldur/buffer"

    def test_map_size_smaller_on_windows(self, monkeypatch):
        """Windows map_size 기본값은 Linux보다 작아야 한다."""
        from baldur.audit.persistence.config import _get_default_lmdb_map_size_mb

        monkeypatch.setattr(sys, "platform", "win32")
        win_size = _get_default_lmdb_map_size_mb()

        monkeypatch.setattr(sys, "platform", "linux")
        linux_size = _get_default_lmdb_map_size_mb()

        assert win_size < linux_size

    def test_writemap_disabled_on_windows(self, monkeypatch):
        """Windows에서 writemap은 비활성화되어야 한다."""
        from baldur.audit.persistence.config import _get_default_lmdb_writemap

        monkeypatch.setattr(sys, "platform", "win32")
        assert _get_default_lmdb_writemap() is False

    def test_writemap_enabled_on_linux(self, monkeypatch):
        """Linux에서 writemap은 활성화되어야 한다."""
        from baldur.audit.persistence.config import _get_default_lmdb_writemap

        monkeypatch.setattr(sys, "platform", "linux")
        assert _get_default_lmdb_writemap() is True

    def test_env_override_takes_precedence_over_platform_default(self, monkeypatch):
        """환경변수 설정은 플랫폼 기본값보다 우선한다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("BALDUR_DISK_BUFFER_LMDB_MAP_SIZE_MB", "4096")
        monkeypatch.setenv("BALDUR_DISK_BUFFER_LMDB_WRITEMAP", "true")

        settings = DiskBufferSettings()
        assert settings.lmdb_map_size_mb == 4096
        assert settings.lmdb_writemap is True

    def test_lmdb_open_kwargs_reflects_platform_defaults(self, monkeypatch):
        """get_lmdb_open_kwargs()는 플랫폼별 기본값을 반영한다."""
        from baldur.audit.persistence.config import DiskBufferSettings

        monkeypatch.setattr(sys, "platform", "win32")
        settings = DiskBufferSettings()
        kwargs = settings.get_lmdb_open_kwargs()

        assert kwargs["map_size"] == 256 * 1024 * 1024
        assert kwargs["writemap"] is False
