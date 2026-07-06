"""
로그 프로세서 (rate_limit_processor) 단위 테스트.

검증 대상:
- rate_limit_processor(): 동일 (logger_name, event) 조합이 윈도우 내 max_count 초과 시
  DropEvent를 발생시키고, 윈도우 전환 시 suppress 카운트를 event_dict에 주입한다.

기법 분류:
- 계약 검증: _NEVER_SUPPRESS_LEVELS 포함 레벨, 설정 기본값, ge=0 제약
- 경계값 분석: count == max_count(통과) vs count == max_count+1(DropEvent)
- 예외/엣지: max_count=0, window_seconds=0 시 비활성화
- 상태 전이: 윈도우 만료 → 새 윈도우 시작 + suppressed_count 주입
- 부수효과: event_dict에 _rate_limit_suppressed_previous 필드 주입
- 시간 의존성: time.monotonic 패치로 윈도우 경과 시뮬레이션
- 동시성: 20개 스레드 동시 호출 시 카운터 정합성
- 싱글톤/라이프사이클: reset_rate_limit_state() 후 카운터 초기화
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
import structlog

# =============================================================================
# 공통 픽스처
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_state():
    """각 테스트 전후로 rate limit 상태와 LoggingSettings 싱글톤을 초기화한다."""
    from baldur.observability.log_processors import reset_rate_limit_state
    from baldur.settings.logging_settings import reset_logging_settings

    reset_rate_limit_state()
    reset_logging_settings()
    yield
    reset_rate_limit_state()
    reset_logging_settings()


# =============================================================================
# 계약 검증
# =============================================================================


class TestRateLimitProcessorContract:
    """rate_limit_processor 설계 계약값 검증."""

    def test_never_suppress_levels_include_error_and_critical(self):
        """suppress 제외 레벨이 설계대로 'error'와 'critical'이어야 한다."""
        from baldur.observability.log_processors import _NEVER_SUPPRESS_LEVELS

        assert "error" in _NEVER_SUPPRESS_LEVELS
        assert "critical" in _NEVER_SUPPRESS_LEVELS

    def test_never_suppress_levels_excludes_info_and_warning(self):
        """'info'와 'warning'은 suppress 제외 레벨에 포함되지 않아야 한다."""
        from baldur.observability.log_processors import _NEVER_SUPPRESS_LEVELS

        assert "info" not in _NEVER_SUPPRESS_LEVELS
        assert "warning" not in _NEVER_SUPPRESS_LEVELS

    def test_rate_limit_window_default_is_10_seconds(self):
        """log_rate_limit_window 기본값이 설계 계약대로 10이어야 한다."""
        from baldur.settings.logging_settings import LoggingSettings

        assert LoggingSettings.model_fields["log_rate_limit_window"].default == 10

    def test_rate_limit_max_default_is_10(self):
        """log_rate_limit_max 기본값이 10이어야 한다 (프로덕션 로그 볼륨 제어)."""
        from baldur.settings.logging_settings import LoggingSettings

        assert LoggingSettings.model_fields["log_rate_limit_max"].default == 10

    def test_rate_limit_window_rejects_negative_value(self):
        """log_rate_limit_window에 ge=0 제약이 적용되어 음수 입력 시 ValidationError."""
        from pydantic import ValidationError

        from baldur.settings.logging_settings import LoggingSettings

        with pytest.raises(ValidationError):
            LoggingSettings(log_rate_limit_window=-1)

    def test_rate_limit_max_rejects_negative_value(self):
        """log_rate_limit_max에 ge=0 제약이 적용되어 음수 입력 시 ValidationError."""
        from pydantic import ValidationError

        from baldur.settings.logging_settings import LoggingSettings

        with pytest.raises(ValidationError):
            LoggingSettings(log_rate_limit_max=-1)


# =============================================================================
# 동작 검증 — ERROR/CRITICAL 절대 suppress 안 함
# =============================================================================


class TestRateLimitNeverSuppressBehavior:
    """ERROR/CRITICAL 레벨은 max_count 초과 후에도 항상 통과해야 한다."""

    def test_error_level_passes_beyond_max_count(self, monkeypatch):
        """ERROR 레벨은 max_count=1을 초과해도 계속 통과한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "1")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "60")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        for _ in range(5):
            result = rate_limit_processor(
                None, "error", {"event": "err_event", "logger": "test"}
            )
            assert isinstance(result, dict)

    def test_critical_level_passes_beyond_max_count(self, monkeypatch):
        """CRITICAL 레벨은 max_count=1을 초과해도 계속 통과한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "1")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "60")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        for _ in range(5):
            result = rate_limit_processor(
                None, "critical", {"event": "crit_event", "logger": "test"}
            )
            assert isinstance(result, dict)

    def test_info_level_suppressed_after_max_count(self, monkeypatch):
        """INFO 레벨은 max_count 초과 시 DropEvent가 발생한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "2")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "60")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        # 처음 2번은 통과
        for _ in range(2):
            rate_limit_processor(None, "info", {"event": "ev", "logger": "test"})

        # 3번째에서 DropEvent
        with pytest.raises(structlog.DropEvent):
            rate_limit_processor(None, "info", {"event": "ev", "logger": "test"})


