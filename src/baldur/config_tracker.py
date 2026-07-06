"""
Configuration Change Tracker

설정 변경 시 자동으로 감사 로그를 남기는 유틸리티.

문제점 (사용자 우려):
- 설정을 변경했는데 캐시 때문에 적용이 안 됨
- 누가 언제 어떤 설정을 변경했는지 추적 불가
- 대형 사고 발생 시 원인 분석 어려움

해결책:
- 설정 변경 전/후 값을 자동 기록
- 누가 언제 변경했는지 ActorContext에서 자동 추출
- 캐시 무효화 여부도 기록

Usage:
    from baldur.config_tracker import ConfigChangeTracker

    tracker = ConfigChangeTracker(audit_adapter)

    # 설정 변경 추적
    with tracker.track_change(
        config_key="circuit_breaker.external_api.threshold",
        old_value=10,
        new_value=50,
        reason="트래픽 증가에 따른 임계값 조정",
    ):
        # 실제 설정 변경
        update_config("circuit_breaker.external_api.threshold", 50)

    # 또는 데코레이터로
    @tracker.track_config_change("service.timeout")
    def update_service_timeout(new_value):
        settings.SERVICE_TIMEOUT = new_value
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

import structlog

from baldur.context.actor_context import ActorContext
from baldur.core.serializable import SerializableMixin
from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()

T = TypeVar("T")


@dataclass
class ConfigChange(SerializableMixin):
    """설정 변경 정보."""

    config_key: str
    old_value: Any
    new_value: Any
    reason: str | None = None
    changed_at: datetime | None = None
    changed_by: str | None = None
    applied: bool = False
    cache_invalidated: bool = False
    error_message: str | None = None

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Convert old_value/new_value to str (original behavior)."""
        data["old_value"] = str(self.old_value)
        data["new_value"] = str(self.new_value)
        return super()._post_serialize(data)


