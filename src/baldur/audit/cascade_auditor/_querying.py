"""
Cascade Auditor - 조회 모듈.

Cascade Event 조회, 인과관계 추적 책임을 담당합니다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.cascade_auditor._helpers import get_index_ids
from baldur.audit.cascade_event import CascadeEvent

logger = structlog.get_logger()


class QueryingMixin:
    """Cascade Event 조회 관련 메서드."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided by CascadeEventAuditor.
        CASCADE_KEY: str
        CASCADE_INDEX_KEY: str

        def _get_backend(self) -> Any: ...

    def get_cascade_event(
        self,
        cascade_id: str,
        namespace: str,
    ) -> CascadeEvent | None:
        """
        Cascade Event 조회.

        Args:
            cascade_id: Cascade Event ID
            namespace: 네임스페이스

        Returns:
            CascadeEvent 또는 None
        """
        backend = self._get_backend()
        key = self.CASCADE_KEY.format(namespace=namespace, cascade_id=cascade_id)
        data = backend.get(key)

        if data:
            return CascadeEvent.from_dict(data)
        return None

    def get_recent_events(
        self,
        namespace: str,
        limit: int = 100,
    ) -> list[CascadeEvent]:
        """
        최근 Cascade Event 목록 조회.

        Args:
            namespace: 네임스페이스
            limit: 최대 개수

        Returns:
            CascadeEvent 목록 (최신순)
        """
        backend = self._get_backend()
        index_key = self.CASCADE_INDEX_KEY.format(namespace=namespace)

        cascade_ids = get_index_ids(backend, index_key)
        if not cascade_ids:
            return []

        cascade_ids = cascade_ids[:limit]

        events = []
        for cascade_id in cascade_ids:
            event = self.get_cascade_event(cascade_id, namespace)
            if event:
                events.append(event)

        return events

    def get_event_count(self, namespace: str) -> int:
        """
        네임스페이스의 Cascade Event 총 개수 조회.

        Args:
            namespace: 네임스페이스

        Returns:
            이벤트 개수
        """
        backend = self._get_backend()
        index_key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        return len(get_index_ids(backend, index_key))

    def find_by_trigger_event(
        self,
        trigger_event_id: str,
        namespace: str,
    ) -> CascadeEvent | None:
        """
        트리거 이벤트 ID로 Cascade Event 조회.

        Args:
            trigger_event_id: 트리거 이벤트 ID
            namespace: 네임스페이스

        Returns:
            CascadeEvent 또는 None
        """
        events = self.get_recent_events(namespace, limit=1000)

        for event in events:
            if event.trigger.event_id == trigger_event_id:
                return event

        return None

    def get_causation_trace(
        self,
        effect_event_id: str,
        namespace: str,
    ) -> list[dict[str, Any]]:
        """
        효과 이벤트의 인과관계 추적.

        특정 효과가 왜 발생했는지 역추적합니다.

        Args:
            effect_event_id: 효과 이벤트 ID
            namespace: 네임스페이스

        Returns:
            인과관계 추적 결과 (트리거까지 역추적)
        """
        events = self.get_recent_events(namespace, limit=1000)

        for cascade in events:
            for effect in cascade.effects:
                if effect.event_id == effect_event_id:
                    # 인과관계 역추적
                    trace: list[dict[str, Any]] = []
                    current_id = effect_event_id

                    while True:
                        # 현재 ID에 해당하는 효과 찾기
                        found = False
                        for e in cascade.effects:
                            if e.event_id == current_id:
                                trace.append(
                                    {
                                        "event_id": e.event_id,
                                        "action_type": e.action_type,
                                        "caused_by": e.caused_by,
                                    }
                                )
                                current_id = e.caused_by
                                found = True
                                break

                        if not found:
                            # 트리거에 도달
                            if current_id == cascade.trigger.event_id:
                                trace.append(
                                    {
                                        "event_id": cascade.trigger.event_id,
                                        "action_type": cascade.trigger.trigger_type,
                                        "caused_by": None,
                                    }
                                )
                            break

                    return list(reversed(trace))

        return []

    def get_events_after_timestamp(
        self,
        namespace: str,
        after_timestamp: str,
        limit: int = 1000,
    ) -> list[CascadeEvent]:
        """
        특정 시각 이후의 이벤트 조회.

        Args:
            namespace: 네임스페이스
            after_timestamp: 이 시각 이후의 이벤트만 조회 (ISO format)
            limit: 최대 개수

        Returns:
            CascadeEvent 목록 (최신순)
        """
        from datetime import datetime

        all_events = self.get_recent_events(namespace, limit=limit)

        try:
            cutoff = datetime.fromisoformat(after_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return all_events

        filtered = []
        for event in all_events:
            try:
                event_time = datetime.fromisoformat(
                    event.timestamp.replace("Z", "+00:00")
                )
                if event_time > cutoff:
                    filtered.append(event)
            except ValueError:
                # 파싱 실패 시 포함
                filtered.append(event)

        return filtered