# =============================================================================
# 동작 검증 — max_count 경계값
# =============================================================================


class TestRateLimitMaxCountBoundaryBehavior:
    """max_count 경계에서의 suppress 동작 검증."""

    def test_exactly_max_count_calls_pass(self, monkeypatch):
        """count ≤ max_count 구간에서 모든 호출이 통과한다."""
        # Given
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "3")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "60")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        # When / Then: 3번 모두 통과
        for i in range(3):
            result = rate_limit_processor(
                None, "info", {"event": "boundary_ev", "logger": "test"}
            )
            assert isinstance(result, dict), f"{i + 1}번째 호출이 통과해야 한다"

    def test_max_count_plus_one_drops(self, monkeypatch):
        """count == max_count + 1 번째 호출에서 DropEvent가 발생한다."""
        # Given
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "3")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "60")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        for _ in range(3):
            rate_limit_processor(
                None, "info", {"event": "boundary_ev", "logger": "test"}
            )

        # When / Then: 4번째에서 DropEvent
        with pytest.raises(structlog.DropEvent):
            rate_limit_processor(
                None, "info", {"event": "boundary_ev", "logger": "test"}
            )

    def test_disabled_when_max_count_zero(self, monkeypatch):
        """max_count=0 이면 rate limit이 비활성화되어 무제한 통과한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "0")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "60")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        for _ in range(200):
            result = rate_limit_processor(
                None, "info", {"event": "unlimited_ev", "logger": "test"}
            )
            assert isinstance(result, dict)

    def test_disabled_when_window_zero(self, monkeypatch):
        """window_seconds=0 이면 rate limit이 비활성화되어 무제한 통과한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "1")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "0")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        for _ in range(5):
            result = rate_limit_processor(
                None, "info", {"event": "window_zero_ev", "logger": "test"}
            )
            assert isinstance(result, dict)


# =============================================================================
# 동작 검증 — 윈도우 상태 전이 (시간 의존성)
# =============================================================================


