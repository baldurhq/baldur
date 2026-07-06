"""
Configuration History & Rollback Service.

Redis에 설정 변경 이력을 저장하고 롤백 기능 제공.

Features:
- 변경 시 자동 버전 저장
- 최근 N개 버전 유지
- 특정 버전으로 롤백
- Redis 장애 시 Graceful Degradation

Usage:
    from baldur.services.config_history import get_config_history_service

    service = get_config_history_service()

    # 버전 저장
    version = service.save_version(
        config_type="circuit_breaker",
        values={"failure_threshold": 10},
        changed_by="admin",
        reason="Increase threshold for high load",
    )

    # 이력 조회
    history = service.get_history("circuit_breaker", limit=10)

    # 롤백
    rolled_back = service.rollback(
        config_type="circuit_breaker",
        target_version=1,
        rolled_back_by="admin",
    )

Audit:
- save_version: log_config_apply_audit(status="applied")
- rollback: log_rollback_audit(state="completed")

Reference:
    92_CONFIG_IMPLEMENTATION_GUIDE.md Week 4 [20] AuditSettings 참조.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.interfaces.config_history_store import ConfigHistoryStore

from baldur.audit.helpers import log_config_apply_audit, log_rollback_audit

from .keys import (
    _get_max_history_entries,
)
from .models import ConfigVersion

logger = structlog.get_logger()


class ConfigHistoryService:
    """
    설정 변경 이력 관리 서비스.

    Features:
    - 변경 시 자동 버전 저장
    - 최근 N개 버전 유지
    - 특정 버전으로 롤백
    - Redis 장애 시 Graceful Degradation
    """

    # 지원하는 config_type 목록
    SUPPORTED_CONFIG_TYPES = [
        "circuit_breaker",
        "dlq",
        "retry",
        "sla",
        "slo",
        "rate_limit",
        "security",
        "idempotency",
        "notification",
        "forensic",
        "metrics",
        "error_budget",
        "drift_threshold",  # Drift 임계값 설정
        "emergency",  # Emergency Mode 설정
        "logging",  # Logging 설정
        "chaos",  # Chaos Engineering 설정
    ]

    def __init__(self, store: ConfigHistoryStore | None = None):
        self._store = store

    @property
    def store(self) -> ConfigHistoryStore | None:
        """ConfigHistoryStore (Lazy loading via ProviderRegistry)."""
        if self._store is None:
            try:
                from baldur.factory import ProviderRegistry

                self._store = ProviderRegistry.config_history_store.get()
            except Exception as e:
                logger.warning(
                    "config_history.store_unavailable",
                    error=e,
                )
        return self._store

    def is_valid_config_type(self, config_type: str) -> bool:
        """유효한 config_type인지 확인."""
        return config_type in self.SUPPORTED_CONFIG_TYPES

    def save_version(
        self,
        config_type: str,
        values: dict[str, Any],
        changed_by: str,
        reason: str = "",
    ) -> ConfigVersion | None:
        """
        새 설정 버전 저장.

        Args:
            config_type: 설정 유형 (circuit_breaker, dlq, retry 등)
            values: 설정 값
            changed_by: 변경자
            reason: 변경 사유

        Returns:
            저장된 ConfigVersion 또는 None (Redis 장애 시)
        """
        if not self.is_valid_config_type(config_type):
            logger.error(
                "config_history.invalid",
                config_type=config_type,
            )
            return None

        if not self.store:
            logger.warning("config_history.store_unavailable_skip_save")
            return None

        try:
            # 새 버전 번호 (원자적 증가)
            version_num = self.store.next_version(config_type)

            # 해시 생성
            config_hash = self._compute_hash(values)

            version = ConfigVersion(
                version=version_num,
                timestamp=time.time(),
                config_type=config_type,
                values=values,
                changed_by=changed_by,
                reason=reason,
                hash=config_hash,
            )

            # 원자적 저장 (history + current)
            max_entries = _get_max_history_entries()
            self.store.save_version(config_type, version.to_dict(), max_entries)

            logger.info(
                "config_history.saved",
                config_type=config_type,
                version_num=version_num,
                changed_by=changed_by,
                reason=reason,
            )

            # === Audit 기록: 설정 버전 저장 ===
            log_config_apply_audit(
                pending_id=None,
                config_key=config_type,
                old_value=None,
                new_value=values,
                status="applied",
                details={
                    "version": version_num,
                    "changed_by": changed_by,
                    "reason": reason,
                    "hash": config_hash,
                },
            )

            return version

        except Exception as e:
            logger.exception(
                "config_history.save_failed",
                error=e,
            )
            return None

    def get_history(self, config_type: str, limit: int = 10) -> list[ConfigVersion]:
        """
        설정 변경 이력 조회.

        Args:
            config_type: 설정 유형
            limit: 조회할 버전 수

        Returns:
            ConfigVersion 목록 (최신순)
        """
        if not self.is_valid_config_type(config_type):
            logger.error(
                "config_history.invalid",
                config_type=config_type,
            )
            return []

        if not self.store:
            logger.warning("config_history.store_unavailable_returning_empty")
            return []

        try:
            max_entries = _get_max_history_entries()
            entries = self.store.get_history(config_type, min(limit, max_entries))

            versions = []
            for data in entries:
                try:
                    versions.append(ConfigVersion.from_dict(data))
                except (ValueError, TypeError) as e:
                    logger.warning(
                        "config_history.parse_entry_failed",
                        error=e,
                    )
                    continue

            return versions

        except Exception as e:
            logger.exception(
                "config_history.get_history_failed",
                error=e,
            )
            return []

    def get_current_version(self, config_type: str) -> ConfigVersion | None:
        """현재 버전 조회."""
        if not self.is_valid_config_type(config_type):
            return None

        if not self.store:
            return None

        try:
            data = self.store.get_current(config_type)
            if data:
                return ConfigVersion.from_dict(data)
            return None

        except Exception as e:
            logger.exception(
                "config_history.get_current_failed",
                error=e,
            )
            return None

    def get_version(self, config_type: str, version: int) -> ConfigVersion | None:
        """특정 버전 조회."""
        history = self.get_history(config_type, limit=_get_max_history_entries())

        for v in history:
            if v.version == version:
                return v

        return None

    def rollback(
        self, config_type: str, target_version: int, rolled_back_by: str
    ) -> ConfigVersion | None:
        """
        특정 버전으로 롤백.

        Note: 이 메서드는 버전 이력만 저장합니다.
        실제 설정 적용은 호출자가 _apply_config_values()를 호출해야 합니다.

        Args:
            config_type: 설정 유형
            target_version: 롤백할 버전 번호
            rolled_back_by: 롤백 수행자

        Returns:
            새로 생성된 롤백 버전 정보 또는 None
        """
        target = self.get_version(config_type, target_version)

        if not target:
            logger.error(
                "config_history.rollback_failed_version_found",
                target_version=target_version,
                config_type=config_type,
            )
            return None

        # 롤백도 새 버전으로 저장
        new_version = self.save_version(
            config_type=config_type,
            values=target.values,
            changed_by=rolled_back_by,
            reason=f"Rollback to version {target_version}",
        )

        if new_version:
            logger.info(
                "config_history.rollback_successful",
                config_type=config_type,
                target_version=target_version,
                new_version=new_version.version,
                rolled_back_by=rolled_back_by,
            )

            # === Audit 기록: 설정 롤백 ===
            log_rollback_audit(
                request_id=f"config-rollback-{config_type}-{new_version.version}",
                service_name=config_type,
                state="completed",
                triggered_by=rolled_back_by,
                reason=f"Rollback to version {target_version}",
                source_version=(
                    str(new_version.version - 1) if new_version.version > 1 else None
                ),
                target_version=str(target_version),
                affected_components=[config_type],
            )

        return new_version

    def compare_versions(
        self, config_type: str, version_a: int, version_b: int
    ) -> dict[str, Any] | None:
        """
        두 버전 간 차이점 비교.

        Returns:
            차이점 딕셔너리 또는 None
        """
        v_a = self.get_version(config_type, version_a)
        v_b = self.get_version(config_type, version_b)

        if not v_a or not v_b:
            return None

        diff: dict[str, Any] = {
            "version_a": version_a,
            "version_b": version_b,
            "config_type": config_type,
            "changes": {},
        }

        all_keys = set(v_a.values.keys()) | set(v_b.values.keys())

        for key in all_keys:
            val_a = v_a.values.get(key)
            val_b = v_b.values.get(key)

            if val_a != val_b:
                diff["changes"][key] = {
                    "from": val_a,
                    "to": val_b,
                }

        return diff

    def get_version_count(self, config_type: str) -> int:
        """저장된 버전 수 조회."""
        if not self.store:
            return 0

        try:
            return self.store.get_version_count(config_type)
        except Exception:
            return 0

    def clear_history(self, config_type: str) -> bool:
        """
        특정 config_type의 이력 삭제 (테스트용).

        WARNING: 프로덕션에서 사용 주의!
        """
        if not self.store:
            return False

        try:
            self.store.clear(config_type)
            logger.warning(
                "config_history.cleared_history",
                config_type=config_type,
            )
            return True

        except Exception as e:
            logger.exception(
                "config_history.clear_failed",
                error=e,
            )
            return False

    def _compute_hash(self, values: dict[str, Any]) -> str:
        """설정값 해시 계산."""
        from baldur.utils.serialization import fast_canonical_dumps

        return hashlib.sha256(fast_canonical_dumps(values)).hexdigest()[:16]


# 싱글톤 인스턴스
_config_history_service: ConfigHistoryService | None = None
_config_history_service_lock = threading.Lock()


def get_config_history_service() -> ConfigHistoryService:
    """ConfigHistoryService 싱글톤 반환."""
    global _config_history_service
    if _config_history_service is None:
        with _config_history_service_lock:
            if _config_history_service is None:
                _config_history_service = ConfigHistoryService()
    return _config_history_service


def reset_config_history_service() -> None:
    """싱글톤 인스턴스 리셋 (테스트용)."""
    global _config_history_service
    _config_history_service = None
