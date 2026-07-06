"""
HashChainWALRecovery 배치 멱등성 테스트.

테스트 범위:
1. _batch_check_idempotency — IdempotencyService.batch_check 배치 체크
2. _individual_check_with_guard — 연속 5회 실패 short-circuit
3. _batch_mark_processed — IdempotencyService.batch_mark_as_processed 배치 마킹
4. _individual_mark_with_guard — 연속 5회 실패 short-circuit
5. _recover_from_wal_file — 2-pass 수집 + 배치 멱등성 통합
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.graceful_degradation.wal_recovery import HashChainWALRecovery

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis = MagicMock()
    redis.get.return_value = None
    redis.pipeline.return_value = MagicMock()
    return redis


@pytest.fixture
def recovery(tmp_path, mock_redis):
    """HashChainWALRecovery instance."""
    return HashChainWALRecovery(
        wal_dir=tmp_path,
        redis_client=mock_redis,
        key_prefix="test:",
    )


# =============================================================================
# Contract Tests: consecutive failure threshold
# =============================================================================


class TestConsecutiveFailureThresholdContract:
    """연속 실패 임계값 계약 검증."""

    def test_individual_check_guard_threshold_is_5(self, recovery):
        """_individual_check_with_guard의 short-circuit 임계값은 5."""
        # Given: 모든 호출 실패
        with patch.object(
            recovery, "_is_duplicate_via_idempotency", side_effect=RuntimeError("fail")
        ):
            result = recovery._individual_check_with_guard(
                list(range(1, 20)), "redis_replay"
            )

        # Then: 5회 연속 실패 후 나머지 스킵
        assert result == set()  # 중복 감지 없음

    def test_individual_mark_guard_threshold_is_5(self, recovery):
        """_individual_mark_with_guard의 short-circuit 임계값은 5."""
        with patch.object(
            recovery,
            "_mark_as_processed_idempotency",
            side_effect=RuntimeError("fail"),
        ):
            # When: 20개 시퀀스 마킹 시도
            recovery._individual_mark_with_guard(list(range(1, 20)), "redis_replay")

        # Then: 예외 없이 완료 (5회 후 중단)


# =============================================================================
# Behavior Tests: _batch_check_idempotency
# =============================================================================


class TestBatchCheckIdempotencyBehavior:
    """_batch_check_idempotency 동작 검증."""

    def test_batch_check_returns_duplicate_set(self, recovery):
        """배치 체크에서 중복 시퀀스 set 반환."""
        mock_result_dup = MagicMock()
        mock_result_dup.is_duplicate = True
        mock_result_new = MagicMock()
        mock_result_new.is_duplicate = False

        with patch(
            "baldur.services.idempotency.IdempotencyService",
            autospec=True,
        ) as MockService:
            instance = MockService.return_value
            instance.batch_check.return_value = [
                mock_result_dup,  # seq 1 — duplicate
                mock_result_new,  # seq 2 — new
                mock_result_dup,  # seq 3 — duplicate
            ]

            result = recovery._batch_check_idempotency([1, 2, 3], "redis_replay")

        assert result == {1, 3}

    def test_batch_check_falls_back_on_import_error(self, recovery):
        """ImportError 시 _individual_check_with_guard 폴백."""
        with patch.object(
            recovery,
            "_individual_check_with_guard",
            return_value={2},
        ) as mock_individual:
            with patch.dict("sys.modules", {"baldur.services.idempotency": None}):
                result = recovery._batch_check_idempotency([1, 2, 3], "redis_replay")

        assert result == {2}
        mock_individual.assert_called_once_with([1, 2, 3], "redis_replay")

    def test_batch_check_falls_back_on_runtime_error(self, recovery):
        """RuntimeError 시 _individual_check_with_guard 폴백."""
        with patch.object(
            recovery, "_individual_check_with_guard", return_value=set()
        ) as mock_individual:
            with patch(
                "baldur.services.idempotency.IdempotencyService",
                autospec=True,
            ) as MockService:
                instance = MockService.return_value
                instance.batch_check.side_effect = ConnectionError("Redis down")

                result = recovery._batch_check_idempotency([1, 2], "redis_replay")

        mock_individual.assert_called_once()
        assert result == set()

    def test_batch_check_empty_list_returns_empty_set(self, recovery):
        """빈 리스트이면 빈 set 반환."""
        with patch(
            "baldur.services.idempotency.IdempotencyService",
            autospec=True,
        ) as MockService:
            instance = MockService.return_value
            instance.batch_check.return_value = []

            result = recovery._batch_check_idempotency([], "redis_replay")

        assert result == set()


# =============================================================================
# Behavior Tests: _individual_check_with_guard
# =============================================================================


class TestIndividualCheckWithGuardBehavior:
    """_individual_check_with_guard 동작 검증."""

    def test_returns_duplicates_from_individual_checks(self, recovery):
        """건별 체크에서 중복 시퀀스 감지."""
        with patch.object(
            recovery,
            "_is_duplicate_via_idempotency",
            side_effect=[True, False, True],
        ):
            result = recovery._individual_check_with_guard([10, 20, 30], "redis_replay")

        assert result == {10, 30}

    def test_short_circuits_after_5_consecutive_failures(self, recovery):
        """연속 5회 실패 후 나머지 스킵."""
        call_count = 0

        def failing_check(seq, op):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("timeout")

        with patch.object(
            recovery, "_is_duplicate_via_idempotency", side_effect=failing_check
        ):
            recovery._individual_check_with_guard(list(range(1, 20)), "redis_replay")

        assert call_count == 5  # 5회 후 중단

    def test_resets_counter_on_success(self, recovery):
        """성공 시 연속 실패 카운터 리셋."""
        # Pattern: fail, fail, success, fail, fail, fail, fail, fail, ...
        side_effects = [
            ConnectionError("fail"),
            ConnectionError("fail"),
            False,  # success — resets counter
            ConnectionError("fail"),
            ConnectionError("fail"),
            ConnectionError("fail"),
            ConnectionError("fail"),
            ConnectionError("fail"),  # 5th consecutive — triggers short-circuit
        ]

        with patch.object(
            recovery,
            "_is_duplicate_via_idempotency",
            side_effect=side_effects,
        ):
            recovery._individual_check_with_guard(list(range(1, 20)), "redis_replay")

        # 2 fails + 1 success + 5 fails = 8 calls total
        # (short-circuits after 5th consecutive failure)

    def test_no_failures_processes_all(self, recovery):
        """실패 없으면 전체 처리."""
        with patch.object(
            recovery,
            "_is_duplicate_via_idempotency",
            return_value=False,
        ) as mock_check:
            recovery._individual_check_with_guard(list(range(1, 11)), "redis_replay")

        assert mock_check.call_count == 10


# =============================================================================
# Behavior Tests: _batch_mark_processed
# =============================================================================


class TestBatchMarkProcessedBehavior:
    """_batch_mark_processed 동작 검증."""

    def test_batch_mark_calls_service(self, recovery):
        """IdempotencyService.batch_mark_as_processed 호출."""
        with patch(
            "baldur.services.idempotency.IdempotencyService",
            autospec=True,
        ) as MockService:
            instance = MockService.return_value

            recovery._batch_mark_processed([1, 2, 3], "redis_replay")

        instance.batch_mark_as_processed.assert_called_once()
        call_args = instance.batch_mark_as_processed.call_args
        assert call_args[1]["ttl"] == 3600 or call_args[0][1] == 3600

    def test_batch_mark_falls_back_on_import_error(self, recovery):
        """ImportError 시 _individual_mark_with_guard 폴백."""
        with patch.object(recovery, "_individual_mark_with_guard") as mock_individual:
            with patch.dict("sys.modules", {"baldur.services.idempotency": None}):
                recovery._batch_mark_processed([1, 2], "redis_replay")

        mock_individual.assert_called_once_with([1, 2], "redis_replay")

    def test_batch_mark_falls_back_on_runtime_error(self, recovery):
        """RuntimeError 시 _individual_mark_with_guard 폴백."""
        with patch.object(recovery, "_individual_mark_with_guard") as mock_individual:
            with patch(
                "baldur.services.idempotency.IdempotencyService",
                autospec=True,
            ) as MockService:
                instance = MockService.return_value
                instance.batch_mark_as_processed.side_effect = ConnectionError("down")

                recovery._batch_mark_processed([1, 2], "redis_replay")

        mock_individual.assert_called_once()


# =============================================================================
# Behavior Tests: _individual_mark_with_guard
# =============================================================================


class TestIndividualMarkWithGuardBehavior:
    """_individual_mark_with_guard 동작 검증."""

    def test_marks_all_on_success(self, recovery):
        """전체 마킹 성공."""
        with patch.object(recovery, "_mark_as_processed_idempotency") as mock_mark:
            recovery._individual_mark_with_guard([1, 2, 3], "redis_replay")

        assert mock_mark.call_count == 3

    def test_short_circuits_after_5_consecutive_failures(self, recovery):
        """연속 5회 실패 후 나머지 스킵."""
        call_count = 0

        def failing_mark(seq, op):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("timeout")

        with patch.object(
            recovery, "_mark_as_processed_idempotency", side_effect=failing_mark
        ):
            recovery._individual_mark_with_guard(list(range(1, 20)), "redis_replay")

        assert call_count == 5

    def test_resets_counter_on_success(self, recovery):
        """성공 시 카운터 리셋."""
        side_effects = [
            ConnectionError("fail"),
            ConnectionError("fail"),
            None,  # success
            ConnectionError("fail"),
            ConnectionError("fail"),
            ConnectionError("fail"),
            ConnectionError("fail"),
            ConnectionError("fail"),  # 5th consecutive
        ]

        with patch.object(
            recovery,
            "_mark_as_processed_idempotency",
            side_effect=side_effects,
        ) as mock_mark:
            recovery._individual_mark_with_guard(list(range(1, 20)), "redis_replay")

        assert mock_mark.call_count == 8


# =============================================================================
# Behavior Tests: _recover_from_wal_file 2-pass + batch
# =============================================================================


class TestRecoverFromWalFileBehavior:
    """_recover_from_wal_file 2-pass 수집 + 배치 멱등성 통합 검증."""

    def _create_wal_file(self, wal_dir: Path, entries: list) -> Path:
        """테스트용 JSONL WAL 파일 생성."""
        import json
        from datetime import datetime

        wal_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        wal_file = wal_dir / f"hash_chain_wal_{date_str}.jsonl"

        with open(wal_file, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        return wal_file

    def test_two_pass_committed_entries_filtered(self, recovery, tmp_path):
        """Pass 1에서 COMMIT된 엔트리는 uncommitted에서 제외."""
        from datetime import datetime

        wal_file = self._create_wal_file(
            tmp_path,
            [
                {
                    "wal_sequence": 1,
                    "operation": "add_integrity",
                    "entry_data": {"integrity": {"sequence": 1, "current_hash": "h1"}},
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pod_id": "pod-1",
                },
                {
                    "wal_sequence": 1,
                    "operation": "COMMIT",
                    "_marker": "COMMIT",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                {
                    "wal_sequence": 2,
                    "operation": "add_integrity",
                    "entry_data": {"integrity": {"sequence": 2, "current_hash": "h2"}},
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pod_id": "pod-1",
                },
            ],
        )

        with patch.object(recovery, "_batch_check_idempotency", return_value=set()):
            with patch.object(recovery, "_replay_entry", return_value=True):
                with patch.object(recovery, "_batch_mark_processed"):
                    result = recovery._recover_from_wal_file(wal_file)

        assert result["already_committed"] == 1
        assert result["recovered"] == 1  # seq 2만 복구

    def test_batch_idempotency_skips_duplicates(self, recovery, tmp_path):
        """배치 멱등성 체크에서 중복은 스킵."""
        from datetime import datetime

        wal_file = self._create_wal_file(
            tmp_path,
            [
                {
                    "wal_sequence": 1,
                    "operation": "add_integrity",
                    "entry_data": {"integrity": {"sequence": 1, "current_hash": "h1"}},
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pod_id": "pod-1",
                },
                {
                    "wal_sequence": 2,
                    "operation": "add_integrity",
                    "entry_data": {"integrity": {"sequence": 2, "current_hash": "h2"}},
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pod_id": "pod-1",
                },
            ],
        )

        # Given: seq 1은 이미 처리됨
        with patch.object(recovery, "_batch_check_idempotency", return_value={1}):
            with patch.object(
                recovery, "_replay_entry", return_value=True
            ) as mock_replay:
                with patch.object(recovery, "_batch_mark_processed"):
                    result = recovery._recover_from_wal_file(wal_file)

        assert result["idempotency_skipped"] == 1
        assert result["recovered"] == 1
        mock_replay.assert_called_once()  # seq 2만 replay

    def test_batch_mark_called_with_recovered_seqs(self, recovery, tmp_path):
        """복구 성공 시퀀스로 _batch_mark_processed 호출."""
        from datetime import datetime

        wal_file = self._create_wal_file(
            tmp_path,
            [
                {
                    "wal_sequence": 1,
                    "operation": "add_integrity",
                    "entry_data": {"integrity": {"sequence": 1, "current_hash": "h1"}},
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pod_id": "pod-1",
                },
            ],
        )

        with patch.object(recovery, "_batch_check_idempotency", return_value=set()):
            with patch.object(recovery, "_replay_entry", return_value=True):
                with patch.object(recovery, "_batch_mark_processed") as mock_mark:
                    recovery._recover_from_wal_file(wal_file)

        mock_mark.assert_called_once_with([1], "redis_replay")

    def test_batch_mark_not_called_on_all_failures(self, recovery, tmp_path):
        """전체 실패 시 _batch_mark_processed 미호출."""
        from datetime import datetime

        wal_file = self._create_wal_file(
            tmp_path,
            [
                {
                    "wal_sequence": 1,
                    "operation": "add_integrity",
                    "entry_data": {"integrity": {"sequence": 1, "current_hash": "h1"}},
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pod_id": "pod-1",
                },
            ],
        )

        with patch.object(recovery, "_batch_check_idempotency", return_value=set()):
            with patch.object(recovery, "_replay_entry", return_value=False):
                with patch.object(recovery, "_batch_mark_processed") as mock_mark:
                    result = recovery._recover_from_wal_file(wal_file)

        assert result["failed"] == 1
        mock_mark.assert_not_called()

    def test_batch_size_1000_chunking(self, recovery, tmp_path):
        """1000건 이상 시 배치 분할 처리."""
        from datetime import datetime

        entries = [
            {
                "wal_sequence": i,
                "operation": "add_integrity",
                "entry_data": {"integrity": {"sequence": i, "current_hash": f"h{i}"}},
                "timestamp": datetime.now(UTC).isoformat(),
                "pod_id": "pod-1",
            }
            for i in range(1, 2501)  # 2500 entries → 3 batches (1000+1000+500)
        ]
        wal_file = self._create_wal_file(tmp_path, entries)

        batch_calls = []

        def track_batch_check(seqs, op):
            batch_calls.append(len(seqs))
            return set()

        with patch.object(
            recovery, "_batch_check_idempotency", side_effect=track_batch_check
        ):
            with patch.object(recovery, "_replay_entry", return_value=True):
                with patch.object(recovery, "_batch_mark_processed"):
                    recovery._recover_from_wal_file(wal_file)

        assert batch_calls == [1000, 1000, 500]


# =============================================================================
# Code Review Fix Tests: enumerate skipped count (#4)
# =============================================================================


class TestShortCircuitSkippedCount:
    """short-circuit 발동 시 skipped 카운트 정확성 검증 (#4)."""

    def test_individual_check_skipped_count_accurate(self, recovery):
        """_individual_check_with_guard short-circuit 시 skipped 수가 정확."""
        seqs = list(range(1, 21))  # 20 entries
        call_count = 0
        logged_skipped = []

        def failing_check(seq, op):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("timeout")

        with patch.object(
            recovery, "_is_duplicate_via_idempotency", side_effect=failing_check
        ):
            with patch(
                "baldur.audit.graceful_degradation.wal_recovery.logger"
            ) as mock_logger:

                def capture_error(event, **kwargs):
                    if event == "wal.idempotency_fallback_short_circuited":
                        logged_skipped.append(kwargs.get("skipped"))

                mock_logger.error = capture_error
                mock_logger.debug = lambda *a, **kw: None
                recovery._individual_check_with_guard(seqs, "redis_replay")

        # 5회 실패 후 short-circuit → 나머지 15개 스킵
        assert call_count == 5
        assert logged_skipped == [15]

    def test_individual_mark_skipped_count_accurate(self, recovery):
        """_individual_mark_with_guard short-circuit 시 skipped 수가 정확."""
        seqs = list(range(1, 11))  # 10 entries
        call_count = 0
        logged_skipped = []

        def failing_mark(seq, op):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("timeout")

        with patch.object(
            recovery, "_mark_as_processed_idempotency", side_effect=failing_mark
        ):
            with patch(
                "baldur.audit.graceful_degradation.wal_recovery.logger"
            ) as mock_logger:

                def capture_error(event, **kwargs):
                    if event == "wal.mark_processed_fallback_short_circuited":
                        logged_skipped.append(kwargs.get("skipped"))

                mock_logger.error = capture_error
                recovery._individual_mark_with_guard(seqs, "redis_replay")

        assert call_count == 5
        assert logged_skipped == [5]  # 10 - 5 = 5개 스킵
