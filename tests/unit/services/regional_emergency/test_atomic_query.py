"""
AtomicStateQuery 단위 테스트.

Lua 스크립트 기반 원자적 Global+Regional 상태 조회 테스트.

테스트 대상:
- 단일 Redis 호출 확인
- Global STRICT 오버라이드 동작
- Admin Override 동작
- Regional STRICT 동작
- 폴백 처리

Reference:
    docs/baldur/middleware_system/73_NAMESPACE_AWARE_EMERGENCY.md
"""

from unittest.mock import MagicMock

import pytest

from baldur.services.regional_emergency.atomic_query import (
    ATOMIC_STATE_QUERY_SCRIPT,
    AtomicStateQuery,
    reset_atomic_state_query,
)


class TestAtomicStateQuery:
    """AtomicStateQuery 단위 테스트."""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis 클라이언트."""
        return MagicMock()

    @pytest.fixture
    def query(self, mock_redis):
        """AtomicStateQuery 인스턴스."""
        return AtomicStateQuery(mock_redis)

    def teardown_method(self):
        """테스트 후 싱글톤 리셋."""
        reset_atomic_state_query()

    # =========================================================================
    # 기본 동작 테스트
    # =========================================================================

    def test_single_redis_call(self, mock_redis, query):
        """단일 Redis 호출 확인 - eval 한 번만 호출."""
        mock_redis.eval.return_value = [
            b'{"namespace":"tokyo","scope":"regional","governance_mode":"NORMAL","is_active":false}',
            b"REGIONAL_DEFAULT",
            b"Both states NORMAL, using regional",
        ]

        query.query_effective_state("tokyo")

        # eval 한 번만 호출되어야 함
        assert mock_redis.eval.call_count == 1

    def test_correct_keys_passed(self, mock_redis, query):
        """올바른 Redis 키가 전달되는지 확인."""
        mock_redis.eval.return_value = [
            b'{"namespace":"seoul","governance_mode":"NORMAL"}',
            b"REGIONAL_DEFAULT",
            b"Both states NORMAL",
        ]

        query.query_effective_state("seoul")

        # eval 호출 인자 확인
        call_args = mock_redis.eval.call_args
        assert call_args[0][0] == ATOMIC_STATE_QUERY_SCRIPT
        assert call_args[0][1] == 2  # number of keys
        assert call_args[0][2] == "baldur:governance:emergency_state"  # global key
        assert (
            call_args[0][3] == "baldur:seoul:governance:emergency_state"
        )  # regional key

    # =========================================================================
    # Global Override 테스트
    # =========================================================================

    def test_global_override_regional(self, mock_redis, query):
        """Global STRICT가 Regional NORMAL을 오버라이드."""
        mock_redis.eval.return_value = [
            b'{"namespace":"global","scope":"global","governance_mode":"STRICT","is_active":true,"emergency_level":3}',
            b"GLOBAL_OVERRIDE",
            b"Global STRICT overrides regional seoul",
        ]

        state, decision_type, reason = query.query_effective_state("seoul")

        assert state["namespace"] == "global"
        assert state["governance_mode"] == "STRICT"
        assert state["is_active"] is True
        assert decision_type == "GLOBAL_OVERRIDE"
        assert "overrides regional seoul" in reason

    def test_both_strict_uses_global(self, mock_redis, query):
        """Global과 Regional 모두 STRICT면 Global 사용."""
        mock_redis.eval.return_value = [
            b'{"namespace":"global","scope":"global","governance_mode":"STRICT","is_active":true}',
            b"GLOBAL_OVERRIDE",
            b"Both Global and Regional STRICT, using Global state",
        ]

        state, decision_type, reason = query.query_effective_state("tokyo")

        assert state["namespace"] == "global"
        assert decision_type == "GLOBAL_OVERRIDE"
        assert "Both Global and Regional STRICT" in reason

    # =========================================================================
    # Admin Override 테스트
    # =========================================================================

    def test_admin_override_ignores_global(self, mock_redis, query):
        """ADMIN_OVERRIDE 우선순위는 Global을 무시하고 Regional 사용."""
        mock_redis.eval.return_value = [
            b'{"namespace":"seoul","scope":"regional","governance_mode":"NORMAL","is_active":false}',
            b"ADMIN_OVERRIDE",
            b"Admin override active, using regional state",
        ]

        state, decision_type, reason = query.query_effective_state(
            "seoul", precedence="ADMIN_OVERRIDE"
        )

        assert state["namespace"] == "seoul"
        assert decision_type == "ADMIN_OVERRIDE"
        assert "Admin override" in reason

    def test_kill_switch_uses_regional(self, mock_redis, query):
        """KILL_SWITCH 우선순위도 Regional 사용."""
        mock_redis.eval.return_value = [
            b'{"namespace":"oregon","scope":"regional","governance_mode":"NORMAL"}',
            b"ADMIN_OVERRIDE",
            b"Admin override active, using regional state",
        ]

        state, decision_type, _ = query.query_effective_state(
            "oregon", precedence="KILL_SWITCH"
        )

        assert state["namespace"] == "oregon"
        assert decision_type == "ADMIN_OVERRIDE"

    # =========================================================================
    # Regional STRICT 테스트
    # =========================================================================

    def test_regional_strict_when_global_normal(self, mock_redis, query):
        """Global NORMAL이고 Regional STRICT면 Regional 사용."""
        mock_redis.eval.return_value = [
            b'{"namespace":"tokyo","scope":"regional","governance_mode":"STRICT","is_active":true,"emergency_level":2}',
            b"REGIONAL_STRICT",
            b"Regional STRICT active",
        ]

        state, decision_type, reason = query.query_effective_state("tokyo")

        assert state["namespace"] == "tokyo"
        assert state["governance_mode"] == "STRICT"
        assert decision_type == "REGIONAL_STRICT"
        assert "Regional STRICT" in reason

    # =========================================================================
    # Regional Default 테스트
    # =========================================================================

    def test_both_normal_uses_regional(self, mock_redis, query):
        """둘 다 NORMAL이면 Regional 반환."""
        mock_redis.eval.return_value = [
            b'{"namespace":"busan","scope":"regional","governance_mode":"NORMAL","is_active":false}',
            b"REGIONAL_DEFAULT",
            b"Both states NORMAL, using regional",
        ]

        state, decision_type, reason = query.query_effective_state("busan")

        assert state["namespace"] == "busan"
        assert state["governance_mode"] == "NORMAL"
        assert decision_type == "REGIONAL_DEFAULT"
        assert "Both states NORMAL" in reason

    # =========================================================================
    # 폴백 테스트
    # =========================================================================

    def test_fallback_on_redis_error(self, mock_redis, query):
        """Redis 오류 시 안전한 기본값 반환."""
        mock_redis.eval.side_effect = Exception("Connection refused")

        state, decision_type, reason = query.query_effective_state("osaka")

        assert state["namespace"] == "osaka"
        assert state["governance_mode"] == "NORMAL"
        assert state["is_active"] is False
        assert decision_type == "FALLBACK"
        assert "Connection refused" in reason

    def test_fallback_returns_safe_default(self, mock_redis, query):
        """폴백 시 NORMAL 상태 반환."""
        mock_redis.eval.side_effect = TimeoutError("Timeout")

        state, _, _ = query.query_effective_state("nagoya")

        assert state["governance_mode"] == "NORMAL"
        assert state["is_active"] is False
        assert state["emergency_level"] == "normal"

    # =========================================================================
    # Precedence 레벨 테스트
    # =========================================================================

    def test_precedence_auto_is_default(self, mock_redis, query):
        """precedence 미지정 시 AUTO (0) 사용."""
        mock_redis.eval.return_value = [b"{}", b"REGIONAL_DEFAULT", b"test"]

        query.query_effective_state("test")

        call_args = mock_redis.eval.call_args
        assert call_args[0][4] == "0"  # AUTO = 0

    def test_precedence_manual_value(self, mock_redis, query):
        """MANUAL precedence 값 확인."""
        mock_redis.eval.return_value = [b"{}", b"REGIONAL_DEFAULT", b"test"]

        query.query_effective_state("test", precedence="MANUAL")

        call_args = mock_redis.eval.call_args
        assert call_args[0][4] == "1"  # MANUAL = 1

    def test_precedence_admin_override_value(self, mock_redis, query):
        """ADMIN_OVERRIDE precedence 값 확인."""
        mock_redis.eval.return_value = [b"{}", b"ADMIN_OVERRIDE", b"test"]

        query.query_effective_state("test", precedence="ADMIN_OVERRIDE")

        call_args = mock_redis.eval.call_args
        assert call_args[0][4] == "2"  # ADMIN_OVERRIDE = 2

    def test_precedence_kill_switch_value(self, mock_redis, query):
        """KILL_SWITCH precedence 값 확인."""
        mock_redis.eval.return_value = [b"{}", b"ADMIN_OVERRIDE", b"test"]

        query.query_effective_state("test", precedence="KILL_SWITCH")

        call_args = mock_redis.eval.call_args
        assert call_args[0][4] == "3"  # KILL_SWITCH = 3

    # =========================================================================
    # 스크립트 사전 로드 테스트
    # =========================================================================

    def test_preload_script(self, mock_redis, query):
        """스크립트 사전 로드."""
        mock_redis.script_load.return_value = "abc123sha256"

        sha = query.preload_script()

        assert sha == "abc123sha256"
        mock_redis.script_load.assert_called_once_with(ATOMIC_STATE_QUERY_SCRIPT)

    def test_preload_script_caches_sha(self, mock_redis, query):
        """스크립트 SHA 캐싱."""
        mock_redis.script_load.return_value = "cached_sha"

        sha1 = query.preload_script()
        sha2 = query.preload_script()

        assert sha1 == sha2
        # 한 번만 호출
        assert mock_redis.script_load.call_count == 1

    def test_query_with_sha(self, mock_redis, query):
        """EVALSHA로 쿼리."""
        mock_redis.script_load.return_value = "test_sha"
        mock_redis.evalsha.return_value = [
            b'{"namespace":"test"}',
            b"REGIONAL_DEFAULT",
            b"test",
        ]

        state, _, _ = query.query_with_sha("test")

        mock_redis.evalsha.assert_called_once()
        call_args = mock_redis.evalsha.call_args
        assert call_args[0][0] == "test_sha"

    def test_query_with_sha_fallback_to_eval(self, mock_redis, query):
        """EVALSHA 실패 시 EVAL로 폴백."""
        mock_redis.script_load.return_value = "test_sha"
        mock_redis.evalsha.side_effect = Exception("NOSCRIPT")
        mock_redis.eval.return_value = [
            b'{"namespace":"fallback"}',
            b"REGIONAL_DEFAULT",
            b"test",
        ]

        state, _, _ = query.query_with_sha("fallback")

        # evalsha 실패 후 eval 호출
        assert mock_redis.eval.call_count == 1

    # =========================================================================
    # 키 생성 테스트
    # =========================================================================

    def test_get_global_key(self, query):
        """Global 키 생성."""
        assert query._get_global_key() == "baldur:governance:emergency_state"

    def test_get_regional_key(self, query):
        """Regional 키 생성."""
        assert (
            query._get_regional_key("seoul")
            == "baldur:seoul:governance:emergency_state"
        )
        assert (
            query._get_regional_key("tokyo")
            == "baldur:tokyo:governance:emergency_state"
        )

    def test_custom_key_prefix(self, mock_redis):
        """커스텀 키 프리픽스."""
        query = AtomicStateQuery(mock_redis, key_prefix="myapp")

        assert query._get_global_key() == "myapp:governance:emergency_state"
        assert (
            query._get_regional_key("test") == "myapp:test:governance:emergency_state"
        )


class TestAtomicStateQueryBytes:
    """AtomicStateQuery 바이트 처리 테스트."""

    @pytest.fixture
    def mock_redis(self):
        return MagicMock()

    @pytest.fixture
    def query(self, mock_redis):
        return AtomicStateQuery(mock_redis)

    def test_handles_bytes_response(self, mock_redis, query):
        """bytes 응답 처리."""
        mock_redis.eval.return_value = [
            b'{"namespace":"test","governance_mode":"STRICT"}',
            b"GLOBAL_OVERRIDE",
            b"Test reason",
        ]

        state, decision_type, reason = query.query_effective_state("test")

        assert isinstance(state, dict)
        assert isinstance(decision_type, str)
        assert isinstance(reason, str)
        assert state["governance_mode"] == "STRICT"

    def test_handles_string_response(self, mock_redis, query):
        """str 응답 처리."""
        mock_redis.eval.return_value = [
            '{"namespace":"test","governance_mode":"NORMAL"}',
            "REGIONAL_DEFAULT",
            "Test reason",
        ]

        state, decision_type, reason = query.query_effective_state("test")

        assert state["governance_mode"] == "NORMAL"
        assert decision_type == "REGIONAL_DEFAULT"
