"""
Air-Gap Adapter Unit Tests.

Tests for the Air-Gap storage adapters.
"""

import json
from unittest.mock import MagicMock, patch

from baldur.adapters.airgap.base import (
    AirGapKeys,
    AirGapStorageAdapter,
)
from baldur.adapters.airgap.factory import (
    configure_airgap_adapter,
    get_airgap_adapter,
    reset_airgap_adapter,
)
from baldur.adapters.airgap.null_adapter import NullAirGapAdapter

# =============================================================================
# NullAirGapAdapter Tests
# =============================================================================


class TestNullAirGapAdapter:
    """NullAirGapAdapter 테스트."""

    def setup_method(self):
        """테스트 전 어댑터 리셋."""
        reset_airgap_adapter()
        self.adapter = NullAirGapAdapter()

    def test_is_not_enabled(self):
        """비활성화 상태 확인."""
        assert self.adapter.is_enabled() is False

    def test_write_summary_returns_true(self):
        """쓰기는 항상 성공으로 간주."""
        result = self.adapter.write_summary("test_key", "test_value")
        assert result is True

    def test_write_summary_with_ttl_returns_true(self):
        """TTL 포함 쓰기도 성공."""
        result = self.adapter.write_summary("test_key", "test_value", ttl=3600)
        assert result is True

    def test_read_summary_returns_none(self):
        """읽기는 항상 None 반환."""
        # 먼저 쓰기
        self.adapter.write_summary("test_key", "test_value")
        # 읽기 시 None
        result = self.adapter.read_summary("test_key")
        assert result is None

    def test_delete_summary_returns_true(self):
        """삭제는 항상 성공으로 간주."""
        result = self.adapter.delete_summary("test_key")
        assert result is True

    def test_read_many_returns_all_none(self):
        """다중 읽기도 모두 None."""
        keys = ["key1", "key2", "key3"]
        result = self.adapter.read_many(keys)
        assert result == {"key1": None, "key2": None, "key3": None}

    def test_increment_returns_zero(self):
        """증가 연산은 0 반환."""
        result = self.adapter.increment("counter_key")
        assert result == 0

    def test_increment_with_amount_returns_zero(self):
        """증가량 지정해도 0 반환."""
        result = self.adapter.increment("counter_key", amount=10)
        assert result == 0

    def test_decrement_returns_zero(self):
        """감소 연산은 0 반환."""
        result = self.adapter.decrement("counter_key")
        assert result == 0

    def test_decrement_with_amount_returns_zero(self):
        """감소량 지정해도 0 반환."""
        result = self.adapter.decrement("counter_key", amount=5)
        assert result == 0

    def test_is_protocol_compatible(self):
        """Protocol 호환성 확인."""
        assert isinstance(self.adapter, AirGapStorageAdapter)


# =============================================================================
# RedisAirGapAdapter Tests (with Mock)
# =============================================================================