class ConfigChangeTracker:
    """
    설정 변경을 추적하고 감사 로그를 남기는 클래스.

    Features:
    - 변경 전/후 값 기록
    - 누가 변경했는지 자동 추적 (ActorContext 사용)
    - 캐시 무효화 여부 기록
    - 변경 실패 시에도 시도 기록
    """

    def __init__(
        self,
        audit_adapter: AuditLogAdapter,
        auto_log: bool = True,
    ):
        """
        Initialize ConfigChangeTracker.

        Args:
            audit_adapter: Audit log adapter for recording changes
            auto_log: If True, automatically log changes (default: True)
        """
        self.audit_adapter = audit_adapter
        self.auto_log = auto_log

    @contextmanager
    def track_change(
        self,
        config_key: str,
        old_value: Any,
        new_value: Any,
        reason: str | None = None,
        invalidate_cache_fn: Callable[[], None] | None = None,
    ) -> Generator[ConfigChange, None, None]:
        """
        Track a configuration change.

        Usage:
            with tracker.track_change(
                config_key="circuit_breaker.threshold",
                old_value=10,
                new_value=50,
                reason="트래픽 증가",
            ) as change:
                update_config(...)

            # change.applied will be True if no exception
        """
        # Get current actor
        actor = ActorContext.get_current()

        change = ConfigChange(
            config_key=config_key,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            changed_at=utc_now(),
            changed_by=actor.actor_id,
        )

        # Log before change (pre-action logging)
        logger.info(
            "config_change_tracker.changing",
            config_key=config_key,
            old_value=old_value,
            new_value=new_value,
            actor_id=actor.actor_id,
            reason=reason,
        )

        try:
            yield change

            # Change succeeded
            change.applied = True

            # Invalidate cache if function provided
            if invalidate_cache_fn:
                try:
                    invalidate_cache_fn()
                    change.cache_invalidated = True
                    logger.info(
                        "config_change_tracker.cache_invalidated",
                        config_key=config_key,
                    )
                except Exception as e:
                    logger.exception(
                        "config_change_tracker.cache_invalidation_failed",
                        error=e,
                    )
                    change.cache_invalidated = False

            # Log success
            if self.auto_log:
                self._log_change(change, success=True)

        except Exception as e:
            # Change failed
            change.applied = False
            change.error_message = str(e)

            # Log failure
            if self.auto_log:
                self._log_change(change, success=False)

            raise

    def _log_change(
        self,
        change: ConfigChange,
        success: bool,
        request: Any = None,
    ) -> None:
        """
        Log configuration change to audit adapter.

        request 파라미터 추가하여 AuditMiddleware 버퍼 패턴 지원
        """
        # === 버퍼 패턴 우선 ===
        if request is not None:
            try:
                from baldur.audit.event_buffer import (
                    AuditEventType,
                    RequestAuditBuffer,
                )

                buffer = RequestAuditBuffer.get_or_create(request)
                buffer.add(
                    event_type=AuditEventType.CONFIG_CHANGE,
                    source="ConfigChangeTracker",
                    details={
                        "config_key": change.config_key,
                        "old_value": str(change.old_value),
                        "new_value": str(change.new_value),
                        "cache_invalidated": change.cache_invalidated,
                        "reason": change.reason,
                    },
                    success=success,
                    error_message=change.error_message,
                    target_id=change.config_key,
                )
                return  # 버퍼에 추가됨 - AuditMiddleware에서 기록
            except ImportError:
                pass  # event_buffer 사용 불가 - fallback

        # === Fallback: 기존 방식 ===
        entry = AuditEntry(
            action=AuditAction.CONFIG_CHANGE,
            target_type="config",
            target_id=change.config_key,
            reason=change.reason,
            success=success,
            error_message=change.error_message,
            details={
                "old_value": str(change.old_value),
                "new_value": str(change.new_value),
                "cache_invalidated": change.cache_invalidated,
            },
        )

        self.audit_adapter.log(entry)

    def log_manual_override(
        self,
        config_key: str,
        new_value: Any,
        reason: str,
        override_type: str = "config",
        request: Any = None,
    ) -> None:
        """
        Log a manual override action.

        Use this for one-off overrides that bypass normal config flow.

        request 파라미터 추가하여 AuditMiddleware 버퍼 패턴 지원
        """
        # === 버퍼 패턴 우선 ===
        if request is not None:
            try:
                from baldur.audit.event_buffer import (
                    AuditEventType,
                    RequestAuditBuffer,
                )

                buffer = RequestAuditBuffer.get_or_create(request)
                buffer.add(
                    event_type=AuditEventType.MANUAL_OVERRIDE,
                    source="ConfigChangeTracker",
                    details={
                        "config_key": config_key,
                        "new_value": str(new_value),
                        "override_type": override_type,
                        "reason": reason,
                    },
                    success=True,
                    target_id=config_key,
                )
                logger.warning(
                    "config_change_tracker.event",
                    override_type=override_type,
                    config_key=config_key,
                    new_value=new_value,
                    reason=reason,
                )
                return  # 버퍼에 추가됨 - AuditMiddleware에서 기록
            except ImportError:
                pass  # event_buffer 사용 불가 - fallback

        # === Fallback: 기존 방식 ===
        entry = AuditEntry(
            action=AuditAction.MANUAL_OVERRIDE,
            target_type=override_type,
            target_id=config_key,
            reason=reason,
            details={
                "new_value": str(new_value),
            },
        )

        self.audit_adapter.log(entry)
        logger.warning(
            "config_change_tracker.event",
            override_type=override_type,
            config_key=config_key,
            new_value=new_value,
            reason=reason,
        )


# Singleton instance for easy use
_default_tracker: ConfigChangeTracker | None = None


def get_config_tracker() -> ConfigChangeTracker | None:
    """Get the default config change tracker."""
    return _default_tracker


def set_config_tracker(tracker: ConfigChangeTracker) -> None:
    """Set the default config change tracker."""
    global _default_tracker
    _default_tracker = tracker