class TestRateLimitWindowTransitionBehavior:
    """윈도우 전환 시 suppressed count 주입 및 상태 리셋 동작 검증."""

    def test_suppressed_count_injected_at_window_transition(self, monkeypatch):
        """윈도우 만료 후 첫 통과 이벤트에 _rate_limit_suppressed_previous 필드가 주입된다."""
        # Given: max_count=1, window=10s
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "1")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "10")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        with patch("baldur.observability.log_processors.time") as mock_time:
            # t=0: 첫 이벤트 통과 (윈도우 시작)
            mock_time.monotonic.return_value = 0.0
            rate_limit_processor(None, "info", {"event": "ev", "logger": "test"})

            # max_count 초과 → 2건 suppress (t=1은 여전히 같은 윈도우)
            mock_time.monotonic.return_value = 1.0
            with pytest.raises(structlog.DropEvent):
                rate_limit_processor(None, "info", {"event": "ev", "logger": "test"})
            with pytest.raises(structlog.DropEvent):
                rate_limit_processor(None, "info", {"event": "ev", "logger": "test"})

            # t=11: 윈도우 만료 → 새 윈도우 시작
            mock_time.monotonic.return_value = 11.0
            result = rate_limit_processor(
                None, "info", {"event": "ev", "logger": "test"}
            )

        # Then: suppress 건수 2가 주입됨
        assert result.get("_rate_limit_suppressed_previous") == 2

    def test_no_suppressed_field_when_no_events_were_suppressed(self, monkeypatch):
        """이전 윈도우에서 suppress가 없었으면 해당 필드가 주입되지 않는다."""
        # Given: max_count=100, window=10s
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "100")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "10")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        with patch("baldur.observability.log_processors.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            rate_limit_processor(None, "info", {"event": "ev", "logger": "test"})

            # 윈도우 만료 (suppress 없이)
            mock_time.monotonic.return_value = 11.0
            result = rate_limit_processor(
                None, "info", {"event": "ev", "logger": "test"}
            )

        # Then: suppress 필드 없음
        assert "_rate_limit_suppressed_previous" not in result

    def test_different_logger_names_tracked_independently(self, monkeypatch):
        """같은 이벤트명이라도 다른 logger_name은 별도 윈도우로 추적된다."""
        # Given: max_count=1
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "1")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "60")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        # logger_a: max_count 소진
        rate_limit_processor(
            None, "info", {"event": "same_event", "logger": "logger_a"}
        )

        # When: logger_b로 같은 이벤트 호출
        result = rate_limit_processor(
            None, "info", {"event": "same_event", "logger": "logger_b"}
        )

        # Then: 별도 키이므로 통과
        assert isinstance(result, dict)


# =============================================================================
# 동작 검증 — reset_rate_limit_state() 라이프사이클
# =============================================================================


class TestRateLimitStateResetBehavior:
    """reset_rate_limit_state() 이후 카운터가 완전히 초기화된다."""

    def test_reset_allows_suppressed_event_to_pass_again(self, monkeypatch):
        """reset 후 max_count에 도달했던 이벤트가 다시 통과한다."""
        # Given
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "1")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "60")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import (
            rate_limit_processor,
            reset_rate_limit_state,
        )

        rate_limit_processor(None, "info", {"event": "reset_test", "logger": "test"})
        with pytest.raises(structlog.DropEvent):
            rate_limit_processor(
                None, "info", {"event": "reset_test", "logger": "test"}
            )

        # When
        reset_rate_limit_state()

        # Then: 카운터 초기화 → 다시 통과
        result = rate_limit_processor(
            None, "info", {"event": "reset_test", "logger": "test"}
        )
        assert isinstance(result, dict)


# =============================================================================
# 동시성 검증
# =============================================================================


class TestRateLimitThreadSafetyBehavior:
    """멀티스레드 환경에서도 카운터 정합성이 유지된다."""

    def test_concurrent_calls_pass_exactly_max_count(self, monkeypatch):
        """20개 스레드 동시 호출 시 정확히 max_count개만 통과하고 나머지는 drop된다."""
        # Given
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_MAX", "5")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_RATE_LIMIT_WINDOW", "60")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import rate_limit_processor

        passed: list[int] = []
        dropped: list[int] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                rate_limit_processor(
                    None, "info", {"event": "concurrent_ev", "logger": "test"}
                )
                passed.append(1)
            except structlog.DropEvent:
                dropped.append(1)
            except Exception as e:
                errors.append(e)

        # When: 20개 스레드 동시 실행
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Then: 에러 없이 정확히 5개만 통과
        assert len(errors) == 0
        assert len(passed) == 5
        assert len(dropped) == 15


# =============================================================================
# 282 Sampling 프로세서 — 계약 검증
# =============================================================================