class TestRedisAirGapAdapter:
    """RedisAirGapAdapter 테스트 (Mock Redis 사용)."""

    def setup_method(self):
        """테스트 전 Mock Redis 설정."""
        reset_airgap_adapter()
        self.mock_redis = MagicMock()
        self.mock_redis.ping.return_value = True

    def _create_adapter(self):
        """테스트용 어댑터 생성."""
        from baldur.adapters.airgap.redis_adapter import RedisAirGapAdapter

        return RedisAirGapAdapter(
            redis_client=self.mock_redis,
            prefix="test:airgap:",
            default_ttl=3600,
        )

    def test_is_enabled_when_redis_connected(self):
        """Redis 연결 시 활성화 상태."""
        adapter = self._create_adapter()
        assert adapter.is_enabled() is True

    def test_is_disabled_when_redis_disconnected(self):
        """Redis 연결 실패 시 비활성화."""
        self.mock_redis.ping.side_effect = Exception("Connection refused")
        adapter = self._create_adapter()
        assert adapter.is_enabled() is False

    def test_write_summary_calls_setex(self):
        """쓰기 시 SETEX 호출."""
        adapter = self._create_adapter()
        adapter.write_summary("test_key", "test_value")

        self.mock_redis.setex.assert_called_once_with(
            "test:airgap:test_key", 3600, "test_value"
        )

    def test_write_summary_with_custom_ttl(self):
        """커스텀 TTL로 쓰기."""
        adapter = self._create_adapter()
        adapter.write_summary("test_key", "test_value", ttl=7200)

        self.mock_redis.setex.assert_called_once_with(
            "test:airgap:test_key", 7200, "test_value"
        )

    def test_write_summary_serializes_dict(self):
        """딕셔너리 값 JSON 직렬화."""
        adapter = self._create_adapter()
        data = {"status": "active", "count": 5}
        adapter.write_summary("test_key", data)

        expected_json = json.dumps(data, separators=(",", ":"))
        self.mock_redis.setex.assert_called_once_with(
            "test:airgap:test_key", 3600, expected_json
        )

    def test_read_summary_returns_value(self):
        """읽기 시 값 반환."""
        self.mock_redis.get.return_value = b"test_value"
        adapter = self._create_adapter()

        result = adapter.read_summary("test_key")
        assert result == "test_value"
        self.mock_redis.get.assert_called_once_with("test:airgap:test_key")

    def test_read_summary_deserializes_json(self):
        """JSON 값 역직렬화."""
        data = {"status": "active", "count": 5}
        self.mock_redis.get.return_value = json.dumps(data).encode()
        adapter = self._create_adapter()

        result = adapter.read_summary("test_key")
        assert result == data

    def test_read_summary_returns_none_for_missing_key(self):
        """없는 키는 None 반환."""
        self.mock_redis.get.return_value = None
        adapter = self._create_adapter()

        result = adapter.read_summary("missing_key")
        assert result is None

    def test_delete_summary_calls_delete(self):
        """삭제 시 DELETE 호출."""
        adapter = self._create_adapter()
        adapter.delete_summary("test_key")

        self.mock_redis.delete.assert_called_once_with("test:airgap:test_key")

    def test_read_many_calls_mget(self):
        """다중 읽기 시 MGET 호출."""
        self.mock_redis.mget.return_value = [b"value1", b"value2", None]
        adapter = self._create_adapter()

        result = adapter.read_many(["key1", "key2", "key3"])

        self.mock_redis.mget.assert_called_once()
        assert result == {"key1": "value1", "key2": "value2", "key3": None}

    def test_increment_calls_incrby(self):
        """증가 시 INCRBY 호출."""
        self.mock_redis.incrby.return_value = 5
        adapter = self._create_adapter()

        result = adapter.increment("counter", amount=1)

        self.mock_redis.incrby.assert_called_once_with("test:airgap:counter", 1)
        assert result == 5

    def test_decrement_uses_lua_script(self):
        """감소 시 Lua 스크립트 사용."""
        self.mock_redis.eval.return_value = 3
        adapter = self._create_adapter()

        result = adapter.decrement("counter", amount=2)

        self.mock_redis.eval.assert_called_once()
        assert result == 3

    def test_health_check_returns_healthy_status(self):
        """상태 확인 시 healthy 반환."""
        self.mock_redis.info.return_value = {"used_memory_human": "10.5M"}
        adapter = self._create_adapter()

        health = adapter.health_check()

        assert health["status"] == "healthy"
        assert health["enabled"] is True
        assert health["prefix"] == "test:airgap:"

    def test_health_check_returns_unhealthy_on_error(self):
        """Redis 오류 시 unhealthy 반환."""
        self.mock_redis.ping.side_effect = Exception("Connection refused")
        adapter = self._create_adapter()

        health = adapter.health_check()

        assert health["status"] == "unhealthy"
        assert health["enabled"] is False

    def test_key_prefix_not_duplicated(self):
        """이미 접두사가 있는 키는 중복 추가 안 함."""
        adapter = self._create_adapter()
        adapter.write_summary("test:airgap:already_prefixed", "value")

        self.mock_redis.setex.assert_called_once_with(
            "test:airgap:already_prefixed", 3600, "value"
        )


