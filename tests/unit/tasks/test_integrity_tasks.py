"""
integrity_tasks 단위 테스트.

테스트 대상:
    - verify_hash_chain_integrity: 백그라운드 해시체인 검증 메인 함수
    - _verify_with_retry: 재시도 포함 검증 로직
    - _merkle_spot_check: Merkle 스팟체크 위임
    - _get_entries_since_last_anchor: Anchor 이후 엔트리 로드
    - _alert_integrity_violation: 위반 알림 발송
    - get_integrity_beat_schedule: Celery Beat 스케줄 구성
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.tasks.integrity_tasks import (
    _alert_integrity_violation,
    _get_entries_since_last_anchor,
    _merkle_spot_check,
    _verify_with_retry,
    get_integrity_beat_schedule,
    verify_hash_chain_integrity,
)

# ---------------------------------------------------------------------------
# 패치 경로 상수 — integrity_tasks.py 내 로컬 import 대상 모듈 기준
# verify_hash_chain_integrity 내부:
#     from baldur.adapters.redis import get_redis_client
#     from baldur.adapters.cache.redis_adapter import RedisDistributedLock
#     from baldur.audit.integrity import HashChainVerifier, get_integrity_health_score
#     from baldur.settings.audit_integrity import get_audit_integrity_settings
# _get_entries_since_last_anchor 내부:
#     from baldur.audit.cascade_auditor import get_cascade_event_auditor
# _merkle_spot_check 내부:
#     from baldur.audit.integrity.merkle_spot_checker import MerkleSpotChecker
# _alert_integrity_violation 내부:
#     from baldur_pro.services.audit.base import _write_to_wal
# ---------------------------------------------------------------------------
_PATCH_REDIS_CLIENT = "baldur.adapters.redis.get_redis_client"
_PATCH_LOCK_CLS = "baldur.adapters.cache.redis_adapter.RedisDistributedLock"
_PATCH_VERIFIER_CLS = "baldur.audit.integrity.HashChainVerifier"
_PATCH_HEALTH_SCORE = "baldur.audit.integrity.get_integrity_health_score"
_PATCH_SETTINGS = "baldur.settings.audit_integrity.get_audit_integrity_settings"
_PATCH_GET_ENTRIES = "baldur.tasks.integrity_tasks._get_entries_since_last_anchor"
_PATCH_VERIFY_RETRY = "baldur.tasks.integrity_tasks._verify_with_retry"
_PATCH_MERKLE_CHECK = "baldur.tasks.integrity_tasks._merkle_spot_check"
_PATCH_ALERT = "baldur.tasks.integrity_tasks._alert_integrity_violation"
_PATCH_MERKLE_CHECKER_CLS = (
    "baldur.audit.integrity.merkle_spot_checker.MerkleSpotChecker"
)
_PATCH_WRITE_WAL = "baldur_pro.services.audit.base._write_to_wal"
_PATCH_AUDITOR = "baldur.audit.cascade_auditor.get_cascade_event_auditor"


# =============================================================================
# verify_hash_chain_integrity 동작 검증
# =============================================================================


class TestVerifyHashChainIntegrityBehavior:
    """백그라운드 해시체인 검증 메인 함수 동작 검증."""

    @patch(_PATCH_SETTINGS)
    @patch(_PATCH_REDIS_CLIENT)
    def test_redis_unavailable_returns_skipped(self, mock_redis, mock_settings):
        """Redis 불가 시 skipped=True를 반환한다."""
        mock_redis.return_value = None

        result = verify_hash_chain_integrity()

        assert result["valid"] is True
        assert result["skipped"] is True
        assert result["reason"] == "redis_unavailable"

    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_HEALTH_SCORE)
    @patch(_PATCH_VERIFIER_CLS)
    @patch(_PATCH_LOCK_CLS)
    @patch(_PATCH_SETTINGS)
    @patch(_PATCH_REDIS_CLIENT)
    def test_lock_contention_returns_skipped(
        self,
        mock_redis,
        mock_settings,
        mock_lock_cls,
        mock_verifier_cls,
        mock_health,
        mock_get_entries,
    ):
        """락 경합 시 skipped=True를 반환한다."""
        mock_redis.return_value = MagicMock()
        mock_settings.return_value.hash_chain_lock_timeout = 60
        lock_instance = MagicMock()
        lock_instance.acquire.return_value = False
        mock_lock_cls.return_value = lock_instance

        result = verify_hash_chain_integrity()

        assert result["valid"] is True
        assert result["skipped"] is True
        assert result["reason"] == "lock_contention"

    @patch(_PATCH_VERIFY_RETRY)
    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_HEALTH_SCORE)
    @patch(_PATCH_VERIFIER_CLS)
    @patch(_PATCH_LOCK_CLS)
    @patch(_PATCH_SETTINGS)
    @patch(_PATCH_REDIS_CLIENT)
    def test_small_dataset_uses_full_chain(
        self,
        mock_redis,
        mock_settings,
        mock_lock_cls,
        mock_verifier_cls,
        mock_health,
        mock_get_entries,
        mock_verify_retry,
    ):
        """엔트리 수가 merkle_threshold 미만이면 full_chain 전략을 사용한다."""
        mock_redis.return_value = MagicMock()
        settings = MagicMock()
        settings.hash_chain_lock_timeout = 60
        settings.background_verify_merkle_threshold = 10000
        settings.max_verification_retries = 3
        mock_settings.return_value = settings

        lock_instance = MagicMock()
        lock_instance.acquire.return_value = True
        mock_lock_cls.return_value = lock_instance

        mock_get_entries.return_value = [{"seq": i} for i in range(100)]
        mock_verify_retry.return_value = (True, None)

        result = verify_hash_chain_integrity(use_merkle_spot_check=True)

        assert result["strategy"] == "full_chain"
        assert result["valid"] is True
        mock_verify_retry.assert_called_once()

    @patch(_PATCH_MERKLE_CHECK)
    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_HEALTH_SCORE)
    @patch(_PATCH_VERIFIER_CLS)
    @patch(_PATCH_LOCK_CLS)
    @patch(_PATCH_SETTINGS)
    @patch(_PATCH_REDIS_CLIENT)
    def test_large_dataset_uses_merkle(
        self,
        mock_redis,
        mock_settings,
        mock_lock_cls,
        mock_verifier_cls,
        mock_health,
        mock_get_entries,
        mock_merkle,
    ):
        """엔트리 수가 merkle_threshold 이상이면 merkle_spot_check을 사용한다."""
        mock_redis.return_value = MagicMock()
        settings = MagicMock()
        settings.hash_chain_lock_timeout = 60
        settings.background_verify_merkle_threshold = 50  # 낮은 임계값
        settings.merkle_block_size = 10
        mock_settings.return_value = settings

        lock_instance = MagicMock()
        lock_instance.acquire.return_value = True
        mock_lock_cls.return_value = lock_instance

        mock_get_entries.return_value = [{"seq": i} for i in range(100)]
        mock_merkle.return_value = {
            "valid": True,
            "strategy": "merkle_spot_check",
            "total_entries": 100,
            "total_blocks": 10,
            "blocks_checked": 10,
            "blocks_failed": 0,
            "failed_block_ids": [],
            "errors": [],
        }

        result = verify_hash_chain_integrity(use_merkle_spot_check=True)

        mock_merkle.assert_called_once()
        assert result["valid"] is True

    @patch(_PATCH_ALERT)
    @patch(_PATCH_VERIFY_RETRY)
    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_HEALTH_SCORE)
    @patch(_PATCH_VERIFIER_CLS)
    @patch(_PATCH_LOCK_CLS)
    @patch(_PATCH_SETTINGS)
    @patch(_PATCH_REDIS_CLIENT)
    def test_violation_triggers_alert_and_chain_break(
        self,
        mock_redis,
        mock_settings,
        mock_lock_cls,
        mock_verifier_cls,
        mock_health,
        mock_get_entries,
        mock_verify_retry,
        mock_alert,
    ):
        """무결성 위반 시 alert + record_chain_break가 호출된다."""
        mock_redis.return_value = MagicMock()
        settings = MagicMock()
        settings.hash_chain_lock_timeout = 60
        settings.background_verify_merkle_threshold = 10000
        settings.max_verification_retries = 3
        mock_settings.return_value = settings

        lock_instance = MagicMock()
        lock_instance.acquire.return_value = True
        mock_lock_cls.return_value = lock_instance

        health_instance = MagicMock()
        mock_health.return_value = health_instance

        mock_get_entries.return_value = [{"seq": i} for i in range(10)]
        mock_verify_retry.return_value = (False, "hash mismatch at seq 5")

        result = verify_hash_chain_integrity(use_merkle_spot_check=False)

        assert result["valid"] is False
        health_instance.record_chain_break.assert_called_once()
        mock_alert.assert_called_once()

    @patch(_PATCH_VERIFY_RETRY)
    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_HEALTH_SCORE)
    @patch(_PATCH_VERIFIER_CLS)
    @patch(_PATCH_LOCK_CLS)
    @patch(_PATCH_SETTINGS)
    @patch(_PATCH_REDIS_CLIENT)
    def test_valid_result_calls_record_recovery(
        self,
        mock_redis,
        mock_settings,
        mock_lock_cls,
        mock_verifier_cls,
        mock_health,
        mock_get_entries,
        mock_verify_retry,
    ):
        """검증 성공 시 record_recovery()가 호출된다."""
        mock_redis.return_value = MagicMock()
        settings = MagicMock()
        settings.hash_chain_lock_timeout = 60
        settings.background_verify_merkle_threshold = 10000
        settings.max_verification_retries = 3
        mock_settings.return_value = settings

        lock_instance = MagicMock()
        lock_instance.acquire.return_value = True
        mock_lock_cls.return_value = lock_instance

        health_instance = MagicMock()
        mock_health.return_value = health_instance

        entries = [{"seq": i} for i in range(5)]
        mock_get_entries.return_value = entries
        mock_verify_retry.return_value = (True, None)

        verify_hash_chain_integrity()

        health_instance.record_recovery.assert_called_once()
        call_kwargs = health_instance.record_recovery.call_args.kwargs
        assert call_kwargs["event_type"] == "background_verify_ok"
        assert call_kwargs["sequences_affected"] == 5

    @patch(_PATCH_REDIS_CLIENT)
    def test_top_level_exception_returns_valid_false(self, mock_redis):
        """최상위 예외 시 valid=False를 반환한다."""
        mock_redis.side_effect = RuntimeError("Connection refused")

        result = verify_hash_chain_integrity()

        assert result["valid"] is False
        assert "error" in result

    @patch(_PATCH_VERIFY_RETRY)
    @patch(_PATCH_GET_ENTRIES)
    @patch(_PATCH_HEALTH_SCORE)
    @patch(_PATCH_VERIFIER_CLS)
    @patch(_PATCH_LOCK_CLS)
    @patch(_PATCH_SETTINGS)
    @patch(_PATCH_REDIS_CLIENT)
    def test_lock_is_always_released(
        self,
        mock_redis,
        mock_settings,
        mock_lock_cls,
        mock_verifier_cls,
        mock_health,
        mock_get_entries,
        mock_verify_retry,
    ):
        """성공이든 실패든 락은 반드시 해제된다."""
        mock_redis.return_value = MagicMock()
        settings = MagicMock()
        settings.hash_chain_lock_timeout = 60
        settings.background_verify_merkle_threshold = 10000
        settings.max_verification_retries = 3
        mock_settings.return_value = settings

        lock_instance = MagicMock()
        lock_instance.acquire.return_value = True
        mock_lock_cls.return_value = lock_instance

        mock_get_entries.return_value = [{"seq": 1}]
        mock_verify_retry.side_effect = RuntimeError("Unexpected error")

        verify_hash_chain_integrity()

        lock_instance.release.assert_called_once()


# =============================================================================
# _verify_with_retry 동작 검증
# =============================================================================


class TestVerifyWithRetryBehavior:
    """재시도 포함 검증 로직 동작 검증."""

    def test_valid_on_first_try_returns_immediately(self):
        """첫 시도 성공 시 재시도 없이 즉시 반환한다."""
        verifier = MagicMock()
        verifier.verify_chain.return_value = (True, None)
        loader = MagicMock(return_value=[{"seq": 1}])

        is_valid, error = _verify_with_retry(verifier, loader, max_retries=3)

        assert is_valid is True
        assert error is None
        assert loader.call_count == 1

    def test_succeeds_on_retry(self):
        """첫 시도 실패 후 재시도에서 성공하면 True를 반환한다."""
        verifier = MagicMock()
        verifier.verify_chain.side_effect = [
            (False, "hash mismatch"),  # 1st fail
            (True, None),  # 2nd success
        ]
        loader = MagicMock(return_value=[{"seq": 1}])

        is_valid, error = _verify_with_retry(verifier, loader, max_retries=3)

        assert is_valid is True
        assert error is None
        assert loader.call_count == 2

    def test_all_retries_fail_returns_last_error(self):
        """모든 재시도 실패 시 마지막 에러 메시지를 반환한다."""
        verifier = MagicMock()
        verifier.verify_chain.return_value = (False, "persistent error")
        loader = MagicMock(return_value=[{"seq": 1}])

        is_valid, error = _verify_with_retry(verifier, loader, max_retries=3)

        assert is_valid is False
        assert error == "persistent error"
        assert loader.call_count == 3

    def test_empty_entries_returns_valid(self):
        """엔트리가 비었으면 valid=True를 반환한다."""
        verifier = MagicMock()
        loader = MagicMock(return_value=[])

        is_valid, error = _verify_with_retry(verifier, loader, max_retries=3)

        assert is_valid is True
        assert error is None
        verifier.verify_chain.assert_not_called()

    def test_reloads_entries_each_retry(self):
        """매 재시도마다 entries_loader를 호출하여 소스를 리로드한다."""
        verifier = MagicMock()
        verifier.verify_chain.return_value = (False, "error")
        loader = MagicMock(return_value=[{"seq": 1}])

        _verify_with_retry(verifier, loader, max_retries=3)

        assert loader.call_count == 3


# =============================================================================
# _merkle_spot_check 동작 검증
# =============================================================================


class TestMerkleSpotCheckBehavior:
    """Merkle 스팟체크 위임 동작 검증."""

    @patch(_PATCH_MERKLE_CHECKER_CLS)
    def test_delegates_to_merkle_spot_checker(self, mock_checker_cls):
        """MerkleSpotChecker.spot_check()에 위임한다."""
        mock_instance = MagicMock()
        mock_instance.spot_check.return_value = {
            "valid": True,
            "strategy": "merkle_spot_check",
        }
        mock_checker_cls.return_value = mock_instance

        entries = [{"seq": 1}]
        result = _merkle_spot_check(entries, block_size=100)

        mock_checker_cls.assert_called_once_with(block_size=100)
        mock_instance.spot_check.assert_called_once_with(entries)
        assert result["valid"] is True


# =============================================================================
# _alert_integrity_violation 동작 검증
# =============================================================================


class TestAlertIntegrityViolationBehavior:
    """무결성 위반 알림 동작 검증."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @patch(_PATCH_WRITE_WAL)
    def test_writes_violation_to_wal(self, mock_write):
        """WAL에 INTEGRITY_VIOLATION 이벤트를 기록한다."""
        result = {
            "checked": 10,
            "errors": ["hash mismatch"],
            "strategy": "full_chain",
        }

        _alert_integrity_violation("test_namespace", result)

        mock_write.assert_called_once()
        call_kwargs = mock_write.call_args.kwargs
        assert call_kwargs["event_type"] == "INTEGRITY_VIOLATION"
        assert call_kwargs["source"] == "BackgroundIntegrityVerifier"
        assert call_kwargs["success"] is False

    @patch(_PATCH_WRITE_WAL)
    def test_wal_write_failure_does_not_propagate(self, mock_write):
        """WAL 기록 실패 시 예외가 전파되지 않는다."""
        mock_write.side_effect = RuntimeError("WAL down")

        # 예외가 전파되지 않아야 함
        _alert_integrity_violation("test_namespace", {"errors": []})


