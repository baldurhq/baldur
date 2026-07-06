"""
WAL Recovery for Hash Chain.

Contains:
- HashChainRecoveryWALEntry: WAL entry dataclass
- HashChainWALRecovery: WAL-based recovery for hash chain operations
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


@dataclass
class HashChainRecoveryWALEntry:
    """WAL entry for hash chain operation."""

    sequence: int
    operation: str  # "add_integrity", "commit", "abort"
    entry_data: dict[str, Any]
    timestamp: str
    pod_id: str
    committed: bool = False


class HashChainWALRecovery:  # verified-by: test_recover_uncommitted_entries
    """
    WAL-based recovery for hash chain operations.

    .. note::
        **Tier**: Dormant (compliance-grade enhancement, no standalone demand)
        **Status**: Not auto-wired. Available for custom integration engagements.
        PRO Audit (full) uses the basic file-based ``HashChainManager`` in
        ``audit/integrity/`` instead.

    Ensures zero data loss by recording operations in WAL before
    attempting Redis writes. On failure, WAL entries are replayed.

    Pattern source:
        adapters/resilient/backend.py#L183-230
        audit/wal.py

    Usage:
        recovery = HashChainWALRecovery(wal_dir, redis_client)
        recovery.recover_on_startup()  # Called during app initialization
    """

    def __init__(
        self,
        wal_dir: Path,
        redis_client: Any | None = None,
        key_prefix: str = "baldur:",
    ):
        """
        Initialize WAL recovery.

        Args:
            wal_dir: Directory for WAL files
            redis_client: Redis client for recovery
            key_prefix: Prefix for Redis keys
        """
        self._wal_dir = Path(wal_dir)
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._lock = threading.RLock()

        # WAL file management (writer created lazily per date)
        self._writer = None
        self._writer_date: str | None = None
        self._wal_sequence = 0

        # Recovery state
        self._recovery_done = False
        self._recovered_count = 0
        self._failed_count = 0

        # Ensure WAL directory exists
        self._wal_dir.mkdir(parents=True, exist_ok=True)

    def _get_or_create_writer(self):
        """Get or create a JSONLWriter for today's date."""
        from baldur.audit.wal._jsonl import JSONLWriter

        date_str = utc_now().strftime("%Y%m%d")
        if self._writer is None or self._writer_date != date_str:
            if self._writer is not None:
                self._writer.close()
            wal_file = self._wal_dir / f"hash_chain_wal_{date_str}.jsonl"
            self._writer = JSONLWriter(file_path=wal_file, fsync=True)
            self._writer_date = date_str
        return self._writer

    def write_wal_entry(
        self,
        operation: str,
        entry: dict[str, Any],
    ) -> int:
        """
        Write entry to WAL before main operation.

        Args:
            operation: Operation type
            entry: Entry data

        Returns:
            WAL sequence number
        """
        with self._lock:
            self._wal_sequence += 1
            wal_seq = self._wal_sequence

            timestamp = utc_now().isoformat()
            pod_id = os.environ.get("HOSTNAME", os.environ.get("POD_NAME", "unknown"))

            wal_entry = {
                "wal_sequence": wal_seq,
                "operation": operation,
                "entry_data": entry,
                "timestamp": timestamp,
                "pod_id": pod_id,
                "committed": False,
            }

            self._get_or_create_writer().append(wal_entry)
            return wal_seq

    def mark_wal_committed(self, wal_sequence: int) -> None:
        """Mark WAL entry as committed (successfully written to Redis)."""
        with self._lock:
            self._get_or_create_writer().append(
                {
                    "_marker": "COMMIT",
                    "wal_sequence": wal_sequence,
                    "operation": "COMMIT",
                    "timestamp": utc_now().isoformat(),
                }
            )

    def recover_on_startup(self) -> dict[str, Any]:
        """
        Recover uncommitted entries from WAL on startup.

        This is called during application initialization to replay
        any entries that were written to WAL but not committed to Redis.

        Returns:
            Recovery result dictionary
        """
        if self._recovery_done:
            return {"status": "already_done", "recovered": 0}

        result: dict[str, Any] = {
            "status": "success",
            "wal_files_scanned": 0,
            "entries_found": 0,
            "entries_recovered": 0,
            "entries_failed": 0,
            "entries_already_committed": 0,
            "idempotency_skipped": 0,
        }

        try:
            wal_files = sorted(self._wal_dir.glob("hash_chain_wal_*.jsonl"))
            result["wal_files_scanned"] = len(wal_files)

            for wal_file in wal_files:
                file_result = self._recover_from_wal_file(wal_file)
                result["entries_found"] += file_result["found"]
                result["entries_recovered"] += file_result["recovered"]
                result["entries_failed"] += file_result["failed"]
                result["entries_already_committed"] += file_result["already_committed"]
                result["idempotency_skipped"] += file_result.get(
                    "idempotency_skipped", 0
                )

            self._recovery_done = True
            self._recovered_count = result["entries_recovered"]
            self._failed_count = result["entries_failed"]

            logger.info(
                "hash_chain_wal.recovery_completed",
                recovery_result=result,
            )

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            logger.exception(
                "watchdog.recovery_failed",
                error=e,
            )

        return result

    def _recover_from_wal_file(self, wal_file: Path) -> dict[str, int]:  # noqa: C901
        """WAL 파일에서 미커밋 엔트리 복구 (Redis 파이프라인 배치)."""
        from baldur.audit.wal._jsonl import JSONLReader

        result = {
            "found": 0,
            "recovered": 0,
            "failed": 0,
            "already_committed": 0,
            "idempotency_skipped": 0,
        }

        entries: dict[int, dict[str, Any]] = {}
        committed_sequences: set[int] = set()

        try:
            # Pass 1: 수집
            for entry in JSONLReader.iter_entries(wal_file):
                wal_seq = entry.get("wal_sequence")
                operation = entry.get("operation")

                if wal_seq is None:
                    continue  # malformed entry: skip

                if operation == "COMMIT" or entry.get("_marker") == "COMMIT":
                    committed_sequences.add(wal_seq)
                elif operation in ("add_integrity", "write"):
                    entries[wal_seq] = entry
                    result["found"] += 1

            # 커밋 필터링
            uncommitted = {
                seq: entry
                for seq, entry in entries.items()
                if seq not in committed_sequences
            }
            result["already_committed"] = len(entries) - len(uncommitted)

            if not uncommitted:
                return result

            # Pass 2: 배치 멱등성 체크 (Redis 파이프라인)
            batch_size = 1000
            seqs = list(uncommitted.keys())
            recovered_seqs: list[int] = []

            for i in range(0, len(seqs), batch_size):
                batch_seqs = seqs[i : i + batch_size]
                duplicates = self._batch_check_idempotency(batch_seqs, "redis_replay")

                for seq in batch_seqs:
                    if seq in duplicates:
                        result["idempotency_skipped"] += 1
                        continue

                    entry = uncommitted[seq]
                    if self._replay_entry(entry):
                        result["recovered"] += 1
                        recovered_seqs.append(seq)
                    else:
                        result["failed"] += 1

            # 배치 멱등성 마킹
            if recovered_seqs:
                self._batch_mark_processed(recovered_seqs, "redis_replay")

        except Exception as e:
            logger.exception(
                "hash_chain_wal.error_reading",
                wal_file=wal_file,
                error=e,
            )

        return result

    def _batch_check_idempotency(self, wal_seqs: list[int], operation: str) -> set[int]:
        """
        배치 멱등성 검사 (Redis 파이프라인).

        건별 SETNX 대신 파이프라인으로 1000건씩 검사.
        런타임 실패 시 건별 폴백 + 연속 실패 short-circuit으로 안전하게 처리한다.
        """
        try:
            from baldur.services.idempotency import (
                IdempotencyKey,
                IdempotencyService,
            )

            service = IdempotencyService()
            keys = [
                IdempotencyKey.for_wal_recovery(
                    wal_entry_id=str(seq),
                    operation=operation,
                )
                for seq in wal_seqs
            ]
            results = service.batch_check(keys)
            duplicates = {
                wal_seqs[i] for i, result in enumerate(results) if result.is_duplicate
            }
            logger.debug(
                "wal.batch_idempotency_checked",
                batch_size=len(wal_seqs),
                duplicates_found=len(duplicates),
            )
            return duplicates

        except (ImportError, AttributeError):
            # batch_check 미구현 시 건별 폴백
            return self._individual_check_with_guard(wal_seqs, operation)

        except Exception:
            # 런타임 실패 (Redis 네트워크 파티션, 클러스터 슬롯 변경 등)
            logger.warning(
                "wal.batch_idempotency_fallback",
                batch_size=len(wal_seqs),
            )
            return self._individual_check_with_guard(wal_seqs, operation)

    def _individual_check_with_guard(
        self, wal_seqs: list[int], operation: str
    ) -> set[int]:
        """
        건별 멱등성 체크 + 연속 실패 short-circuit.

        Redis 완전 다운 시 1,000건 × socket_timeout(5초) = 5,000초 블로킹을
        방지하기 위해 연속 5회 실패 시 나머지를 즉시 스킵한다.
        """
        max_consecutive_failures = 5
        consecutive_failures = 0
        duplicates: set[int] = set()

        for idx, seq in enumerate(wal_seqs):
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "wal.idempotency_fallback_short_circuited",
                    skipped=len(wal_seqs) - idx,
                    consecutive_failures=consecutive_failures,
                )
                break

            try:
                if self._is_duplicate_via_idempotency(seq, operation):
                    duplicates.add(seq)
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1

        return duplicates

    def _batch_mark_processed(self, wal_seqs: list[int], operation: str) -> None:
        """
        배치 멱등성 마킹 (Redis 파이프라인).

        런타임 실패 시 건별 폴백 + 연속 실패 short-circuit 동일 적용.
        """
        try:
            from baldur.services.idempotency import (
                IdempotencyKey,
                IdempotencyService,
            )

            service = IdempotencyService()
            keys = [
                IdempotencyKey.for_wal_recovery(
                    wal_entry_id=str(seq),
                    operation=operation,
                )
                for seq in wal_seqs
            ]
            service.batch_mark_as_processed(keys, ttl=3600)
            logger.debug(
                "wal.batch_idempotency_marked",
                batch_size=len(wal_seqs),
            )

        except (ImportError, AttributeError):
            self._individual_mark_with_guard(wal_seqs, operation)

        except Exception:
            logger.warning(
                "wal.batch_mark_processed_fallback",
                batch_size=len(wal_seqs),
            )
            self._individual_mark_with_guard(wal_seqs, operation)

    def _individual_mark_with_guard(self, wal_seqs: list[int], operation: str) -> None:
        """건별 멱등성 마킹 + 연속 실패 short-circuit."""
        max_consecutive_failures = 5
        consecutive_failures = 0

        for idx, seq in enumerate(wal_seqs):
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "wal.mark_processed_fallback_short_circuited",
                    skipped=len(wal_seqs) - idx,
                )
                break

            try:
                self._mark_as_processed_idempotency(seq, operation)
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1

    def _replay_entry(self, wal_entry: dict[str, Any]) -> bool:
        """Replay a single WAL entry to Redis."""
        if not self._redis:
            logger.warning("hash_chain_wal.no_redis_client_replay")
            return False

        try:
            entry_data = wal_entry.get("entry_data", {})
            integrity = entry_data.get("integrity", {})

            # Check if already exists in Redis
            seq_key = f"{self._key_prefix}audit:hash_chain:seq"
            current_seq = self._redis.get(seq_key)
            current_seq = int(current_seq) if current_seq else 0

            entry_seq = integrity.get("sequence", 0)

            if entry_seq <= current_seq:
                # Already processed
                return True

            # Update Redis state
            state_key = f"{self._key_prefix}audit:hash_chain:state"
            current_hash = integrity.get("current_hash", "")
            timestamp = utc_now().isoformat()

            pipe = self._redis.pipeline()
            pipe.set(seq_key, entry_seq)
            pipe.hset(
                state_key,
                mapping={
                    "previous_hash": current_hash,
                    "sequence": str(entry_seq),
                    "updated_at": timestamp,
                    "recovered_from": "wal",
                },
            )
            pipe.execute()

            logger.debug(
                "hash_chain_wal.replayed_entry",
                entry_seq=entry_seq,
            )
            return True

        except Exception as e:
            logger.exception(
                "hash_chain_wal.replay_failed",
                error=e,
            )
            return False

    def _is_duplicate_via_idempotency(self, wal_seq: int, operation: str) -> bool:
        """
        IdempotencyKey를 사용하여 중복 WAL 엔트리인지 확인 (1차 방어).

        Redis 기반 빠른 중복 감지로 불필요한 DB 쓰기를 방지합니다.

        Args:
            wal_seq: WAL 시퀀스 번호
            operation: 복구 작업 유형 (redis_replay, pg_insert 등)

        Returns:
            True if duplicate (should skip), False if new
        """
        try:
            from baldur.services.idempotency import (
                IdempotencyKey,
                IdempotencyService,
            )

            # WAL 복구용 멱등성 키 생성
            key = IdempotencyKey.for_wal_recovery(
                wal_entry_id=str(wal_seq),
                operation=operation,
            )

            service = IdempotencyService()
            result = service.check(key)

            return result.is_duplicate

        except ImportError:
            # IdempotencyService 미사용 환경
            logger.debug("hash_chain_wal.idempotencyservice_available")
            return False
        except Exception as e:
            # 멱등성 검사 실패 시 안전하게 진행 (중복 허용)
            logger.warning(
                "hash_chain_wal.idempotency_check_failed",
                error=e,
            )
            return False

    def _mark_as_processed_idempotency(self, wal_seq: int, operation: str) -> None:
        """
        복구 완료된 WAL 엔트리를 멱등성 캐시에 등록.

        다음 복구 시도에서 중복으로 처리되도록 합니다.

        Args:
            wal_seq: WAL 시퀀스 번호
            operation: 복구 작업 유형
        """
        try:
            from baldur.services.idempotency import (
                IdempotencyKey,
                IdempotencyService,
            )

            key = IdempotencyKey.for_wal_recovery(
                wal_entry_id=str(wal_seq),
                operation=operation,
            )

            service = IdempotencyService()
            # TTL 1시간 (복구 세션 내 중복 방지용)
            service.mark_as_processed(key, ttl=3600)

        except ImportError:
            pass
        except Exception as e:
            logger.warning(
                "hash_chain_wal.mark_processed_failed",
                error=e,
            )

    def cleanup_old_wal_files(self, max_age_days: int = 7) -> int:
        """Remove WAL files older than specified days."""
        from baldur.audit.wal._cleanup import cleanup_by_age

        return cleanup_by_age(self._wal_dir, "hash_chain_wal_*.jsonl", max_age_days)

    def close(self) -> None:
        """Close WAL file handle."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def get_stats(self) -> dict[str, Any]:
        """Get recovery statistics."""
        current_file = None
        if self._writer is not None:
            current_file = str(self._writer.path)
        return {
            "recovery_done": self._recovery_done,
            "recovered_count": self._recovered_count,
            "failed_count": self._failed_count,
            "wal_sequence": self._wal_sequence,
            "current_wal_file": current_file,
        }


__all__ = ["HashChainRecoveryWALEntry", "HashChainWALRecovery"]
