"""
Postmortem Auto Trigger Tests (문서 128).

CB CLOSED 이벤트 발생 시 자동 Post-mortem 리포트 생성 테스트.

테스트 항목:
1. 설정 비활성화 시 Post-mortem 미생성 확인
2. 설정 활성화 시 자동 생성 확인
3. 최소 duration 미달 시 생성 스킵 확인
4. 저장된 인시던트 조회 확인
"""

from unittest.mock import patch


class TestAutoPostmortemSettings:
    """Auto Postmortem 설정 테스트."""

    def test_default_settings_disabled(self):
        """기본 설정에서 auto_postmortem_min_duration 기본값 확인."""
        from baldur.settings.api_view import (
            ApiViewSettings,
            reset_api_view_settings,
        )

        reset_api_view_settings()

        settings = ApiViewSettings()

        assert settings.auto_postmortem_min_duration == 30

    def test_settings_with_env_enabled(self, monkeypatch):
        """환경변수로 auto_postmortem_min_duration 설정 테스트."""
        from baldur.settings.api_view import (
            ApiViewSettings,
            reset_api_view_settings,
        )

        reset_api_view_settings()

        monkeypatch.setenv("BALDUR_API_VIEW_AUTO_POSTMORTEM_MIN_DURATION", "60")

        settings = ApiViewSettings()

        assert settings.auto_postmortem_min_duration == 60


class TestCircuitBreakerClosedPostmortemHandler:
    """CB CLOSED Postmortem 핸들러 테스트."""

    def setup_method(self):
        """테스트 전 이벤트 버스 및 설정 리셋."""
        from baldur.services.event_bus import get_event_bus
        from baldur.settings.api_view import reset_api_view_settings

        self.bus = get_event_bus()
        self.bus.reset()
        reset_api_view_settings()

    def teardown_method(self):
        """테스트 후 이벤트 버스 및 설정 리셋."""
        from baldur.settings.api_view import reset_api_view_settings

        self.bus.reset()
        reset_api_view_settings()

    def test_handler_skips_when_disabled(self, monkeypatch):
        """PostmortemSettings.auto_enabled=False일 때 Post-mortem 생성 스킵 확인."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_circuit_breaker_closed_postmortem,
        )
        from baldur.settings.postmortem import reset_postmortem_settings

        monkeypatch.setenv("BALDUR_POSTMORTEM_AUTO_ENABLED", "false")
        reset_postmortem_settings()

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={"service_name": "test_service"},
            source="test",
        )

        # 비활성화 상태에서는 핸들러가 초기 Settings 확인 후 바로 return해야 함
        # 따라서 logger.debug 메시지로 확인 (ImportError 발생 안 함)

        with patch("baldur.services.event_bus.bus._cb_handlers.logger") as mock_logger:
            _on_circuit_breaker_closed_postmortem(event)
            # DEBUG 로그가 호출되었는지 확인
            debug_calls = list(mock_logger.debug.call_args_list)
            assert any(
                "auto_postmortem_disabled_skipping" in str(call) for call in debug_calls
            )

    def test_handler_generates_postmortem_when_enabled_with_full_mocking(
        self, monkeypatch
    ):
        """auto_postmortem_enabled=True일 때 Celery task으로 Postmortem 생성이 위임되는지 확인."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_circuit_breaker_closed_postmortem,
        )
        from baldur.settings.postmortem import reset_postmortem_settings

        # 핸들러는 PostmortemSettings (BALDUR_POSTMORTEM_ prefix) 사용
        monkeypatch.setenv("BALDUR_POSTMORTEM_AUTO_ENABLED", "true")
        monkeypatch.setenv("BALDUR_POSTMORTEM_AUTO_MIN_DURATION", "0")
        monkeypatch.setenv("BALDUR_POSTMORTEM_INCIDENT_GROUP_ENABLED", "false")
        reset_postmortem_settings()

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={"service_name": "test_service"},
            source="test",
        )

        # Celery task .delay() mock
        with patch(
            "baldur.adapters.celery.tasks.postmortem.process_individual_postmortem.delay"
        ) as mock_delay:
            _on_circuit_breaker_closed_postmortem(event)

            # Celery task가 위임되었는지 확인
            mock_delay.assert_called_once()
            call_kwargs = mock_delay.call_args[1]
            assert call_kwargs["service_name"] == "test_service"
            assert call_kwargs["event_type"] == "circuit_breaker_closed"
            assert isinstance(call_kwargs["event_data"], dict)
            assert isinstance(call_kwargs["event_bus_history"], list)

    def test_handler_delegates_to_celery_when_duration_below_minimum(self, monkeypatch):
        """duration이 min_duration 미만일 때도 Celery task로 위임되는지 확인 (duration 체크는 task 내부에서 수행)."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_circuit_breaker_closed_postmortem,
        )
        from baldur.settings.postmortem import reset_postmortem_settings

        # 올바른 prefix: BALDUR_POSTMORTEM_ 사용
        monkeypatch.setenv("BALDUR_POSTMORTEM_AUTO_ENABLED", "true")
        monkeypatch.setenv("BALDUR_POSTMORTEM_AUTO_MIN_DURATION", "60")
        monkeypatch.setenv("BALDUR_POSTMORTEM_INCIDENT_GROUP_ENABLED", "false")
        reset_postmortem_settings()

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={"service_name": "test_service"},
            source="test",
        )

        # Celery task가 위임되는지 확인 (duration 체크는 task 내부에서 수행)
        with patch(
            "baldur.adapters.celery.tasks.postmortem.process_individual_postmortem.delay"
        ) as mock_delay:
            _on_circuit_breaker_closed_postmortem(event)

            # Celery task에 위임됨 — duration 체크는 task에서 수행
            mock_delay.assert_called_once()


class TestPostmortemHandlerRegistration:
    """Postmortem 핸들러 등록 테스트."""

    def setup_method(self):
        """테스트 전 이벤트 버스 리셋."""
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        """테스트 후 이벤트 버스 리셋."""
        self.bus.reset()

    def test_postmortem_handler_registered_with_low_priority(self):
        """Postmortem 핸들러가 LOW 우선순위로 등록되는지 확인."""
        from baldur.services.event_bus import (
            EventPriority,
            EventType,
            _on_circuit_breaker_closed_postmortem,
            register_default_handlers,
        )

        register_default_handlers()

        # CB CLOSED 구독 목록 확인 (속성명: _subscriptions)
        subscriptions = self.bus._subscriptions.get(
            EventType.CIRCUIT_BREAKER_CLOSED, []
        )

        # 핸들러 중 postmortem 핸들러 찾기
        postmortem_subs = [
            s
            for s in subscriptions
            if s.handler == _on_circuit_breaker_closed_postmortem
        ]

        assert len(postmortem_subs) == 1
        assert postmortem_subs[0].priority == EventPriority.LOW

    def test_both_cb_closed_handlers_registered(self):
        """CB CLOSED에 replay 핸들러와 postmortem 핸들러 모두 등록되는지 확인."""
        from baldur.services.event_bus import (
            EventType,
            _on_circuit_breaker_closed,
            _on_circuit_breaker_closed_postmortem,
            register_default_handlers,
        )

        register_default_handlers()

        subscriptions = self.bus._subscriptions.get(
            EventType.CIRCUIT_BREAKER_CLOSED, []
        )
        handler_funcs = [s.handler for s in subscriptions]

        assert _on_circuit_breaker_closed in handler_funcs
        assert _on_circuit_breaker_closed_postmortem in handler_funcs


class TestEventTriggeredPostmortem:
    """이벤트 트리거를 통한 Postmortem 생성 통합 테스트."""

    def setup_method(self):
        """테스트 전 이벤트 버스 리셋."""
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        """테스트 후 이벤트 버스 리셋."""
        self.bus.reset()

    def test_cb_closed_event_triggers_postmortem_handler(self, monkeypatch):
        """CB CLOSED 이벤트 발행 시 postmortem 핸들러가 호출되는지 확인."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_circuit_breaker_closed_postmortem,
            register_default_handlers,
        )
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.api_view import reset_api_view_settings

        reset_api_view_settings()

        # 기본 핸들러 등록
        register_default_handlers()

        # 핸들러 호출 추적
        handler_called = []

        original_handler = _on_circuit_breaker_closed_postmortem

        def tracking_handler(event):
            handler_called.append(event)
            # 원래 핸들러는 호출하지 않음 (mock 환경이므로)

        # 모든 CB CLOSED 핸들러를 no-op으로 교체하되, postmortem 핸들러만 추적용으로 교체
        # (다른 핸들러가 Celery .delay() → Redis 연결을 시도하여 123초 타임아웃 발생 방지)
        subscriptions = self.bus._subscriptions.get(
            EventType.CIRCUIT_BREAKER_CLOSED, []
        )
        for sub in subscriptions:
            if sub.handler == original_handler:
                sub.handler = tracking_handler
            else:
                sub.handler = lambda event: None  # no-op으로 교체

        # 이벤트 발행
        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_CLOSED,
            data={"service_name": "test_service"},
            source="test",
        )
        self.bus.publish(event)

        # The postmortem handler is dispatched fire-and-forget (636 D2) — under
        # the default async_pool dispatch publish() does not wait for the handler
        # body to finish, so drain the shared dispatch executor to complete the
        # in-flight handler before the tracking assertion.
        BaldurEventBus.shutdown_dispatch_executor()

        # 핸들러가 호출되었는지 확인
        assert len(handler_called) == 1
        assert handler_called[0].data["service_name"] == "test_service"