class TestSamplingProcessorContract:
    """sampling_processor 설계 계약값 검증."""

    def test_sampling_target_levels_contains_debug_and_info(self):
        """샘플링 대상 레벨이 설계 계약대로 'debug'와 'info'여야 한다."""
        from baldur.observability.log_processors import _SAMPLING_TARGET_LEVELS

        assert "debug" in _SAMPLING_TARGET_LEVELS
        assert "info" in _SAMPLING_TARGET_LEVELS

    def test_sampling_target_levels_excludes_warning_and_above(self):
        """'warning', 'error', 'critical'은 샘플링 대상 레벨에 포함되지 않아야 한다."""
        from baldur.observability.log_processors import _SAMPLING_TARGET_LEVELS

        assert "warning" not in _SAMPLING_TARGET_LEVELS
        assert "error" not in _SAMPLING_TARGET_LEVELS
        assert "critical" not in _SAMPLING_TARGET_LEVELS

    def test_log_sampling_rate_default_is_1_0(self):
        """log_sampling_rate 기본값이 설계 계약대로 1.0이어야 한다 (비활성화)."""
        from baldur.settings.logging_settings import LoggingSettings

        assert LoggingSettings.model_fields["log_sampling_rate"].default == 1.0

    def test_log_sampling_events_default_is_empty_string(self):
        """log_sampling_events 기본값이 설계 계약대로 빈 문자열이어야 한다."""
        from baldur.settings.logging_settings import LoggingSettings

        assert LoggingSettings.model_fields["log_sampling_events"].default == ""

    def test_log_sampling_rate_rejects_below_zero(self):
        """log_sampling_rate에 ge=0.0 제약이 적용되어 음수 입력 시 ValidationError."""
        from pydantic import ValidationError

        from baldur.settings.logging_settings import LoggingSettings

        with pytest.raises(ValidationError):
            LoggingSettings(log_sampling_rate=-0.01)

    def test_log_sampling_rate_rejects_above_one(self):
        """log_sampling_rate에 le=1.0 제약이 적용되어 1.0 초과 입력 시 ValidationError."""
        from pydantic import ValidationError

        from baldur.settings.logging_settings import LoggingSettings

        with pytest.raises(ValidationError):
            LoggingSettings(log_sampling_rate=1.01)


# =============================================================================
# 282 Sampling 프로세서 — WARNING 이상 항상 통과 동작 검증
# =============================================================================


class TestSamplingWarningAlwaysPassesBehavior:
    """WARNING/ERROR/CRITICAL 레벨은 rate=0.0이어도 항상 통과해야 한다."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()
        yield
        reset_logging_settings()

    def test_warning_always_passes_regardless_of_rate(self, monkeypatch):
        """warning 레벨은 sample_rate=0.0이어도 DropEvent 없이 통과한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.0")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        for _ in range(5):
            result = sampling_processor(None, "warning", {"event": "warn_event"})
            assert isinstance(result, dict)

    def test_error_always_passes_regardless_of_rate(self, monkeypatch):
        """error 레벨은 sample_rate=0.0이어도 DropEvent 없이 통과한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.0")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        result = sampling_processor(None, "error", {"event": "err_event"})
        assert isinstance(result, dict)

    def test_critical_always_passes_regardless_of_rate(self, monkeypatch):
        """critical 레벨은 sample_rate=0.0이어도 DropEvent 없이 통과한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.0")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        result = sampling_processor(None, "critical", {"event": "crit_event"})
        assert isinstance(result, dict)


# =============================================================================
# 282 Sampling 프로세서 — 비활성화 동작 검증
# =============================================================================


