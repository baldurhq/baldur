"""
EventCalendar — 예정 이벤트 등록/관리/스케줄링.

인메모리 dict + StateBackend 영속화 (Pull + Push 하이브리드).
이벤트 수가 많지 않으므로 (일 수십 건 이하) 인메모리가 런타임 캐시이고,
StateBackend(Redis/File)가 SSOT(단일 진실 공급원)이다.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from threading import Lock
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.settings.capacity_reservation import (
    CapacityReservationSettings,
    get_capacity_reservation_settings,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()

STATE_KEY_EVENTS = "capacity_reservation:events"


class EventStatus(str, Enum):
    """예정 이벤트 상태."""

    PENDING = "pending"
    WARMING = "warming"
    ACTIVE = "active"
    COOLING_DOWN = "cooling_down"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class ScheduledEvent(SerializableMixin):
    """예정 이벤트 정의."""

    name: str
    start_time: datetime
    end_time: datetime
    expected_rps_multiplier: float = 2.0
    pool_multiplier: float = 1.5
    bulkhead_extra_permits: int = 50
    suppress_degradation: bool = True
    warmup_minutes: int = 5
    tags: list[str] = field(default_factory=list)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: EventStatus = EventStatus.PENDING

    def __post_init__(self) -> None:
        if self.start_time.tzinfo is None:
            self.start_time = self.start_time.replace(tzinfo=UTC)
        if self.end_time.tzinfo is None:
            self.end_time = self.end_time.replace(tzinfo=UTC)

    @property
    def warmup_time(self) -> datetime:
        """워밍 시작 시각."""
        return self.start_time - timedelta(minutes=self.warmup_minutes)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledEvent:
        """역직렬화."""
        return cls(
            name=data["name"],
            start_time=datetime.fromisoformat(data["start_time"]),
            end_time=datetime.fromisoformat(data["end_time"]),
            expected_rps_multiplier=data.get("expected_rps_multiplier", 2.0),
            pool_multiplier=data.get("pool_multiplier", 1.5),
            bulkhead_extra_permits=data.get("bulkhead_extra_permits", 50),
            suppress_degradation=data.get("suppress_degradation", True),
            warmup_minutes=data.get("warmup_minutes", 5),
            tags=data.get("tags", []),
            event_id=data["event_id"],
            status=EventStatus(data.get("status", "pending")),
        )

    def to_event_context(self) -> dict:
        """EventBus/ML context용 메타데이터."""
        return {
            "event_id": self.event_id,
            "name": self.name,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "expected_rps_multiplier": self.expected_rps_multiplier,
            "tags": self.tags,
            "scheduled_event": True,
        }


@dataclass
class EffectiveMultipliers:
    """활성 이벤트들의 병합된 유효 배율."""

    rate_multiplier: float
    pool_multiplier: float
    bulkhead_extra_permits: int
    suppress_degradation: bool
    source_event_ids: list[str]


class EventCalendar:
    """예정 이벤트 캘린더 — 등록/조회/스케줄링."""

    def __init__(
        self,
        state_backend: Any | None = None,
        settings: CapacityReservationSettings | None = None,
        cache_ttl_seconds: int = 30,
    ) -> None:
        self._events: dict[str, ScheduledEvent] = {}
        self._lock = Lock()
        self._state_backend = state_backend
        self._settings = settings or get_capacity_reservation_settings()
        self._cache_ttl_seconds = cache_ttl_seconds
        self._last_load_time: float | None = None

    def initialize(self) -> None:
        """Pod 기동 시 StateBackend에서 활성 이벤트 로드 (Pull)."""
        if not self._state_backend:
            return
        try:
            saved = self._state_backend.get(STATE_KEY_EVENTS)
            if saved:
                self._events = self._deserialize(saved)
                self._last_load_time = time.monotonic()
                logger.info(
                    "capacity_reservation.calendar_initialized",
                    event_count=len(self._events),
                )
        except Exception as exc:
            logger.exception(
                "capacity_reservation.calendar_init_failed",
                error=str(exc),
            )

    def register(self, event: ScheduledEvent) -> None:
        """이벤트 등록. 시작 시간이 과거이면 ValueError."""
        now = utc_now()
        if event.start_time <= now:
            raise ValueError(
                f"Event start time is in the past: {event.start_time.isoformat()}"
            )
        if event.end_time <= event.start_time:
            raise ValueError(
                f"End time is before start time: "
                f"start={event.start_time.isoformat()}, end={event.end_time.isoformat()}"
            )

        with self._lock:
            if event.event_id in self._events:
                raise ValueError(f"Duplicate event ID: {event.event_id}")

            overlapping = self._find_overlapping(event)
            if overlapping:
                logger.warning(
                    "capacity_reservation.event_overlap",
                    new_event=event.event_id,
                    overlapping=[e.event_id for e in overlapping],
                )

            self._events[event.event_id] = event
            self._persist()

        logger.info(
            "capacity_reservation.event_registered",
            event_id=event.event_id,
            name=event.name,
            start_time=event.start_time.isoformat(),
            end_time=event.end_time.isoformat(),
            warmup_minutes=event.warmup_minutes,
        )

    def cancel(self, event_id: str) -> bool:
        """이벤트 취소. 존재하지 않으면 False."""
        with self._lock:
            event = self._events.get(event_id)
            if event is None:
                return False
            previous_status = event.status.value
            event.status = EventStatus.CANCELLED
            self._persist()

        logger.info(
            "capacity_reservation.event_cancelled",
            event_id=event_id,
            previous_status=previous_status,
        )
        return True

    def get_upcoming(self, within_minutes: int = 60) -> list[ScheduledEvent]:
        """워밍 시작 시각이 N분 이내인 PENDING 이벤트 조회."""
        now = utc_now()
        cutoff = now + timedelta(minutes=within_minutes)
        with self._lock:
            return [
                e
                for e in self._events.values()
                if e.status == EventStatus.PENDING and e.warmup_time <= cutoff
            ]

    def get_needs_warmup(self) -> list[ScheduledEvent]:
        """워밍 시작 시각에 도달한 PENDING 이벤트 조회."""
        now = utc_now()
        with self._lock:
            return [
                e
                for e in self._events.values()
                if e.status == EventStatus.PENDING and e.warmup_time <= now
            ]

    def get_needs_cooldown(self) -> list[ScheduledEvent]:
        """종료 시각 + 유예 시간(cooldown_grace_period_seconds) 경과한 ACTIVE 이벤트 조회."""
        now = utc_now()
        grace = timedelta(seconds=self._settings.cooldown_grace_period_seconds)
        with self._lock:
            return [
                e
                for e in self._events.values()
                if e.status == EventStatus.ACTIVE and e.end_time + grace <= now
            ]

    def get_active(self) -> list[ScheduledEvent]:
        """현재 ACTIVE 또는 WARMING 상태 이벤트 조회."""
        with self._lock:
            return [
                e
                for e in self._events.values()
                if e.status in (EventStatus.ACTIVE, EventStatus.WARMING)
            ]

    def is_event_period(self) -> bool:
        """현재 시각이 이벤트 기간인지 여부. ML context 주입에 사용."""
        return len(self.get_active()) > 0

    def get_effective_multipliers(self) -> EffectiveMultipliers:
        """활성 이벤트 중 MAX 배율 계산 + Settings cap 적용."""
        active = self.get_active()
        if not active:
            return EffectiveMultipliers(
                rate_multiplier=1.0,
                pool_multiplier=1.0,
                bulkhead_extra_permits=0,
                suppress_degradation=False,
                source_event_ids=[],
            )

        return EffectiveMultipliers(
            rate_multiplier=min(
                max(e.expected_rps_multiplier for e in active),
                self._settings.max_rate_multiplier,
            ),
            pool_multiplier=min(
                max(e.pool_multiplier for e in active),
                self._settings.max_pool_multiplier,
            ),
            bulkhead_extra_permits=min(
                max(e.bulkhead_extra_permits for e in active),
                self._settings.max_bulkhead_extra_permits,
            ),
            suppress_degradation=any(e.suppress_degradation for e in active),
            source_event_ids=[e.event_id for e in active],
        )

    def update_status(self, event_id: str, status: EventStatus) -> None:
        """이벤트 상태 업데이트."""
        with self._lock:
            event = self._events.get(event_id)
            if event is not None:
                event.status = status
                self._persist()

    def get_event(self, event_id: str) -> ScheduledEvent | None:
        """이벤트 ID로 조회."""
        with self._lock:
            return self._events.get(event_id)

    def remove_completed(self) -> int:
        """완료/취소 이벤트 정리. 제거된 수 반환."""
        with self._lock:
            to_remove = [
                eid
                for eid, e in self._events.items()
                if e.status in (EventStatus.COMPLETED, EventStatus.CANCELLED)
            ]
            for eid in to_remove:
                del self._events[eid]
            if to_remove:
                self._persist()
            return len(to_remove)

    def _find_overlapping(self, event: ScheduledEvent) -> list[ScheduledEvent]:
        """시간이 겹치는 기존 이벤트 검색 (lock 내부 호출)."""
        overlapping = []
        for existing in self._events.values():
            if existing.status in (EventStatus.COMPLETED, EventStatus.CANCELLED):
                continue
            if (
                event.start_time < existing.end_time
                and event.end_time > existing.start_time
            ):
                overlapping.append(existing)
        return overlapping

    # ─── StateBackend 영속화 ──────────────────────────────────────────────────

    def _persist(self) -> None:
        """현재 이벤트 상태를 StateBackend에 영속화 (lock 내부 호출)."""
        if not self._state_backend:
            return
        try:
            serialized = self._serialize(self._events)
            self._state_backend.set(STATE_KEY_EVENTS, serialized)
            self._last_load_time = time.monotonic()
        except Exception as exc:
            logger.exception(
                "capacity_reservation.persist_failed",
                error=str(exc),
            )

    @staticmethod
    def _serialize(events: dict[str, ScheduledEvent]) -> dict[str, Any]:
        """이벤트 dict → JSON-serializable dict."""
        return {eid: e.to_dict() for eid, e in events.items()}

    @staticmethod
    def _deserialize(data: dict[str, Any]) -> dict[str, ScheduledEvent]:
        """JSON dict → 이벤트 dict."""
        return {eid: ScheduledEvent.from_dict(d) for eid, d in data.items()}

    def check_drift(self) -> bool:
        """인메모리 vs StateBackend 캐시 유효성 검증. True이면 refresh 수행됨."""
        if not self._state_backend:
            return False
        if self._last_load_time is None:
            self.initialize()
            return True
        elapsed = time.monotonic() - self._last_load_time
        if elapsed > self._cache_ttl_seconds:
            self.initialize()
            return True
        return False