# =============================================================================
# _get_entries_since_last_anchor 동작 검증
# =============================================================================


class TestGetEntriesSinceLastAnchorBehavior:
    """Anchor 이후 엔트리 로드 동작 검증."""

    @patch(_PATCH_AUDITOR)
    def test_returns_entries_from_auditor(self, mock_get_auditor):
        """cascade auditor에서 이벤트를 조회한다."""
        mock_auditor = MagicMock()
        mock_event = MagicMock()
        mock_event.to_dict.return_value = {"seq": 1, "data": "test"}
        mock_auditor.get_recent_events.return_value = [mock_event]
        mock_get_auditor.return_value = mock_auditor

        result = _get_entries_since_last_anchor("test_ns")

        assert len(result) == 1
        assert result[0] == {"seq": 1, "data": "test"}

    @patch(_PATCH_AUDITOR)
    def test_exception_returns_empty_list(self, mock_get_auditor):
        """예외 발생 시 빈 목록을 반환한다."""
        mock_get_auditor.side_effect = RuntimeError("DB unavailable")

        result = _get_entries_since_last_anchor("test_ns")

        assert result == []


# =============================================================================
# get_integrity_beat_schedule 동작 검증
# =============================================================================


class TestGetIntegrityBeatScheduleBehavior:
    """Celery Beat 스케줄 구성 동작 검증."""

    def test_returns_two_schedules(self):
        """5분 주기와 1일 주기 두 개의 스케줄을 포함한다."""
        schedule = get_integrity_beat_schedule()

        if schedule:
            assert "verify-hash-chain-integrity" in schedule
            assert "verify-hash-chain-full" in schedule

    def test_five_minute_schedule_uses_merkle(self):
        """5분 스케줄은 merkle 스팟체크를 활성화한다."""
        schedule = get_integrity_beat_schedule()

        if schedule:
            five_min = schedule["verify-hash-chain-integrity"]
            assert five_min["kwargs"]["use_merkle_spot_check"] is True

    def test_daily_schedule_disables_merkle(self):
        """1일 스케줄은 merkle 스팟체크를 비활성화한다."""
        schedule = get_integrity_beat_schedule()

        if schedule:
            daily = schedule["verify-hash-chain-full"]
            assert daily["kwargs"]["use_merkle_spot_check"] is False

    def test_queue_is_integrity(self):
        """모든 스케줄의 큐는 'integrity'이다."""
        schedule = get_integrity_beat_schedule()

        if schedule:
            for _name, config in schedule.items():
                assert config["options"]["queue"] == "integrity"