class TestGeneratePostmortemDataPure:
    """postmortem 데이터 구조 테스트 (Django 의존성 없이)."""

    def test_postmortem_data_has_required_fields(self):
        """postmortem 데이터에 필수 필드가 포함되어 있는지 확인."""
        # 순수 utils 모듈만 사용하여 테스트
        from baldur.utils.duration import calculate_incident_duration

        timeline = [
            {
                "timestamp": "2026-01-27T10:00:00+00:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "test_service"},
            },
            {
                "timestamp": "2026-01-27T10:05:00+00:00",
                "event_type": "circuit_breaker_closed",
                "details": {"service_name": "test_service"},
            },
        ]

        current_time = "2026-01-27T10:10:00+00:00"
        result = calculate_incident_duration(timeline, current_time)

        # duration 계산 결과 확인
        assert result.started_at is not None
        assert result.resolved_at is not None
        assert result.duration_seconds is not None
        assert result.duration_seconds >= 0

    def test_postmortem_action_items_generation(self):
        """동적 action items 생성 테스트."""
        from baldur.utils.postmortem_actions import generate_dynamic_actions

        timeline = [
            {
                "timestamp": "2026-01-27T10:00:00+00:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "test_service"},
            },
        ]
        affected_services = ["test_service"]
        duration_seconds = 300.0
        current_timestamp = "2026-01-27T10:05:00+00:00"

        auto_actions, recommendations = generate_dynamic_actions(
            timeline=timeline,
            affected_services=affected_services,
            duration_seconds=duration_seconds,
            current_timestamp=current_timestamp,
        )

        # 결과 타입 확인
        assert isinstance(auto_actions, list)
        assert isinstance(recommendations, list)