# =============================================================================
# AirGapKeys Tests
# =============================================================================


class TestAirGapKeys:
    """AirGapKeys 헬퍼 테스트."""

    def test_dlq_pending_key(self):
        """DLQ pending 키 생성."""
        key = AirGapKeys.dlq_pending("payment")
        assert key == "sh:airgap:dlq:payment:pending"

    def test_dlq_status_key(self):
        """DLQ status 키 생성."""
        key = AirGapKeys.dlq_status("payment", "resolved")
        assert key == "sh:airgap:dlq:payment:resolved"

    def test_circuit_breaker_state_key(self):
        """Circuit breaker state 키 생성."""
        key = AirGapKeys.circuit_breaker_state("toss_payment")
        assert key == "sh:airgap:cb:toss_payment:state"

    def test_circuit_breaker_failure_count_key(self):
        """Circuit breaker failure count 키 생성."""
        key = AirGapKeys.circuit_breaker_failure_count("toss_payment")
        assert key == "sh:airgap:cb:toss_payment:failures"

    def test_retry_success_count_key(self):
        """Retry success count 키 생성."""
        key = AirGapKeys.retry_success_count("payment")
        assert key == "sh:airgap:retry:payment:success"

    def test_retry_failure_count_key(self):
        """Retry failure count 키 생성."""
        key = AirGapKeys.retry_failure_count("payment")
        assert key == "sh:airgap:retry:payment:failure"


# =============================================================================
# Factory Tests
# =============================================================================


class TestAirGapFactory:
    """Air-Gap Factory 테스트."""

    def setup_method(self):
        """테스트 전 어댑터 리셋."""
        reset_airgap_adapter()

    def teardown_method(self):
        """테스트 후 어댑터 리셋."""
        reset_airgap_adapter()

    def test_default_returns_null_adapter(self):
        """기본값은 NullAirGapAdapter."""
        with patch.dict("os.environ", {}, clear=True):
            reset_airgap_adapter()
            adapter = get_airgap_adapter()
            assert isinstance(adapter, NullAirGapAdapter)

    def test_disabled_returns_null_adapter(self):
        """비활성화 설정 시 NullAirGapAdapter."""
        with patch.dict("os.environ", {"BALDUR_AIRGAP_ENABLED": "false"}):
            reset_airgap_adapter()
            adapter = get_airgap_adapter()
            assert isinstance(adapter, NullAirGapAdapter)

    def test_singleton_returns_same_instance(self):
        """싱글톤 패턴 확인."""
        adapter1 = get_airgap_adapter()
        adapter2 = get_airgap_adapter()
        assert adapter1 is adapter2

    def test_configure_adapter_sets_custom_adapter(self):
        """커스텀 어댑터 설정."""
        custom_adapter = NullAirGapAdapter()
        configure_airgap_adapter(custom_adapter)

        adapter = get_airgap_adapter()
        assert adapter is custom_adapter

    def test_reset_clears_singleton(self):
        """리셋 시 싱글톤 초기화."""
        adapter1 = get_airgap_adapter()
        reset_airgap_adapter()

        # 리셋 후 새 인스턴스
        adapter2 = get_airgap_adapter()
        assert adapter1 is not adapter2

    @patch("baldur.adapters.airgap.factory._create_redis_adapter")
    def test_enabled_creates_redis_adapter(self, mock_create):
        """활성화 시 Redis 어댑터 생성 시도."""
        mock_adapter = MagicMock()
        mock_create.return_value = mock_adapter

        with patch.dict("os.environ", {"BALDUR_AIRGAP_ENABLED": "true"}):
            reset_airgap_adapter()
            adapter = get_airgap_adapter()

        mock_create.assert_called_once()
        assert adapter is mock_adapter

    @patch("baldur.adapters.airgap.factory._create_redis_adapter")
    def test_enabled_fallback_to_null_on_failure(self, mock_create):
        """Redis 생성 실패 시 Null로 폴백."""
        mock_create.return_value = None

        with patch.dict("os.environ", {"BALDUR_AIRGAP_ENABLED": "true"}):
            reset_airgap_adapter()
            adapter = get_airgap_adapter()

        assert isinstance(adapter, NullAirGapAdapter)


