"""
L1 Metric Snapshot Storage.

Provides local file-based snapshot storage for metric values.
This is the "last resort" fallback when all other data sources fail.

Design Philosophy:
- "Last Known Good" (LKG) pattern
- Atomic writes using Write-to-Temp-and-Rename
- Non-blocking: failures don't affect system operation
- Age tracking for data freshness indication

Fallback Hierarchy:
1. Real-time Push Events
2. DB Query (Manual Sync)
3. Redis Air-Gap
4. L1 Local Snapshot ← This module
5. Safe Defaults (Emergency)
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from baldur.core.file_utils import safe_unlink
from baldur.core.serializable import SerializableMixin

logger = structlog.get_logger()


def _get_snapshot_max_age() -> int:
    """Read snapshot_max_age from MetricsSettings via the layered provider.

    Layered read (686 D1/D5) so a console edit of the metrics domain takes
    effect; env base when no RuntimeConfigManager is registered.
    """
    try:
        from baldur.settings.layered_provider import get_layered_settings
        from baldur.settings.metrics import MetricsSettings

        return get_layered_settings(MetricsSettings, "metrics").snapshot_max_age
    except Exception:
        return 3600  # 1 hour fallback


@dataclass
class MetricSnapshot(SerializableMixin):
    """메트릭 스냅샷 데이터."""

    # 메트릭 값들 (도메인별)
    values: dict[str, dict[str, Any]] = field(default_factory=dict)

    # 메타데이터
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: str = "1.0"
    source: str = "unknown"

    @property
    def age_seconds(self) -> float:
        """스냅샷 나이 (초)."""
        return time.time() - self.updated_at

    def get_value(self, category: str, key: str, default: Any = None) -> Any:
        """
        스냅샷에서 값 조회.

        Args:
            category: 카테고리 (예: "dlq_pending", "circuit_breaker")
            key: 키 (예: "payment", "toss")
            default: 기본값

        Returns:
            저장된 값 또는 기본값
        """
        if category not in self.values:
            return default
        return self.values[category].get(key, default)

    def set_value(self, category: str, key: str, value: Any) -> None:
        """
        스냅샷에 값 저장.

        Args:
            category: 카테고리
            key: 키
            value: 값
        """
        if category not in self.values:
            self.values[category] = {}
        self.values[category][key] = value
        self.updated_at = time.time()


class MetricSnapshotStorage:
    """
    L1 로컬 파일 스냅샷 저장소.

    메트릭 값을 로컬 파일에 주기적으로 저장하여,
    모든 데이터 소스가 실패해도 "마지막 알려진 값"을 사용할 수 있게 합니다.

    Features:
    - Atomic writes: Write-to-Temp-and-Rename 패턴
    - Non-blocking: 실패해도 시스템 동작에 영향 없음
    - Age tracking: 스냅샷 나이 추적
    - Thread-safe: 동시 접근 안전

    Example:
        >>> storage = MetricSnapshotStorage("/var/lib/baldur/metrics")
        >>>
        >>> # 스냅샷 저장
        >>> storage.save_value("dlq_pending", "payment", 5)
        >>>
        >>> # 스냅샷 로드
        >>> value = storage.load_value("dlq_pending", "payment")
        >>> age = storage.get_snapshot_age()
    """

    DEFAULT_FILENAME = "last_known_metrics.json"
    DEFAULT_MAX_AGE = 3600  # 하위 호환성용 레거시 상수

    def __init__(
        self,
        storage_dir: str | None = None,
        filename: str = DEFAULT_FILENAME,
        max_age_seconds: float | None = None,
    ):
        """
        Initialize MetricSnapshotStorage.

        Args:
            storage_dir: 저장 디렉토리 (None이면 기본 위치 사용)
            filename: 스냅샷 파일명
            max_age_seconds: 스냅샷 최대 유효 기간 (초). None이면 Settings에서 가져옴.
        """
        self._storage_dir = (
            Path(storage_dir) if storage_dir else self._get_default_dir()
        )
        self._filename = filename
        self._max_age = (
            max_age_seconds if max_age_seconds is not None else _get_snapshot_max_age()
        )
        self._lock = threading.Lock()
        self._snapshot: MetricSnapshot | None = None
        self._dirty = False

        # 디렉토리 생성
        self._ensure_directory()

        # 기존 스냅샷 로드
        self._load_snapshot()

    @property
    def file_path(self) -> Path:
        """스냅샷 파일 경로."""
        return self._storage_dir / self._filename

    @property
    def snapshot(self) -> MetricSnapshot | None:
        """현재 스냅샷."""
        return self._snapshot

    def _get_default_dir(self) -> Path:
        """기본 저장 디렉토리."""
        try:
            from baldur.settings.metrics import get_metrics_settings

            snapshot_dir = get_metrics_settings().snapshot_dir
            if snapshot_dir:
                return Path(snapshot_dir)
        except Exception:
            pass

        # 기본: 현재 디렉토리 하위
        return Path.cwd() / ".baldur"

    def _ensure_directory(self) -> None:
        """디렉토리 존재 확인 및 생성."""
        try:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(
                "snapshot.create_directory_failed",
                storage_dir=self._storage_dir,
                error=e,
            )

    def _load_snapshot(self) -> None:
        """파일에서 스냅샷 로드."""
        try:
            if not self.file_path.exists():
                self._snapshot = MetricSnapshot(source="new")
                logger.debug("snapshot_storage.initialized")
                return

            with open(self.file_path, encoding="utf-8") as f:
                data = json.load(f)

            self._snapshot = MetricSnapshot.from_dict(data)
            logger.info(
                "snapshot.loaded_snapshot_age_categories",
                snapshot_age_seconds=self._snapshot.age_seconds,
                values_count=len(self._snapshot.values),
            )
        except Exception as e:
            logger.warning(
                "snapshot.load_snapshot_failed",
                error=e,
            )
            self._snapshot = MetricSnapshot(source="new_after_error")

    def _save_snapshot_atomic(self) -> bool:
        """
        원자적으로 스냅샷 저장.

        Write-to-Temp-and-Rename 패턴 사용:
        1. 임시 파일에 쓰기
        2. fsync로 디스크 동기화
        3. 원래 파일명으로 rename (원자적 연산)

        Returns:
            성공 여부
        """
        if self._snapshot is None:
            return False

        try:
            # 임시 파일 생성 (같은 디렉토리에)
            fd, temp_path = tempfile.mkstemp(
                suffix=".tmp",
                prefix="snapshot_",
                dir=self._storage_dir,
            )

            try:
                # JSON 쓰기
                data = self._snapshot.to_dict()
                json_str = json.dumps(data, indent=2, ensure_ascii=False)

                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json_str)
                    f.flush()
                    os.fsync(f.fileno())

                # 원자적 rename
                os.replace(temp_path, self.file_path)

                self._dirty = False
                logger.debug(
                    "snapshot.saved_snapshot",
                    file_path=self.file_path,
                )
                return True

            except Exception:
                # 임시 파일 정리
                safe_unlink(Path(temp_path))
                raise

        except Exception as e:
            logger.warning(
                "snapshot.save_snapshot_failed",
                error=e,
            )
            return False

    def save_value(
        self,
        category: str,
        key: str,
        value: Any,
        immediate: bool = False,
    ) -> bool:
        """
        값 저장.

        Args:
            category: 카테고리 (예: "dlq_pending", "circuit_breaker")
            key: 키 (예: "payment", "toss")
            value: 값
            immediate: 즉시 디스크에 저장할지 여부

        Returns:
            성공 여부
        """
        with self._lock:
            if self._snapshot is None:
                self._snapshot = MetricSnapshot(source="save")

            self._snapshot.set_value(category, key, value)
            self._dirty = True

            if immediate:
                return self._save_snapshot_atomic()
            return True

    def save_bulk(
        self,
        values: dict[str, dict[str, Any]],
        source: str = "bulk",
    ) -> bool:
        """
        여러 값 일괄 저장.

        Args:
            values: {category: {key: value}} 형태의 딕셔너리
            source: 저장 소스 식별자

        Returns:
            성공 여부
        """
        with self._lock:
            if self._snapshot is None:
                self._snapshot = MetricSnapshot(source=source)

            for category, items in values.items():
                for key, value in items.items():
                    self._snapshot.set_value(category, key, value)

            self._snapshot.source = source
            self._dirty = True
            return self._save_snapshot_atomic()

    def load_value(
        self,
        category: str,
        key: str,
        default: Any = None,
        max_age: float | None = None,
    ) -> Any:
        """
        값 로드.

        Args:
            category: 카테고리
            key: 키
            default: 기본값
            max_age: 최대 허용 나이 (초), None이면 기본값 사용

        Returns:
            저장된 값 또는 기본값 (너무 오래된 경우도 기본값)
        """
        with self._lock:
            if self._snapshot is None:
                return default

            effective_max_age = max_age if max_age is not None else self._max_age

            if self._snapshot.age_seconds > effective_max_age:
                logger.debug(
                    "snapshot.value_too_old",
                    snapshot_age_seconds=self._snapshot.age_seconds,
                    effective_max_age=effective_max_age,
                )
                return default

            return self._snapshot.get_value(category, key, default)

    def load_all(self, max_age: float | None = None) -> dict[str, dict[str, Any]]:
        """
        모든 값 로드.

        Args:
            max_age: 최대 허용 나이 (초)

        Returns:
            저장된 모든 값 또는 빈 딕셔너리
        """
        with self._lock:
            if self._snapshot is None:
                return {}

            effective_max_age = max_age if max_age is not None else self._max_age

            if self._snapshot.age_seconds > effective_max_age:
                return {}

            return dict(self._snapshot.values)

    def get_snapshot_age(self) -> float | None:
        """
        스냅샷 나이 조회.

        Returns:
            스냅샷 나이 (초) 또는 None
        """
        with self._lock:
            if self._snapshot is None:
                return None
            return self._snapshot.age_seconds

    def get_snapshot_info(self) -> dict[str, Any]:
        """
        스냅샷 정보 조회.

        Returns:
            스냅샷 메타데이터
        """
        with self._lock:
            if self._snapshot is None:
                return {"exists": False}

            return {
                "exists": True,
                "age_seconds": self._snapshot.age_seconds,
                "created_at": self._snapshot.created_at,
                "updated_at": self._snapshot.updated_at,
                "source": self._snapshot.source,
                "categories": list(self._snapshot.values.keys()),
                "is_valid": self._snapshot.age_seconds <= self._max_age,
                "file_path": str(self.file_path),
            }

    def flush(self) -> bool:
        """
        변경사항 디스크에 저장.

        Returns:
            성공 여부
        """
        with self._lock:
            if not self._dirty:
                return True
            return self._save_snapshot_atomic()

    def clear(self) -> bool:
        """
        스냅샷 초기화.

        Returns:
            성공 여부
        """
        with self._lock:
            self._snapshot = MetricSnapshot(source="cleared")
            self._dirty = True
            return self._save_snapshot_atomic()


# =============================================================================
# Singleton Instance
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_snapshot_storage, configure_snapshot_storage, reset_snapshot_storage = (
    make_singleton_factory("snapshot_storage", MetricSnapshotStorage)
)


__all__ = [
    "MetricSnapshot",
    "MetricSnapshotStorage",
    "configure_snapshot_storage",
    "get_snapshot_storage",
    "reset_snapshot_storage",
]
