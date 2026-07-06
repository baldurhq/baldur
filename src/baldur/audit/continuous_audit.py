"""
Continuous Audit Recorder - 기업 감사 스타일 지속적 감사.

모든 자동화된 결정을 위변조 불가능하게 기록합니다.
보고서 포맷팅은 제공하지 않고, 완전한 raw data만 제공합니다.
(각 조직은 자체 형식으로 데이터를 가공해야 함)

Design Philosophy:
- 완전하고 정확한 raw data 기록
- 해시 체인으로 위변조 방지
- 쿼리/필터링/익스포트 기능 제공
- 포맷팅은 사용자 책임
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.config import AuditConfig
from baldur.audit.integrity import HashChainManager, HashChainVerifier
from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.audit.checkpoint import (
        CheckpointStorageStrategy,
    )
    from baldur.audit.wal import WALConfig, WriteAheadLog

logger = structlog.get_logger()


class ContinuousAuditRecorder:
    """
    지속적 감사 기록기.

    특징:
    - 해시 체인으로 위변조 방지
    - 다중 스토리지 백엔드 지원
    - 규정 위반 시 즉시 알림
    - Raw data 조회/필터/익스포트 제공
    - 보고서 포맷팅 미제공 (사용자가 직접 가공)

    FAIL-OPEN Design Policy:
    --------------------------
    감사 로그 기록 실패가 비즈니스 처리를 방해하지 않습니다.
    - 기본: Audit 실패 → 경고 로그 + fallback stdout
    - 선택: fail_open=False로 Fail-Secure 모드 가능 (PCI-DSS)

    업계 정책:
    - Netflix Zuul: Fail-Open (가용성 우선)
    - Stripe: Fail-Open + 재시도
    - PCI-DSS: Fail-Secure 권장 (단, 가용성 예외 허용)
    - SOC2: Fail-Open 허용 (실패 기록만 있으면 됨)

    Usage:
        config = AuditConfig.get_default()
        recorder = ContinuousAuditRecorder(
            audit_adapter=FileAuditLogAdapter("logs/audit.jsonl"),
            config=config,
            fail_open=True,  # 기본: Fail-Open
        )

        recorder.record_auto_tuning(
            parameter="timeout_ms",
            old_value=5000,
            new_value=6000,
            reason="P99 레이턴시 증가",
            confidence=0.85,
            metrics_snapshot={"p99_latency_ms": 4200},
            safety_check={"within_bounds": True},
        )
    """

    def __init__(
        self,
        audit_adapter: AuditLogAdapter,
        config: AuditConfig | None = None,
        alert_callback: Callable[[str, dict[str, Any]], None] | None = None,
        state_file: Path | None = None,
        # Fail-Open 정책
        fail_open: bool = True,
        fallback_to_stdout: bool = True,
        # WAL 연동
        wal_enabled: bool = False,
        wal_config: WALConfig | None = None,
        # Checkpoint Strategy 연동
        checkpoint_strategy: CheckpointStorageStrategy | None = None,
        checkpoint_namespace: str = "default",
        # Checkpoint Back-pressure 설정
        checkpoint_save_interval: int = 10,
        checkpoint_save_max_seconds: float = 30.0,
    ):
        """
        Initialize ContinuousAuditRecorder.

        Args:
            audit_adapter: 감사 로그 저장 어댑터
            config: 감사 설정 (None이면 환경변수에서 로드)
            alert_callback: 알림 콜백 (channel, data) -> None
            state_file: 해시 체인 상태 파일 경로
            fail_open: Fail-Open 정책 (기본: True)
            fallback_to_stdout: 실패 시 stdout 출력 (기본: True)
            wal_enabled: WAL 활성화 (기본: False)
            wal_config: WAL 설정
            checkpoint_strategy: 체크포인트 저장 전략 (None이면 WAL 활성화 시 기본값 사용)
            checkpoint_namespace: 체크포인트 네임스페이스
            checkpoint_save_interval: N번 기록마다 체크포인트 저장 (기본: 10)
            checkpoint_save_max_seconds: 최대 저장 간격 초 (기본: 30.0)
        """
        self.audit_adapter = audit_adapter
        self.config = config or AuditConfig.get_default()
        self.alert_callback = alert_callback

        # Fail-Open 정책
        self._fail_open = fail_open
        self._fallback_to_stdout = fallback_to_stdout
        self._failed_write_count = 0

        # 해시 체인 관리자
        self._hash_manager = HashChainManager(state_file=state_file)
        self._lock = threading.RLock()

        # WAL 초기화 (선택적)
        self._wal_enabled = wal_enabled
        self._wal: WriteAheadLog | None = None

        if wal_enabled:
            try:
                from baldur.audit.wal import WALConfig as WALConfigClass
                from baldur.audit.wal import WriteAheadLog

                self._wal = WriteAheadLog(config=wal_config or WALConfigClass())
                logger.info("continuous_audit.wal_enabled")
            except Exception as e:
                logger.warning(
                    "continuous_audit.wal_initialization_failed",
                    error=e,
                )
                self._wal_enabled = False

        # Checkpoint Strategy 초기화
        self._checkpoint_strategy: CheckpointStorageStrategy | None = (
            checkpoint_strategy
        )
        self._checkpoint_namespace = checkpoint_namespace

        # Checkpoint Back-pressure 상태 (호출자 책임 패턴)
        self._checkpoint_save_interval = checkpoint_save_interval
        self._checkpoint_save_max_seconds = checkpoint_save_max_seconds
        self._records_since_checkpoint: int = 0
        self._last_checkpoint_time: float = time.time()

        if checkpoint_strategy is None and wal_enabled:
            # WAL 활성화 시 기본 전략 자동 설정
            try:
                from baldur.audit.checkpoint import (
                    get_default_checkpoint_strategy,
                )

                self._checkpoint_strategy = get_default_checkpoint_strategy()
                logger.info("continuous_audit.checkpoint_strategy_initialized")
            except Exception as e:
                logger.warning(
                    "continuous_audit.checkpoint_strategy_init_failed",
                    error=e,
                )

        # 환경 정보
        self._environment = os.environ.get("ENVIRONMENT", "development")
        self._service_name = os.environ.get("SERVICE_NAME", "unknown")
        self._service_version = os.environ.get("SERVICE_VERSION", "unknown")

    # ─────────────────────────────────────────────────────────────
    # 기록 메서드 (Auto Tuning)
    # ─────────────────────────────────────────────────────────────

    def record_auto_tuning(
        self,
        parameter: str,
        old_value: Any,
        new_value: Any,
        reason: str,
        confidence: float,
        metrics_snapshot: dict[str, Any],
        safety_check: dict[str, Any],
        actor_id: str = "runtime_feedback_loop",
    ) -> str:
        """
        자율 조정 기록.

        Args:
            parameter: 조정된 파라미터 이름
            old_value: 이전 값
            new_value: 새 값
            reason: 조정 사유
            confidence: 신뢰도 (0.0 ~ 1.0)
            metrics_snapshot: 결정 시점 메트릭
            safety_check: 안전 검사 결과
            actor_id: 조정 수행 주체

        Returns:
            감사 로그 ID
        """
        entry = AuditEntry(
            action=AuditAction.AUTO_TUNING_ADJUSTMENT,
            target_type="runtime_config",
            target_id=parameter,
            actor_type="system",
            actor_id=actor_id,
            service_name=self._service_name,
            reason=reason,
            details={
                "adjustment_type": "automatic",
                "parameter": parameter,
                "before": {"value": old_value},
                "after": {"value": new_value, "confidence": confidence},
                "reason": reason,
                "metrics_snapshot": metrics_snapshot,
                "safety_check": safety_check,
                "environment": self._environment,
                "service_version": self._service_version,
            },
        )

        audit_id = self._record_with_integrity(entry)

        # 알림 발송
        self._send_alert(
            "auto_tuning",
            {
                "parameter": parameter,
                "old_value": old_value,
                "new_value": new_value,
                "reason": reason,
            },
        )

        return audit_id

    def record_auto_tuning_rejected(
        self,
        parameter: str,
        requested_value: Any,
        current_value: Any,
        rejection_reason: str,
        safety_bounds: dict[str, Any],
    ) -> str:
        """안전 한계 초과로 자율 조정 거부됨."""
        entry = AuditEntry(
            action=AuditAction.AUTO_TUNING_REJECTED,
            target_type="runtime_config",
            target_id=parameter,
            actor_type="system",
            actor_id="safety_guard",
            service_name=self._service_name,
            reason=rejection_reason,
            success=False,
            details={
                "parameter": parameter,
                "requested_value": requested_value,
                "current_value": current_value,
                "rejection_reason": rejection_reason,
                "safety_bounds": safety_bounds,
            },
        )

        audit_id = self._record_with_integrity(entry)

        self._send_alert(
            "auto_tuning_rejected",
            {
                "parameter": parameter,
                "requested_value": requested_value,
                "rejection_reason": rejection_reason,
                "severity": "warning",
            },
        )

        return audit_id

    def record_auto_tuning_rollback(
        self,
        parameter: str,
        rolled_back_value: Any,
        target_value: Any,
        rollback_reason: str,
        strategy: str,  # last_known_good, dna_declared, system_defaults
    ) -> str:
        """자율 조정 롤백 기록."""
        entry = AuditEntry(
            action=AuditAction.AUTO_TUNING_ROLLBACK,
            target_type="runtime_config",
            target_id=parameter,
            actor_type="system",
            actor_id="auto_rollback_guard",
            service_name=self._service_name,
            reason=rollback_reason,
            details={
                "parameter": parameter,
                "rolled_back_value": rolled_back_value,
                "target_value": target_value,
                "rollback_reason": rollback_reason,
                "recovery_strategy": strategy,
            },
        )

        return self._record_with_integrity(entry)

    # ─────────────────────────────────────────────────────────────
    # 기록 메서드 (DNA Drift)
    # ─────────────────────────────────────────────────────────────

    def record_drift_detected(
        self,
        resource_id: str,
        declared: dict[str, Any],
        actual: dict[str, Any],
        drifted_fields: list[str],
        severity: str,  # low, medium, high, critical
    ) -> str:
        """
        DNA Drift 감지 기록.

        Args:
            resource_id: 드리프트가 발생한 리소스 ID
            declared: DNA에 선언된 값
            actual: 실제 런타임 값
            drifted_fields: 드리프트된 필드 목록
            severity: 심각도
        """
        entry = AuditEntry(
            action=AuditAction.DNA_DRIFT_DETECTED,
            target_type="stage_dna",
            target_id=resource_id,
            actor_type="system",
            actor_id="dna_drift_detector",
            service_name=self._service_name,
            reason=f"Configuration drift detected in {len(drifted_fields)} field(s)",
            details={
                "drift_type": "configuration_mismatch",
                "declared": declared,
                "actual": actual,
                "drifted_fields": drifted_fields,
                "severity": severity,
                "auto_remediation": False,
            },
        )

        audit_id = self._record_with_integrity(entry)

        # 심각도에 따라 알림
        if severity in ("high", "critical"):
            self._send_alert(
                "drift_critical",
                {
                    "resource_id": resource_id,
                    "drifted_fields": drifted_fields,
                    "severity": severity,
                },
            )

        return audit_id

    def record_drift_resolved(
        self,
        resource_id: str,
        resolved_fields: list[str],
        resolution_method: str,  # manual, auto_sync, config_update
    ) -> str:
        """DNA Drift 해결 기록."""
        entry = AuditEntry(
            action=AuditAction.DNA_DRIFT_RESOLVED,
            target_type="stage_dna",
            target_id=resource_id,
            actor_type="system",
            actor_id="drift_resolver",
            service_name=self._service_name,
            reason=f"Drift resolved via {resolution_method}",
            details={
                "resolved_fields": resolved_fields,
                "resolution_method": resolution_method,
            },
        )

        return self._record_with_integrity(entry)

    # ─────────────────────────────────────────────────────────────
    # 기록 메서드 (Compliance)
    # ─────────────────────────────────────────────────────────────

    def record_compliance_check(
        self,
        standards_checked: list[str],
        results: dict[str, Any],
        overall_status: str,  # compliant, compliant_with_warnings, non_compliant
    ) -> str:
        """
        Compliance 검사 결과 기록.

        Args:
            standards_checked: 검사한 규정 목록 (예: ["DORA", "PCI-DSS"])
            results: 규정별 검사 결과
            overall_status: 전체 준수 상태
        """
        entry = AuditEntry(
            action=AuditAction.COMPLIANCE_CHECK,
            target_type="baldur_system",
            target_id="global",
            actor_type="system",
            actor_id="compliance_checker",
            service_name=self._service_name,
            reason=f"Compliance check: {overall_status}",
            success=overall_status != "non_compliant",
            details={
                "standards_checked": standards_checked,
                "results": results,
                "overall_status": overall_status,
                "checked_at": utc_now().isoformat(),
            },
        )

        audit_id = self._record_with_integrity(entry)

        # 위반 시 알림
        if overall_status == "non_compliant":
            self._send_alert(
                "compliance_violation",
                {
                    "standards_checked": standards_checked,
                    "results": results,
                    "severity": "critical",
                },
            )

        return audit_id

    def record_compliance_violation(
        self,
        standard: str,
        violation_type: str,
        description: str,
        remediation_required: bool = True,
    ) -> str:
        """Compliance 위반 기록."""
        entry = AuditEntry(
            action=AuditAction.COMPLIANCE_VIOLATION,
            target_type="compliance",
            target_id=standard,
            actor_type="system",
            actor_id="compliance_checker",
            service_name=self._service_name,
            reason=description,
            success=False,
            details={
                "standard": standard,
                "violation_type": violation_type,
                "description": description,
                "remediation_required": remediation_required,
            },
        )

        audit_id = self._record_with_integrity(entry)

        self._send_alert(
            "compliance_violation",
            {
                "standard": standard,
                "violation_type": violation_type,
                "description": description,
                "severity": "critical",
            },
        )

        return audit_id

    # ─────────────────────────────────────────────────────────────
    # 조회 메서드 (Raw Data)
    # ─────────────────────────────────────────────────────────────

    def query(
        self,
        action: AuditAction | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        감사 로그 조회 (Raw data).

        Args:
            action: 액션 유형 필터
            target_type: 대상 유형 필터
            target_id: 대상 ID 필터
            start_time: 시작 시간
            end_time: 종료 시간
            limit: 최대 반환 개수

        Returns:
            감사 로그 딕셔너리 목록
        """
        entries = self.audit_adapter.query(
            action=action,
            target_type=target_type,
            target_id=target_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

        return [e.to_dict() for e in entries]

    def query_auto_tuning_history(
        self,
        parameter: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        자율 조정 이력 조회.

        Args:
            parameter: 파라미터 이름 필터
            start_time: 시작 시간
            end_time: 종료 시간
            limit: 최대 반환 개수

        Returns:
            자율 조정 로그 목록
        """
        return self.query(
            action=AuditAction.AUTO_TUNING_ADJUSTMENT,
            target_type="runtime_config",
            target_id=parameter,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    def query_drift_history(
        self,
        resource_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        DNA Drift 이력 조회.

        Returns:
            드리프트 감지/해결 로그 목록
        """
        # DNA_DRIFT_DETECTED와 DNA_DRIFT_RESOLVED 모두 조회
        detected = self.query(
            action=AuditAction.DNA_DRIFT_DETECTED,
            target_id=resource_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit // 2,
        )

        resolved = self.query(
            action=AuditAction.DNA_DRIFT_RESOLVED,
            target_id=resource_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit // 2,
        )

        # 시간순 정렬
        all_entries = detected + resolved
        all_entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        return all_entries[:limit]

    def query_compliance_history(
        self,
        standard: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Compliance 검사 이력 조회.

        Returns:
            규정 준수 검사 로그 목록
        """
        return self.query(
            action=AuditAction.COMPLIANCE_CHECK,
            target_id=standard or "global",
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    # ─────────────────────────────────────────────────────────────
    # 익스포트 메서드 (Raw Data)
    # ─────────────────────────────────────────────────────────────

    def export_jsonl(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        action_filter: list[AuditAction] | None = None,
        limit: int = 50000,
    ) -> Iterator[str]:
        """
        JSON Lines streaming export.

        Yields:
            JSON string per entry
        """
        entries = self.query(
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

        for entry in entries:
            if action_filter:
                entry_action = entry.get("action", "")
                if not any(a.value == entry_action for a in action_filter):
                    continue
            yield fast_dumps_str(entry, default=str)

    def export_csv_compatible(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        """
        Streaming CSV-compatible flattened data.

        Yields:
            Flattened dict per entry (fixed audit fields + details_* keys)
        """
        from baldur.audit.constants import FIXED_AUDIT_FIELDS

        for line in self.export_jsonl(start_time=start_time, end_time=end_time):
            entry = fast_loads(line)
            flat = {k: entry.get(k) for k in FIXED_AUDIT_FIELDS}

            details = entry.get("details", {})
            for key, value in details.items():
                if isinstance(value, (dict, list)):
                    flat[f"details_{key}"] = fast_dumps_str(value)
                else:
                    flat[f"details_{key}"] = value

            yield flat

    # ─────────────────────────────────────────────────────────────
    # 무결성 검증
    # ─────────────────────────────────────────────────────────────

    def verify_integrity(self) -> dict[str, Any]:
        """
        감사 로그 무결성 검증.

        Returns:
            검증 결과 딕셔너리
        """
        entries = self.query(limit=10000)

        verifier = HashChainVerifier()

        # 엔트리에 integrity 필드가 있으면 검증
        entries_with_integrity = [
            e for e in entries if "integrity" in e.get("details", {})
        ]

        if not entries_with_integrity:
            return {
                "verified": True,
                "total_entries": len(entries),
                "verified_entries": 0,
                "message": "No entries with integrity information found",
            }

        # 무결성 정보를 최상위로 이동
        for entry in entries_with_integrity:
            entry["integrity"] = entry.get("details", {}).get("integrity", {})

        is_valid, error_msg = verifier.verify_chain(entries_with_integrity)
        issues = verifier.find_tampering(entries_with_integrity) if not is_valid else []

        result = {
            "verified": is_valid,
            "total_entries": len(entries),
            "verified_entries": len(entries_with_integrity),
            "chain_state": self._hash_manager.get_state(),
        }

        if not is_valid:
            result["error"] = error_msg
            result["issues"] = issues

        return result

    def get_chain_state(self) -> dict[str, Any]:
        """현재 해시 체인 상태 반환."""
        return self._hash_manager.get_state()

    # ─────────────────────────────────────────────────────────────
    # 내부 메서드
    # ─────────────────────────────────────────────────────────────

    def _record_with_integrity(self, entry: AuditEntry) -> str:
        """
        해시 체인과 함께 기록.

        FAIL-OPEN 정책:
        - 기록 실패 시에도 비즈니스 로직을 차단하지 않음
        - fallback_to_stdout 활성화 시 stdout에 최소한의 기록
        - fail_open=False로 Fail-Secure 모드 가능

        Args:
            entry: 감사 엔트리

        Returns:
            감사 로그 ID
        """
        with self._lock:
            # 엔트리를 딕셔너리로 변환
            entry_dict = entry.to_dict()

            # 해시 체인 무결성 정보 추가
            entry_dict = self._hash_manager.add_integrity(entry_dict)

            # 무결성 정보를 details에 포함
            entry.details["integrity"] = entry_dict.get("integrity", {})

            # WAL 기록 (활성화된 경우)
            wal_seq = None
            if self._wal_enabled and self._wal:
                try:
                    wal_seq = self._wal.write(entry_dict)
                except Exception as e:
                    logger.warning(
                        "continuous_audit.wal_write_failed",
                        error=e,
                    )

            # Fail-Open 패턴으로 기록
            try:
                self.audit_adapter.log(entry)

                # WAL 커밋 (성공 시) — `mark_processed` is a PRO-impl extension;
                # OSS WriteAheadLog is write-only, so duck-type the call.
                if wal_seq is not None and self._wal:
                    try:
                        mark_processed = getattr(self._wal, "mark_processed", None)
                        if mark_processed is not None:
                            mark_processed(wal_seq)
                    except Exception as e:
                        logger.warning(
                            "continuous_audit.wal_commit_failed",
                            error=e,
                        )

                # Checkpoint 저장 (Back-pressure 적용)
                if wal_seq is not None and self._checkpoint_strategy:
                    self._maybe_save_checkpoint(wal_seq, entry_dict)

            except Exception as e:
                self._failed_write_count += 1

                if self._fallback_to_stdout:
                    # Fallback: stdout에 최소한의 기록
                    import sys

                    print(
                        f"[FALLBACK_AUDIT_LOG] {entry.action}: {entry.to_json()}",
                        file=sys.stderr,
                    )

                if not self._fail_open:
                    # Fail-Secure 모드: 예외 전파
                    raise

                logger.warning(
                    "continuous_audit.write_failed_fail_open",
                    error=e,
                    failed_write_count=self._failed_write_count,
                )

            # ID 생성 (timestamp + sequence)
            integrity = entry_dict.get("integrity", {})
            audit_id = f"audit-{entry.timestamp.strftime('%Y%m%d%H%M%S')}-{integrity.get('sequence', 0):06d}"

            logger.debug(
                "continuous_audit.recorded",
                entry_action=entry.action,
                audit_id=audit_id,
            )

            return audit_id

    def get_stats(self) -> dict[str, Any]:
        """감사 기록기 통계 반환."""
        return {
            "failed_write_count": self._failed_write_count,
            "fail_open": self._fail_open,
            "fallback_to_stdout": self._fallback_to_stdout,
            "wal_enabled": self._wal_enabled,
            "chain_state": self._hash_manager.get_state(),
            "records_since_checkpoint": self._records_since_checkpoint,
            "checkpoint_save_interval": self._checkpoint_save_interval,
        }

    def _maybe_save_checkpoint(self, wal_seq: int, entry_dict: dict[str, Any]) -> None:
        """
        Back-pressure를 적용한 체크포인트 저장.

        N번 기록마다 또는 최대 저장 간격 초과 시에만 저장.
        sync_worker.py의 Back-pressure 패턴과 동일.
        """
        self._records_since_checkpoint += 1

        should_save = (
            self._records_since_checkpoint >= self._checkpoint_save_interval
            or time.time() - self._last_checkpoint_time
            >= self._checkpoint_save_max_seconds
        )

        if not should_save:
            return

        try:
            from baldur.audit.checkpoint import UnifiedCheckpointData

            checkpoint_data = UnifiedCheckpointData(
                wal_sequence=wal_seq,
                checksum=entry_dict.get("integrity", {}).get("hash"),
            )
            assert self._checkpoint_strategy is not None  # caller-side truthy guard
            self._checkpoint_strategy.save(
                self._checkpoint_namespace,
                checkpoint_data,
            )

            # 저장 성공 시 카운터 리셋
            self._records_since_checkpoint = 0
            self._last_checkpoint_time = time.time()

            logger.debug(
                "continuous_audit.checkpoint_saved",
                wal_seq=wal_seq,
            )

        except Exception as e:
            logger.warning(
                "continuous_audit.checkpoint_save_failed",
                error=e,
            )

    def force_save_checkpoint(self, wal_seq: int | None = None) -> None:
        """
        체크포인트 강제 저장 (Back-pressure 무시).

        종료 시그널, 에러 복구 등 즉시 저장이 필요한 경우 사용.
        """
        if not self._checkpoint_strategy:
            return

        try:
            from baldur.audit.checkpoint import UnifiedCheckpointData

            checkpoint_data = UnifiedCheckpointData(
                wal_sequence=wal_seq or 0,
            )
            self._checkpoint_strategy.save(
                self._checkpoint_namespace,
                checkpoint_data,
            )

            self._records_since_checkpoint = 0
            self._last_checkpoint_time = time.time()

            logger.info(
                "continuous_audit.checkpoint_force_saved",
                wal_seq=wal_seq,
            )

        except Exception as e:
            logger.warning(
                "continuous_audit.checkpoint_force_save_failed",
                error=e,
            )

    def _send_alert(self, channel: str, data: dict[str, Any]) -> None:
        """알림 발송."""
        if self.alert_callback:
            try:
                self.alert_callback(channel, data)
            except Exception as e:
                logger.warning(
                    "continuous_audit.alert_callback_failed",
                    error=e,
                )

        # 설정된 채널로 알림 (확장 가능)
        if channel in self.config.alert_channels or "all" in self.config.alert_channels:
            logger.info(
                "continuous_audit.alert",
                channel=channel,
                data=data,
            )


# =============================================================================
# Singleton Management
# =============================================================================

_recorder_instance: ContinuousAuditRecorder | None = None
_recorder_lock = threading.Lock()


def get_continuous_audit_recorder() -> ContinuousAuditRecorder:
    """
    ContinuousAuditRecorder singleton instance.

    Uses double-check locking for thread safety.
    Adapter is resolved via get_audit_adapter() singleton
    (priority: set → Registry → File → Null).

    Returns:
        ContinuousAuditRecorder instance
    """
    global _recorder_instance

    if _recorder_instance is not None:
        return _recorder_instance

    with _recorder_lock:
        if _recorder_instance is not None:
            return _recorder_instance

        from baldur.adapters.audit.singleton import get_audit_adapter

        adapter = get_audit_adapter()
        config = AuditConfig.get_default()
        _recorder_instance = ContinuousAuditRecorder(
            audit_adapter=adapter,
            config=config,
        )
        logger.debug("continuous_audit.recorder_initialized")
        return _recorder_instance


def reset_continuous_audit_recorder() -> None:
    """
    Reset ContinuousAuditRecorder singleton (for testing).

    Ensures test isolation by clearing the cached instance.
    """
    global _recorder_instance

    with _recorder_lock:
        _recorder_instance = None
        logger.debug("continuous_audit.recorder_reset")