# =============================================================================
# Integration-like Tests
# =============================================================================


class TestAirGapIntegration:
    """Air-Gap 통합 시나리오 테스트."""

    def setup_method(self):
        """테스트 전 어댑터 리셋."""
        reset_airgap_adapter()

    def test_null_adapter_passthrough_scenario(self):
        """NullAdapter는 기존 로직에 영향 없음."""
        adapter = NullAirGapAdapter()

        # 비즈니스 레이어가 쓰기 시도 (무시됨)
        adapter.write_summary(AirGapKeys.dlq_pending("payment"), 10)

        # Baldur 엔진이 읽기 (None → 기존 로직 사용해야 함)
        result = adapter.read_summary(AirGapKeys.dlq_pending("payment"))
        assert result is None

        # is_enabled() 체크로 분기 가능
        if not adapter.is_enabled():
            # 기존 로직 (예: DB 직접 조회) 사용
            pass

    def test_business_layer_write_engine_read_scenario(self):
        """비즈니스 레이어 쓰기 → 엔진 읽기 시나리오."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.get.return_value = b"5"

        from baldur.adapters.airgap.redis_adapter import RedisAirGapAdapter

        adapter = RedisAirGapAdapter(mock_redis)

        # 비즈니스 레이어: DLQ 적재 시 요약 기록
        adapter.write_summary(AirGapKeys.dlq_pending("payment"), 5)

        # Baldur 엔진: Air-Gap에서 읽기
        # JSON 역직렬화로 숫자 "5"는 int 5로 변환됨
        count = adapter.read_summary(AirGapKeys.dlq_pending("payment"))
        assert count == 5

    def test_counter_increment_decrement_scenario(self):
        """카운터 증감 시나리오."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.incrby.return_value = 1
        mock_redis.eval.return_value = 0

        from baldur.adapters.airgap.redis_adapter import RedisAirGapAdapter

        adapter = RedisAirGapAdapter(mock_redis)

        # DLQ 적재 시 증가
        new_count = adapter.increment(AirGapKeys.dlq_pending("payment"))
        assert new_count == 1

        # DLQ 처리 완료 시 감소
        new_count = adapter.decrement(AirGapKeys.dlq_pending("payment"))
        assert new_count == 0


# =============================================================================
# Edge Case Tests
# =============================================================================


class TestAirGapEdgeCases:
    """엣지 케이스 테스트."""

    def test_null_adapter_empty_read_many(self):
        """빈 키 리스트로 read_many 호출."""
        adapter = NullAirGapAdapter()
        result = adapter.read_many([])
        assert result == {}

    def test_redis_adapter_handles_connection_error_gracefully(self):
        """Redis 연결 오류 시 graceful 처리."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.get.side_effect = Exception("Connection lost")

        from baldur.adapters.airgap.redis_adapter import RedisAirGapAdapter

        adapter = RedisAirGapAdapter(mock_redis)

        # 오류 발생해도 None 반환 (예외 전파 안 함)
        result = adapter.read_summary("test_key")
        assert result is None

    def test_redis_adapter_handles_write_error_gracefully(self):
        """Redis 쓰기 오류 시 False 반환."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.setex.side_effect = Exception("Connection lost")

        from baldur.adapters.airgap.redis_adapter import RedisAirGapAdapter

        adapter = RedisAirGapAdapter(mock_redis)

        result = adapter.write_summary("test_key", "value")
        assert result is False

    def test_redis_adapter_numeric_values(self):
        """숫자 값 직렬화/역직렬화."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.get.return_value = b"42"

        from baldur.adapters.airgap.redis_adapter import RedisAirGapAdapter

        adapter = RedisAirGapAdapter(mock_redis)

        result = adapter.read_summary("counter")
        assert result == 42  # JSON 파싱으로 int 반환
