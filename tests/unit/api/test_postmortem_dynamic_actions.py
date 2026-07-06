"""
Postmortem 동적 Action Items 생성 테스트.

테스트 대상:
1. generate_dynamic_actions - 타임라인 기반 동적 actions/recommendations 생성

테스트 케이스:
- Circuit Breaker OPEN 이벤트가 있을 때 Action 생성 확인
- Circuit Breaker HALF_OPEN/CLOSED 이벤트 Action 생성 확인
- 빈 타임라인일 때 기본 메시지 반환
- 느린 복구 시(120초 초과) Recommendation 생성 확인
- 다중 서비스 장애 시(3개 초과) Recommendation 생성 확인
- Emergency/Kill Switch 이벤트 Action 생성 확인
"""

from baldur.utils.postmortem_actions import generate_dynamic_actions


class TestGenerateDynamicActions:
    """generate_dynamic_actions 함수 테스트."""

    def test_circuit_breaker_opened_event_creates_action(self):
        """Circuit Breaker OPEN 이벤트가 있을 때 Action 생성."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
        ]

        actions, recommendations = generate_dynamic_actions(timeline, [], 30.0)

        assert len(actions) >= 1
        cb_open_action = next(
            (a for a in actions if "OPEN" in a["action"]),
            None,
        )
        assert cb_open_action is not None
        assert cb_open_action["status"] == "completed"
        assert cb_open_action["service"] == "database"

    def test_circuit_breaker_half_open_and_closed_events(self):
        """HALF_OPEN과 CLOSED 이벤트 Action 생성."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "api"},
            },
            {
                "timestamp": "2026-01-27T14:00:30+09:00",
                "event_type": "circuit_breaker_half_opened",
                "details": {"service_name": "api"},
            },
            {
                "timestamp": "2026-01-27T14:00:35+09:00",
                "event_type": "circuit_breaker_closed",
                "details": {"service_name": "api"},
            },
        ]

        actions, _ = generate_dynamic_actions(timeline, [], 35.0)

        action_texts = [a["action"] for a in actions]
        assert any("OPEN" in text for text in action_texts)
        assert any("HALF_OPEN" in text for text in action_texts)
        assert any("CLOSED" in text for text in action_texts)

    def test_empty_timeline_returns_default_action(self):
        """빈 타임라인일 때 기본 인시던트 기록 메시지 반환."""
        actions, recommendations = generate_dynamic_actions([], [], None)

        assert len(actions) == 1
        assert "Incident recorded" in actions[0]["action"]
        assert actions[0]["status"] == "completed"

    def test_slow_recovery_over_120_seconds_creates_recommendation(self):
        """복구 시간이 120초 초과 시 SLA 검토 Recommendation 생성."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
        ]

        _, recommendations = generate_dynamic_actions(timeline, [], 150.0)

        assert any("SLA review" in r for r in recommendations)
        assert any("150" in r for r in recommendations)

    def test_slow_recovery_60_to_120_seconds_creates_recommendation(self):
        """복구 시간이 60-120초일 때 개선 검토 Recommendation 생성."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
        ]

        _, recommendations = generate_dynamic_actions(timeline, [], 90.0)

        assert any("improvement review" in r for r in recommendations)
        assert any("90" in r for r in recommendations)

    def test_multiple_affected_services_creates_recommendation(self):
        """3개 초과 서비스 장애 시 공통 원인 분석 Recommendation 생성."""
        affected_services = ["db", "api", "cache", "queue"]  # 4개

        _, recommendations = generate_dynamic_actions([], affected_services, 30.0)

        assert any("Multi-service failure" in r for r in recommendations)
        assert any("4" in r for r in recommendations)

    def test_emergency_activated_event_creates_action(self):
        """비상 모드 활성화 이벤트 Action 생성."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "emergency_activated",
                "details": {"service_name": "system"},
            },
        ]

        actions, _ = generate_dynamic_actions(timeline, [], 30.0)

        action_texts = [a["action"] for a in actions]
        assert any("Emergency mode" in text for text in action_texts)

    def test_kill_switch_activated_event_creates_action(self):
        """Kill Switch 활성화 이벤트 Action 생성."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "kill_switch_activated",
                "details": {"service_name": "feature-x"},
            },
        ]

        actions, _ = generate_dynamic_actions(timeline, [], 30.0)

        action_texts = [a["action"] for a in actions]
        assert any("Kill switch" in text for text in action_texts)

    def test_error_budget_critical_event_creates_action(self):
        """Error Budget 임계치 경고 이벤트 Action 생성."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "error_budget_critical",
                "details": {"remaining": 5.0},
            },
        ]

        actions, _ = generate_dynamic_actions(timeline, [], 30.0)

        action_texts = [a["action"] for a in actions]
        assert any("Error budget" in text for text in action_texts)

    def test_no_duration_with_no_affected_services_returns_default_recommendation(self):
        """duration이 None이고 affected_services가 없고 정상 복구됐을 때 기본 recommendation 반환."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "test"},
            },
            {
                "timestamp": "2026-01-27T14:00:30+09:00",
                "event_type": "circuit_breaker_closed",
                "details": {"service_name": "test"},
            },
        ]

        _, recommendations = generate_dynamic_actions(timeline, [], None)

        assert len(recommendations) >= 1
        assert any(
            "Root cause analysis" in r or "recurrence prevention" in r
            for r in recommendations
        )

    def test_action_structure_matches_google_sre_standard(self):
        """Action 구조가 Google SRE 표준 형식과 일치."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:01:24+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
        ]

        actions, _ = generate_dynamic_actions(timeline, [], 30.0)

        for action in actions:
            assert "action" in action
            assert "status" in action
            assert "timestamp" in action
            assert "service" in action
            assert action["status"] in ["completed", "in_progress", "pending"]

    def test_duplicate_events_same_service_not_repeated(self):
        """동일 서비스의 중복 이벤트는 반복되지 않음."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
            {
                "timestamp": "2026-01-27T14:00:10+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
        ]

        actions, _ = generate_dynamic_actions(timeline, [], 30.0)

        cb_open_actions = [a for a in actions if "OPEN" in a["action"]]
        # 동일 서비스의 OPEN 이벤트는 하나만 기록
        assert len(cb_open_actions) == 1

    def test_multiple_services_each_tracked_separately(self):
        """서로 다른 서비스의 이벤트는 각각 추적됨."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
            {
                "timestamp": "2026-01-27T14:00:05+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "api"},
            },
        ]

        actions, _ = generate_dynamic_actions(timeline, [], 30.0)

        cb_open_actions = [a for a in actions if "OPEN" in a["action"]]
        services = [a["service"] for a in cb_open_actions]
        assert "database" in services
        assert "api" in services

    def test_dlq_item_added_event_creates_action(self):
        """DLQ 항목 적재 이벤트가 있을 때 Action 생성."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "dlq_item_added",
                "details": {"service_name": "payment", "count": 5},
            },
        ]

        actions, _ = generate_dynamic_actions(timeline, [], 30.0)

        dlq_action = next(
            (a for a in actions if "DLQ" in a["action"]),
            None,
        )
        assert dlq_action is not None
        assert dlq_action["status"] == "completed"
        assert dlq_action["service"] == "payment"

    def test_dlq_replay_blocked_event_creates_action(self):
        """DLQ Replay 차단 이벤트가 있을 때 Action 생성."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "dlq_replay_blocked",
                "details": {"service_name": "order"},
            },
        ]

        actions, _ = generate_dynamic_actions(timeline, [], 30.0)

        dlq_action = next(
            (
                a
                for a in actions
                if "DLQ" in a["action"] and "replay" in a["action"].lower()
            ),
            None,
        )
        assert dlq_action is not None
        assert dlq_action["status"] == "completed"

    def test_cb_open_without_recovery_generates_fast_fail_recommendation(self):
        """CB OPEN 후 복구(HALF_OPEN/CLOSED) 없으면 Fast Fail 점검 권장."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
        ]

        _, recommendations = generate_dynamic_actions(timeline, [], 30.0)

        assert any("Fast fail not triggered" in r for r in recommendations)
        assert any("circuit breaker configuration review" in r for r in recommendations)

    def test_cb_open_with_recovery_no_fast_fail_recommendation(self):
        """CB OPEN 후 복구되면 Fast Fail 점검 권장 없음."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
            {
                "timestamp": "2026-01-27T14:00:30+09:00",
                "event_type": "circuit_breaker_half_opened",
                "details": {"service_name": "database"},
            },
        ]

        _, recommendations = generate_dynamic_actions(timeline, [], 30.0)

        assert not any("Fast fail not triggered" in r for r in recommendations)

    def test_cb_open_with_closed_no_fast_fail_recommendation(self):
        """CB OPEN 후 CLOSED 되면 Fast Fail 점검 권장 없음."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "circuit_breaker_opened",
                "details": {"service_name": "database"},
            },
            {
                "timestamp": "2026-01-27T14:00:35+09:00",
                "event_type": "circuit_breaker_closed",
                "details": {"service_name": "database"},
            },
        ]

        _, recommendations = generate_dynamic_actions(timeline, [], 30.0)

        assert not any("Fast fail not triggered" in r for r in recommendations)

    def test_error_budget_warning_event_creates_action(self):
        """Error Budget 경고 이벤트 Action 생성."""
        timeline = [
            {
                "timestamp": "2026-01-27T14:00:00+09:00",
                "event_type": "error_budget_warning",
                "details": {"remaining": 15.0},
            },
        ]

        actions, _ = generate_dynamic_actions(timeline, [], 30.0)

        action_texts = [a["action"] for a in actions]
        assert any("Error budget" in text for text in action_texts)
