"""
Cascade Auditor - WAL/Load Shedding 모듈.

로컬 WAL 저장, Load Shedding, 복구 관련 책임을 담당합니다.
JSONLWriter를 통해 스레드 안전한 JSONL WAL 쓰기를 수행합니다.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.cascade_auditor._helpers import get_index_ids
from baldur.audit.cascade_event import CascadeEvent, ExternalTraceContext
from baldur.audit.wal._jsonl import JSONLWriter
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    import threading

    from baldur.audit.cascade_load_shedding import CascadeLoadShedding

logger = structlog.get_logger()


# WAL path defaults (overridden by BALDUR_CASCADE_WAL_DIR setting)
_DEFAULT_CASCADE_WAL_DIR = "/var/log/baldur/cascade_wal"


def _get_cascade_wal_dir() -> str:
    """Get cascade WAL directory from settings with fallback to default."""
    try:
        from baldur.settings.cascade import get_cascade_settings

        return get_cascade_settings().wal_dir
    except Exception:
        return _DEFAULT_CASCADE_WAL_DIR


LOCAL_CASCADE_WAL_DIR = _DEFAULT_CASCADE_WAL_DIR
LOCAL_CASCADE_WAL_PATH = f"{LOCAL_CASCADE_WAL_DIR}/cascade_audit_wal.jsonl"
LOCAL_CASCADE_FALLBACK_PATH = LOCAL_CASCADE_WAL_PATH

# Lazy-initialized JSONLWriter (fsync=False — Redis가 1차 복구 수단, 로컬 WAL은 best-effort fallback)
# Lazy init prevents file creation during unit test imports where the module is loaded but not used.
_wal_writer: JSONLWriter | None = None


def _get_wal_writer() -> JSONLWriter:
    """Get or create module-level JSONLWriter singleton."""
    global _wal_writer
    if _wal_writer is None:
        wal_dir = _get_cascade_wal_dir()
        wal_path = f"{wal_dir}/cascade_audit_wal.jsonl"
        _wal_writer = JSONLWriter(
            file_path=Path(wal_path),
            fsync=False,
        )
    return _wal_writer


def reset_wal_writer() -> None:
    """Reset WAL writer for test isolation."""
    global _wal_writer
    _wal_writer = None


# 배치 복구 상수
_BATCH_SIZE = 1000
_IDEMPOTENCY_TTL = 3600  # 1시간


def _append_to_wal(data: dict) -> None:
    """
    WAL 파일에 데이터를 JSONL 형식으로 추가.

    JSONLWriter를 통해 스레드 안전하게 기록합니다.

    Args:
        data: 저장할 딕셔너리 데이터
    """
    _get_wal_writer().append(data)


class WALRecoveryMixin:
    """WAL/Load Shedding/복구 관련 메서드."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided via MRO by
        # CascadeEventAuditor and sibling mixins (QueryingMixin,
        # RecordingMixin).
        _lock: threading.RLock
        _load_shedding: CascadeLoadShedding | None
        _max_index_size: int
        CASCADE_INDEX_KEY: str

        def _get_backend(self) -> Any: ...
        def _get_load_shedding(self) -> CascadeLoadShedding | None: ...
        def _save_cascade_event(self, event: Any) -> None: ...
        def record(
            self,
            trigger_type: str,
            trigger_details: dict[str, Any],
            effects: list[dict[str, Any]],
            namespace: str,
            triggered_by: str | None = None,
            external_trace: ExternalTraceContext | None = None,
        ) -> CascadeEvent: ...

    def record_with_load_shedding(
        self,
        trigger_type: str,
        trigger_details: dict[str, Any],
        effects: list[dict[str, Any]],
        namespace: str,
        triggered_by: str | None = None,
        external_trace: ExternalTraceContext | None = None,
    ) -> CascadeEvent | None:
        """
        Load Shedding을 적용하여 Cascade Event 기록.

        버퍼 사용률에 따라 우선순위가 낮은 이벤트를 드롭합니다.
        CRITICAL 이벤트는 절대 드롭하지 않으며, 필요시 로컬 폴백을 사용합니다.

        Args:
            trigger_type: 트리거 유형
            trigger_details: 트리거 상세 정보
            effects: 연쇄 효과 목록
            namespace: 네임스페이스
            triggered_by: 트리거 주체
            external_trace: 외부 분산 추적 컨텍스트

        Returns:
            생성된 CascadeEvent 또는 None (드롭된 경우)
        """
        load_shedding = self._get_load_shedding()

        if not load_shedding:
            # Load Shedding 비활성화 시 일반 기록
            return self.record(
                trigger_type=trigger_type,
                trigger_details=trigger_details,
                effects=effects,
                namespace=namespace,
                triggered_by=triggered_by,
                external_trace=external_trace,
            )

        # 버퍼 상태 확인
        backend = self._get_backend()
        index_key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        buffer_size = len(get_index_ids(backend, index_key))

        # Load Shedding 결정
        decision = load_shedding.should_accept(
            trigger_type=trigger_type,
            buffer_size=buffer_size,
            buffer_capacity=self._max_index_size,
        )

        if not decision["accepted"]:
            # 드롭
            logger.warning(
                "cascade_audit.event_dropped_load_shedding",
                trigger_type=trigger_type,
                decision=decision["reason"],
            )

            # 폴백 권장 시 로컬에 저장
            if decision.get("use_fallback"):
                self._record_dropped_to_wal(
                    trigger_type=trigger_type,
                    trigger_details=trigger_details,
                    effects=effects,
                    namespace=namespace,
                    reason=decision["reason"],
                )

            return None

        # 정상 기록
        return self.record(
            trigger_type=trigger_type,
            trigger_details=trigger_details,
            effects=effects,
            namespace=namespace,
            triggered_by=triggered_by,
            external_trace=external_trace,
        )

    def _save_to_local_wal(self, event: CascadeEvent) -> None:
        """
        로컬 WAL에 Cascade Event 저장.

        Redis 장애 시 로컬 WAL 파일에 JSONL 형식으로 저장합니다.

        Args:
            event: 저장할 CascadeEvent
        """
        try:
            _append_to_wal(event.to_dict())
            logger.info(
                "cascade_audit.saved_local_wal",
                cascade_event_id=event.id,
            )
        except Exception as e:
            logger.exception(
                "cascade_audit.local_wal_save_failed",
                error=e,
            )

    # 하위 호환성
    _save_to_local_fallback = _save_to_local_wal

    def _record_dropped_to_wal(
        self,
        trigger_type: str,
        trigger_details: dict[str, Any],
        effects: list[dict[str, Any]],
        namespace: str,
        reason: str,
    ) -> None:
        """
        드롭된 이벤트 정보를 WAL에 기록.

        Load Shedding으로 드롭된 이벤트의 최소 정보를 기록합니다.
        """
        try:
            _append_to_wal(
                {
                    "type": "dropped",
                    "trigger_type": trigger_type,
                    "namespace": namespace,
                    "reason": reason,
                    "effects_count": len(effects),
                    "dropped_at": utc_now().isoformat(),
                }
            )
        except Exception as e:
            logger.debug(
                "cascade_audit.dropped_record_save_failed",
                error=e,
            )

    # 하위 호환성
    _record_dropped_to_fallback = _record_dropped_to_wal

    def recover_from_local_wal(  # noqa: C901, PLR0912
        self,
        namespace: str = "global",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        로컬 WAL에서 Redis로 복구 (배치 최적화).

        Redis 장애 복구 후 로컬 WAL에 쌓인 이벤트를 Redis로 이관합니다.
        배치 멱등성 검사로 중복 복구를 방지하고,
        인덱스 일괄 업데이트로 Redis 왕복을 최소화합니다.

        Args:
            namespace: 네임스페이스
            dry_run: True면 실제 복구 없이 대상만 확인

        Returns:
            복구 결과 통계 (idempotency_skipped 포함)
        """
        wal_path = Path(LOCAL_CASCADE_WAL_PATH)

        if not wal_path.exists():
            return {
                "status": "no_wal_data",
                "namespace": namespace,
                "recovered": 0,
                "failed": 0,
                "idempotency_skipped": 0,
            }

        entries = []

        # WAL 파일에서 해당 네임스페이스 이벤트 읽기
        from baldur.audit.wal._jsonl import JSONLReader

        for entry in JSONLReader.iter_entries(wal_path):
            if entry.get("namespace") == namespace and entry.get("type") != "dropped":
                entries.append(entry)

        if dry_run:
            logger.info(
                "cascade_audit.wal_recovery_dry_run",
                count=len(entries),
                namespace=namespace,
            )
            return {
                "status": "dry_run",
                "namespace": namespace,
                "entries_to_recover": len(entries),
                "recovered": 0,
                "idempotency_skipped": 0,
            }

        if not entries:
            return {
                "status": "no_wal_data",
                "namespace": namespace,
                "recovered": 0,
                "failed": 0,
                "idempotency_skipped": 0,
            }

        # 배치 멱등성 체크 — 이미 복구된 엔트리 스킵
        idempotency_skipped = 0
        entries_to_recover = []

        for i in range(0, len(entries), _BATCH_SIZE):
            batch = entries[i : i + _BATCH_SIZE]
            duplicate_indices = self._batch_check_cascade_idempotency(batch)

            for j, entry in enumerate(batch):
                if j in duplicate_indices:
                    idempotency_skipped += 1
                else:
                    entries_to_recover.append(entry)

        # Redis로 복구
        recovered = 0
        failed = 0
        recovered_ids: list[str] = []
        recovered_entries: list[dict] = []

        for entry in entries_to_recover:
            try:
                event = CascadeEvent.from_dict(entry)
                self._save_cascade_event(event)
                recovered_ids.append(event.id)
                recovered_entries.append(entry)
                recovered += 1
            except Exception as e:
                logger.exception(
                    "watchdog.recovery_failed",
                    error=e,
                )
                failed += 1

        # 배치 인덱스 업데이트 (N × GET/SET → 1 GET + 1 SET)
        index_failed = False
        if recovered_ids:
            try:
                self._batch_add_to_index(namespace, recovered_ids)
            except Exception:
                logger.exception(
                    "cascade_audit.batch_index_update_failed",
                    namespace=namespace,
                    count=len(recovered_ids),
                )
                index_failed = True

        # 배치 멱등성 마킹 (성공적으로 복구된 엔트리만)
        # 인덱스 실패 시에도 마킹하여 다음 복구에서 데이터 중복 저장 방지
        if recovered_entries:
            self._batch_mark_cascade_processed(recovered_entries)

        # 복구 완료 후 해당 네임스페이스 엔트리 제거
        # 인덱스 실패 시 WAL 유지 → 다음 복구에서 인덱스 재시도
        if (
            failed == 0
            and not index_failed
            and (recovered > 0 or idempotency_skipped > 0)
        ):
            self._remove_namespace_from_wal(namespace)

        logger.info(
            "cascade_audit.wal_recovery_completed",
            recovered=recovered,
            failed=failed,
            idempotency_skipped=idempotency_skipped,
            namespace=namespace,
        )

        return {
            "status": "completed",
            "namespace": namespace,
            "recovered": recovered,
            "failed": failed,
            "idempotency_skipped": idempotency_skipped,
            "index_failed": index_failed,
        }

    def _batch_check_cascade_idempotency(
        self,
        entries: list[dict[str, Any]],
    ) -> set[int]:
        """
        배치 멱등성 검사 (cascade recovery).

        IdempotencyService.batch_check()로 1000건씩 중복 검사.
        서비스 미사용 또는 장애 시 빈 집합 반환 (안전하게 진행).

        Returns:
            중복인 엔트리의 배치 내 인덱스 집합
        """
        try:
            from baldur.services.idempotency import (
                IdempotencyKey,
                IdempotencyService,
            )

            service = IdempotencyService()
            keys = [
                IdempotencyKey.for_wal_recovery(
                    wal_entry_id=entry.get("id", ""),
                    operation="cascade_recovery",
                )
                for entry in entries
            ]
            results = service.batch_check(keys)
            duplicates = {i for i, result in enumerate(results) if result.is_duplicate}
            logger.debug(
                "cascade_audit.batch_idempotency_checked",
                batch_size=len(entries),
                duplicates_found=len(duplicates),
            )
            return duplicates

        except (ImportError, AttributeError):
            return set()

        except Exception:
            logger.warning(
                "cascade_audit.batch_idempotency_check_failed",
                batch_size=len(entries),
            )
            return set()

    def _batch_mark_cascade_processed(
        self,
        entries: list[dict[str, Any]],
    ) -> None:
        """배치 멱등성 마킹 (cascade recovery)."""
        try:
            from baldur.services.idempotency import (
                IdempotencyKey,
                IdempotencyService,
            )

            service = IdempotencyService()
            keys = [
                IdempotencyKey.for_wal_recovery(
                    wal_entry_id=entry.get("id", ""),
                    operation="cascade_recovery",
                )
                for entry in entries
            ]
            service.batch_mark_as_processed(keys, ttl=_IDEMPOTENCY_TTL)
            logger.debug(
                "cascade_audit.batch_idempotency_marked",
                batch_size=len(entries),
            )

        except (ImportError, AttributeError):
            pass

        except Exception:
            logger.warning(
                "cascade_audit.batch_mark_processed_failed",
                batch_size=len(entries),
            )

    def _batch_add_to_index(
        self,
        namespace: str,
        cascade_ids: list[str],
    ) -> None:
        """
        복구된 이벤트 ID를 인덱스에 일괄 추가.

        N × (GET + SET) 대신 1 GET + 1 SET으로 Redis 왕복 최소화.
        기존 _add_to_index 동작과 동일한 최신순 유지를 위해 역순 삽입.
        """
        backend = self._get_backend()
        key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        ids = get_index_ids(backend, key)

        # 기존 _add_to_index는 insert(0, id)를 N회 호출 → 마지막 ID가 맨 앞
        # 동일 순서를 유지하기 위해 역순으로 prepend
        ids = list(reversed(cascade_ids)) + ids

        if len(ids) > self._max_index_size:
            ids = ids[: self._max_index_size]

        backend.set(key, {"ids": ids})

    # 하위 호환성
    recover_from_local_fallback = recover_from_local_wal

    def _remove_namespace_from_wal(self, namespace: str) -> None:
        """WAL 파일에서 특정 네임스페이스 엔트리 제거."""
        from baldur.audit.wal._cleanup import cleanup_by_namespace

        cleanup_by_namespace(Path(LOCAL_CASCADE_WAL_PATH), namespace)

    # 하위 호환성
    _remove_namespace_from_fallback = _remove_namespace_from_wal

    def get_load_shedding_status(
        self,
        namespace: str = "global",
    ) -> dict[str, Any]:
        """
        Load Shedding 상태 조회.

        Args:
            namespace: 네임스페이스

        Returns:
            Load Shedding 상태 정보
        """
        load_shedding = self._get_load_shedding()

        if not load_shedding:
            return {
                "enabled": False,
                "status": "DISABLED",
            }

        # 버퍼 상태 확인
        backend = self._get_backend()
        index_key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        buffer_size = len(get_index_ids(backend, index_key))

        status = load_shedding.get_status(
            buffer_size=buffer_size,
            buffer_capacity=self._max_index_size,
        )
        status["enabled"] = True
        status["namespace"] = namespace

        return status
