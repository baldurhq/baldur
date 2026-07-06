"""
event_name_validator 프로세서 (312 Q5, 314 Audit) 단위 테스트.

검증 대상:
- event_name_validator(): 이벤트명 컨벤션 검증 프로세서
- _EVENT_NAME_PATTERN: 이벤트명 정규식 패턴 (314에서 digits 허용으로 변경)
- strict vs production 모드 분기 (314에서 LoggingSettings 통합)

기법 분류:
- 계약 검증: _EVENT_NAME_PATTERN 매칭 규칙
- 동작 검증: strict mode ValueError, production mode passthrough
- 부수효과: Prometheus counter 증가
- 엣지 케이스: 빈 문자열, None, 비문자열 이벤트명
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.observability.log_processors import (
    _EVENT_NAME_PATTERN,
    event_name_validator,
    reset_strict_validation_cache,
)

# =============================================================================
# 계약 검증 — 이벤트명 패턴
# =============================================================================


class TestEventNamePatternContract:
    """_EVENT_NAME_PATTERN이 설계 컨벤션대로 동작하는지 검증."""

    @pytest.mark.parametrize(
        "valid_name",
        [
            "registry.cache_registered",
            "circuit_breaker.state_changed",
            "dlq.entry_created",
            "audit.entry_logged",
            "retry.attempt_failed",
            "service.fallback_adapter",
            "k8s_ingress_traffic_router.kubernetes_package_installed",
            "s3_worm_backend.cannot_place_legal_hold",
            "watchdog.redis_recovery_stage1",
        ],
    )
    def test_valid_event_names_match_pattern(self, valid_name):
        """올바른 이벤트명은 패턴에 매칭되어야 한다."""
        assert _EVENT_NAME_PATTERN.match(valid_name) is not None

    @pytest.mark.parametrize(
        "invalid_name",
        [
            "CellRegistry.BulkheadsRegistered",
            "cell_registry",
            "registry.cache.registered",
            "Registry.cache_registered",
            "registry cache_registered",
            "123.abc",
            "_foo.bar",
            "foo._bar",
            "",
        ],
    )
    def test_invalid_event_names_do_not_match_pattern(self, invalid_name):
        """규칙 위반 이벤트명은 패턴에 매칭되지 않아야 한다."""
        assert _EVENT_NAME_PATTERN.match(invalid_name) is None


# =============================================================================
# 동작 검증 — strict mode (DEV/TEST)
# =============================================================================


class TestEventNameValidatorStrictBehavior:
    """strict_log_validation=True 시 위반에 ValueError를 발생시킨다."""

    @pytest.fixture(autouse=True)
    def _reset_state(self):
        """Prometheus counter와 strict validation 캐시를 리셋한다."""
        import baldur.observability.log_processors as mod

        self._unregister_violation_counter(mod)
        reset_strict_validation_cache()
        yield
        self._unregister_violation_counter(mod)
        reset_strict_validation_cache()

    @staticmethod
    def _unregister_violation_counter(mod):
        """Safely unregister prometheus counter before resetting flag."""
        if mod._violation_counter is not None:
            try:
                from prometheus_client import REGISTRY

                REGISTRY.unregister(mod._violation_counter)
            except Exception:
                pass
        mod._violation_counter_initialized = False
        mod._violation_counter = None

    def test_valid_event_passes_in_strict_mode(self):
        """올바른 이벤트명은 strict 모드에서도 통과해야 한다."""
        with patch(
            "baldur.settings.logging_settings.get_logging_settings"
        ) as mock_settings:
            mock_settings.return_value.strict_log_validation = True
            result = event_name_validator(
                None, "info", {"event": "registry.cache_registered"}
            )
        assert result["event"] == "registry.cache_registered"

    def test_invalid_event_raises_value_error_in_strict_mode(self):
        """위반 이벤트명은 strict 모드에서 ValueError를 발생시켜야 한다."""
        with patch(
            "baldur.settings.logging_settings.get_logging_settings"
        ) as mock_settings:
            mock_settings.return_value.strict_log_validation = True
            with pytest.raises(ValueError, match="violates naming convention"):
                event_name_validator(None, "info", {"event": "BadEventName"})

    def test_strict_mode_caches_result(self):
        """strict_log_validation 값은 캐싱되어 반복 조회하지 않아야 한다."""
        with patch(
            "baldur.settings.logging_settings.get_logging_settings"
        ) as mock_settings:
            mock_settings.return_value.strict_log_validation = True
            with pytest.raises(ValueError):
                event_name_validator(None, "info", {"event": "invalid"})

        with pytest.raises(ValueError):
            event_name_validator(None, "info", {"event": "also_invalid"})


# =============================================================================
# 동작 검증 — production mode (기본)
# =============================================================================


class TestEventNameValidatorProductionBehavior:
    """Production 모드(기본값)에서는 위반해도 로그를 통과시킨다."""

    @pytest.fixture(autouse=True)
    def _reset_state(self):
        """Prometheus counter와 strict validation 캐시를 리셋한다."""
        import baldur.observability.log_processors as mod

        self._unregister_violation_counter(mod)
        reset_strict_validation_cache()
        yield
        self._unregister_violation_counter(mod)
        reset_strict_validation_cache()

    @staticmethod
    def _unregister_violation_counter(mod):
        """Safely unregister prometheus counter before resetting flag."""
        if mod._violation_counter is not None:
            try:
                from prometheus_client import REGISTRY

                REGISTRY.unregister(mod._violation_counter)
            except Exception:
                pass
        mod._violation_counter_initialized = False
        mod._violation_counter = None

    def test_invalid_event_passes_in_production_mode(self):
        """위반 이벤트명이 production 모드에서는 ValueError 없이 통과한다."""
        with patch(
            "baldur.settings.logging_settings.get_logging_settings"
        ) as mock_settings:
            mock_settings.return_value.strict_log_validation = False
            result = event_name_validator(None, "info", {"event": "BadEvent"})
        assert result["event"] == "BadEvent"

    def test_prometheus_counter_incremented_on_violation(self):
        """Production 모드에서 위반 시 Prometheus counter가 증가해야 한다."""
        with patch(
            "baldur.settings.logging_settings.get_logging_settings"
        ) as mock_settings:
            mock_settings.return_value.strict_log_validation = False
            mock_counter = MagicMock()
            with patch(
                "baldur.observability.log_processors._get_violation_counter",
                return_value=mock_counter,
            ):
                event_name_validator(None, "info", {"event": "BadEvent"})

        mock_counter.labels.assert_called_once_with(event_name="BadEvent")
        mock_counter.labels.return_value.inc.assert_called_once()

    def test_no_error_when_prometheus_unavailable(self):
        """Prometheus가 없어도 에러 없이 통과해야 한다."""
        with patch(
            "baldur.settings.logging_settings.get_logging_settings"
        ) as mock_settings:
            mock_settings.return_value.strict_log_validation = False
            with patch(
                "baldur.observability.log_processors._get_violation_counter",
                return_value=None,
            ):
                result = event_name_validator(None, "info", {"event": "BadEvent"})
                assert isinstance(result, dict)


# =============================================================================
# 동작 검증 — 엣지 케이스
# =============================================================================


class TestEventNameValidatorEdgeCaseBehavior:
    """빈 문자열, None, 비문자열 이벤트명 처리 검증."""

    def test_empty_event_name_passes_without_validation(self):
        """빈 이벤트명은 검증 없이 통과해야 한다."""
        result = event_name_validator(None, "info", {"event": ""})
        assert result["event"] == ""

    def test_missing_event_key_passes_without_validation(self):
        """event 키가 없는 event_dict는 검증 없이 통과해야 한다."""
        result = event_name_validator(None, "info", {"logger": "test"})
        assert isinstance(result, dict)

    def test_non_string_event_passes_without_validation(self):
        """비문자열 이벤트(int 등)는 검증 없이 통과해야 한다."""
        result = event_name_validator(None, "info", {"event": 12345})
        assert result["event"] == 12345

    def test_none_event_passes_without_validation(self):
        """None 이벤트명은 검증 없이 통과해야 한다."""
        result = event_name_validator(None, "info", {"event": None})
        assert result["event"] is None
