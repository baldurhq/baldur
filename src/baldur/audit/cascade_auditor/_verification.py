"""
Cascade Auditor - 무결성 검증 모듈.

해시 체인 검증, 체크포인트 관련 책임을 담당합니다.
중복되던 해시 검증 로직을 _verify_event_chain()으로 통합합니다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.audit.cascade_event import CascadeEvent

logger = structlog.get_logger()


def _verify_event_chain(events: list[CascadeEvent]) -> list[dict[str, Any]]:
    """
    이벤트 목록의 해시 체인 무결성을 검증하는 공통 로직.

    기존 verify_chain_integrity/verify_chain_integrity_from_checkpoint에서
    동일하게 반복되던 검증 코드를 통합합니다.

    Args:
        events: CascadeEvent 목록 (최신순 정렬)

    Returns:
        오류 목록
    """
    errors = []

    for i, event in enumerate(events):
        # 1. 해시 재계산
        recalculated_hash = event.calculate_hash()
        if recalculated_hash != event.current_hash:
            errors.append(
                {
                    "cascade_id": event.id,
                    "error": "hash_mismatch",
                    "expected": event.current_hash,
                    "actual": recalculated_hash,
                }
            )

        # 2. 체인 연결 확인 (마지막 제외)
        # 최신순 정렬이므로 i=0이 최신, i+1이 이전 이벤트
        if i < len(events) - 1:
            older_event = events[i + 1]
            if event.previous_hash != older_event.current_hash:
                errors.append(
                    {
                        "cascade_id": event.id,
                        "error": "chain_broken",
                        "expected_previous": older_event.current_hash,
                        "actual_previous": event.previous_hash,
                    }
                )

    return errors


class VerificationMixin:
    """Hash Chain 무결성 검증 및 체크포인트 관련 메서드."""

    # 체크포인트 Redis 키 패턴
    CHECKPOINT_KEY = "baldur:{namespace}:audit:cascade_checkpoint"

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided by CascadeEventAuditor
        # and sibling mixins (QueryingMixin).
        CASCADE_INDEX_KEY: str

        def _get_backend(self) -> Any: ...
        def _get_last_hash(self, namespace: str) -> str | None: ...
        def get_recent_events(
            self, namespace: str, limit: int = 100
        ) -> list[CascadeEvent]: ...

    def verify_chain_integrity(
        self,
        namespace: str,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """
        Hash Chain 무결성 검증.

        Args:
            namespace: 네임스페이스
            limit: 검증할 최대 이벤트 수

        Returns:
            검증 결과 딕셔너리:
            - valid: 무결성 유효 여부
            - checked: 검증한 이벤트 수
            - errors: 오류 목록
        """
        events = self.get_recent_events(namespace, limit)

        if not events:
            return {"valid": True, "checked": 0, "errors": []}

        errors = _verify_event_chain(events)

        return {
            "valid": len(errors) == 0,
            "checked": len(events),
            "errors": errors,
        }

    def create_checkpoint(self, namespace: str) -> dict[str, Any]:
        """
        현재 상태를 체크포인트로 저장.

        체크포인트는 특정 시점의 Hash Chain 상태를 기록하여
        이후 무결성 검증 시 처음부터 검증하지 않고 체크포인트
        이후만 검증할 수 있게 합니다.

        Daily Celery Beat에서 호출됩니다.

        Args:
            namespace: 네임스페이스

        Returns:
            생성된 체크포인트 정보
        """

        from baldur.audit.cascade_auditor._helpers import get_index_ids

        backend = self._get_backend()

        # 최신 이벤트의 해시 조회
        last_hash = self._get_last_hash(namespace)

        # 이벤트 수 계산
        index_key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        event_count = len(get_index_ids(backend, index_key))

        checkpoint = {
            "last_hash": last_hash,
            "verified_at": utc_now().isoformat(),
            "event_count": event_count,
            "namespace": namespace,
            "version": "1.0",
        }

        key = self.CHECKPOINT_KEY.format(namespace=namespace)
        backend.set(key, checkpoint)

        logger.info(
            "cascade_audit.checkpoint_created",
            namespace=namespace,
            event_count=event_count,
            last_hash=last_hash[:16] if last_hash else "None",
        )

        return checkpoint

    def get_checkpoint(self, namespace: str) -> dict[str, Any] | None:
        """
        체크포인트 조회.

        Args:
            namespace: 네임스페이스

        Returns:
            체크포인트 정보 또는 None
        """
        backend = self._get_backend()
        key = self.CHECKPOINT_KEY.format(namespace=namespace)
        return backend.get(key)

    def verify_chain_integrity_from_checkpoint(
        self,
        namespace: str,
    ) -> dict[str, Any]:
        """
        체크포인트 이후만 검증 (효율적).

        기존 verify_chain_integrity()는 처음부터 검증하지만,
        이 메서드는 마지막 체크포인트 이후만 검증합니다.

        Args:
            namespace: 네임스페이스

        Returns:
            검증 결과 딕셔너리
        """
        # 1. 체크포인트 조회
        checkpoint = self.get_checkpoint(namespace)

        if not checkpoint or not checkpoint.get("last_hash"):
            # 체크포인트 없으면 전체 검증
            return self.verify_chain_integrity(namespace)

        # 2. 전체 이벤트 조회 (최신순)
        events = self.get_recent_events(namespace, limit=10000)

        if not events:
            return {
                "valid": True,
                "checked": 0,
                "from_checkpoint": checkpoint.get("verified_at"),
                "errors": [],
            }

        # 3. 체크포인트 이후 이벤트 필터링
        checkpoint_hash = checkpoint.get("last_hash")
        events_after_checkpoint = []
        checkpoint_found = False

        for event in events:
            if event.current_hash == checkpoint_hash:
                checkpoint_found = True
                break
            events_after_checkpoint.append(event)

        if not checkpoint_found:
            logger.warning(
                "cascade_audit.checkpoint_hash_found_falling",
                namespace=namespace,
            )
            return self.verify_chain_integrity(namespace)

        if not events_after_checkpoint:
            return {
                "valid": True,
                "checked": 0,
                "from_checkpoint": checkpoint.get("verified_at"),
                "errors": [],
            }

        # 4. 체크포인트 이후 이벤트만 검증
        errors = []

        # 첫 번째 이벤트(체크포인트 직후)의 previous_hash가 체크포인트와 연결되는지 확인
        first_event = events_after_checkpoint[-1]  # 가장 오래된 것
        if first_event.previous_hash != checkpoint_hash:
            errors.append(
                {
                    "cascade_id": first_event.id,
                    "error": "checkpoint_mismatch",
                    "expected_previous": checkpoint_hash,
                    "actual_previous": first_event.previous_hash,
                }
            )

        # 나머지 체인 검증 (통합 함수 사용)
        errors.extend(_verify_event_chain(events_after_checkpoint))

        return {
            "valid": len(errors) == 0,
            "checked": len(events_after_checkpoint),
            "from_checkpoint": checkpoint.get("verified_at"),
            "errors": errors,
        }