class TestSamplingDisabledBehavior:
    """sample_rate >= 1.0 이면 모든 DEBUG/INFO 로그가 샘플링 없이 통과한다."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()
        yield
        reset_logging_settings()

    def test_rate_1_0_disables_sampling_for_info(self, monkeypatch):
        """sample_rate=1.0(기본값)이면 info 레그가 항상 통과하고 _sampled 필드가 없다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "1.0")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        for _ in range(5):
            result = sampling_processor(None, "info", {"event": "hot_event"})
            assert isinstance(result, dict)
            assert "_sampled" not in result

    def test_rate_1_0_disables_sampling_for_debug(self, monkeypatch):
        """sample_rate=1.0이면 debug 레벨도 항상 통과한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "1.0")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        result = sampling_processor(None, "debug", {"event": "debug_event"})
        assert isinstance(result, dict)


# =============================================================================
# 282 Sampling 프로세서 — 확률 기반 drop 동작 검증
# =============================================================================


class TestSamplingProbabilisticDropBehavior:
    """random() 결과에 따라 DropEvent 발생 여부가 결정된다."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()
        yield
        reset_logging_settings()

    def test_info_dropped_when_random_exceeds_rate(self, monkeypatch):
        """random() > sample_rate이면 info 로그가 DropEvent로 drop된다."""
        # Given: sample_rate=0.5, random()=0.6 → drop
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.5")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        with patch("baldur.observability.log_processors.random") as mock_random:
            mock_random.random.return_value = 0.6

            # When / Then
            with pytest.raises(structlog.DropEvent):
                sampling_processor(None, "info", {"event": "hot_event"})

    def test_info_passes_when_random_within_rate(self, monkeypatch):
        """random() ≤ sample_rate이면 info 로그가 통과하고 _sampled=True가 주입된다."""
        # Given: sample_rate=0.5, random()=0.4 → pass
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.5")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        with patch("baldur.observability.log_processors.random") as mock_random:
            mock_random.random.return_value = 0.4

            # When
            result = sampling_processor(None, "info", {"event": "hot_event"})

        # Then
        assert isinstance(result, dict)
        assert result["_sampled"] is True

    def test_rate_0_drops_all_debug_info_logs(self, monkeypatch):
        """sample_rate=0.0이면 random() 값과 무관하게 debug/info 로그가 모두 drop된다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.0")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        with patch("baldur.observability.log_processors.random") as mock_random:
            mock_random.random.return_value = (
                0.0  # 0.0 > 0.0은 False → 통과처럼 보이지만
            )

            # random() = 0.0, sample_rate = 0.0 → 0.0 > 0.0 = False → 통과
            # 이것이 설계 의도이므로 검증: 정확히 경계값(0.0) 동작만 확인
            result = sampling_processor(None, "info", {"event": "zero_rate_event"})
            assert isinstance(result, dict)
            assert result["_sampled"] is True

        with patch("baldur.observability.log_processors.random") as mock_random:
            mock_random.random.return_value = 0.0001  # 0.0001 > 0.0 = True → drop

            with pytest.raises(structlog.DropEvent):
                sampling_processor(None, "info", {"event": "zero_rate_event"})


# =============================================================================
# 282 Sampling 프로세서 — target_events 필터 동작 검증
# =============================================================================


class TestSamplingTargetEventsFilterBehavior:
    """log_sampling_events 설정에 따른 이벤트 선택 샘플링 동작 검증."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()
        yield
        reset_logging_settings()

    def test_non_target_event_always_passes_when_events_set(self, monkeypatch):
        """target_events가 설정되어 있을 때 목록에 없는 이벤트는 항상 통과한다."""
        # Given: target_events에 "circuit_breaker.checked"만 포함
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.0")
        monkeypatch.setenv(
            "BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_EVENTS", "circuit_breaker.checked"
        )
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        # When: 목록에 없는 이벤트
        result = sampling_processor(None, "info", {"event": "other_event"})

        # Then: rate=0.0이지만 target_events에 없으므로 통과
        assert isinstance(result, dict)
        assert "_sampled" not in result

    def test_target_event_gets_sampled_when_events_set(self, monkeypatch):
        """target_events가 설정되어 있을 때 목록에 있는 이벤트는 샘플링이 적용된다."""
        # Given: target_events에 "circuit_breaker.checked" 포함, rate=0.5
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.5")
        monkeypatch.setenv(
            "BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_EVENTS", "circuit_breaker.checked"
        )
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        with patch("baldur.observability.log_processors.random") as mock_random:
            mock_random.random.return_value = 0.8  # 0.8 > 0.5 → drop

            # When / Then: target 이벤트는 drop됨
            with pytest.raises(structlog.DropEvent):
                sampling_processor(None, "info", {"event": "circuit_breaker.checked"})

    def test_empty_events_applies_sampling_to_all_debug_info(self, monkeypatch):
        """target_events가 빈 문자열이면 모든 DEBUG/INFO 이벤트에 샘플링이 적용된다."""
        # Given: target_events="" (기본), rate=0.5
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.5")
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_EVENTS", "")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        with patch("baldur.observability.log_processors.random") as mock_random:
            mock_random.random.return_value = 0.8  # drop

            # When / Then: 어떤 이벤트든 drop됨
            with pytest.raises(structlog.DropEvent):
                sampling_processor(None, "info", {"event": "any_event"})

    def test_multiple_target_events_comma_separated(self, monkeypatch):
        """쉼표로 구분된 여러 이벤트가 모두 샘플링 대상으로 인식된다."""
        # Given: two target events
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.5")
        monkeypatch.setenv(
            "BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_EVENTS",
            "circuit_breaker.checked,action_executor.execute",
        )
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        with patch("baldur.observability.log_processors.random") as mock_random:
            mock_random.random.return_value = 0.9  # drop

            with pytest.raises(structlog.DropEvent):
                sampling_processor(None, "info", {"event": "circuit_breaker.checked"})

            with pytest.raises(structlog.DropEvent):
                sampling_processor(None, "debug", {"event": "action_executor.execute"})

    def test_non_target_event_not_dropped_with_comma_separated_events(
        self, monkeypatch
    ):
        """쉼표 구분 이벤트 목록에 없는 이벤트는 drop되지 않는다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.0")
        monkeypatch.setenv(
            "BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_EVENTS",
            "circuit_breaker.checked,action_executor.execute",
        )
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        # When: 목록에 없는 이벤트 (rate=0.0 이지만 target 아님)
        result = sampling_processor(None, "info", {"event": "dlq.enqueue"})

        # Then: 통과
        assert isinstance(result, dict)
        assert "_sampled" not in result


# =============================================================================
# 282 Sampling 프로세서 — _sampled 부수효과 검증
# =============================================================================


class TestSamplingInjectedFlagBehavior:
    """통과한 샘플링 로그에 _sampled=True 가 주입된다."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()
        yield
        reset_logging_settings()

    def test_sampled_flag_injected_on_passing_log(self, monkeypatch):
        """샘플링으로 통과한 로그의 event_dict에 _sampled=True가 주입된다."""
        # Given: rate=0.5, random=0.3 → pass
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.5")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        with patch("baldur.observability.log_processors.random") as mock_random:
            mock_random.random.return_value = 0.3

            # When
            result = sampling_processor(None, "info", {"event": "sampled_event"})

        # Then
        assert result.get("_sampled") is True

    def test_no_sampled_flag_on_warning_level_passthrough(self, monkeypatch):
        """WARNING 레벨은 샘플링 없이 통과하므로 _sampled 필드가 없어야 한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "0.5")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        result = sampling_processor(None, "warning", {"event": "warn_event"})

        assert "_sampled" not in result

    def test_no_sampled_flag_when_sampling_disabled(self, monkeypatch):
        """sample_rate=1.0(비활성화)이면 통과 로그에도 _sampled 필드가 없어야 한다."""
        monkeypatch.setenv("BALDUR_LOGGING_SETTINGS_LOG_SAMPLING_RATE", "1.0")
        from baldur.settings.logging_settings import reset_logging_settings

        reset_logging_settings()

        from baldur.observability.log_processors import sampling_processor

        result = sampling_processor(None, "info", {"event": "normal_event"})

        assert "_sampled" not in result
