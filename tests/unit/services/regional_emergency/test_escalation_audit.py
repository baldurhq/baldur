"""
EscalationAuditTrail 단위 테스트.

오버라이드 의사결정 이유 Audit 로그 테스트.

테스트 대상:
- 의사결정 기록
- Global 오버라이드 기록
- Admin 오버라이드 기록
- Regional STRICT 기록
- Cascade Escalation 기록
- Fallback 기록
- 조회 및 필터링
- 통계
"""

import pytest

from baldur.services.regional_emergency.escalation_audit import (
    EscalationAuditEntry,
    EscalationAuditTrail,
    EscalationDecisionType,
    get_escalation_audit_trail,
    reset_escalation_audit_trail,
)


class TestEscalationAuditEntry:
    """EscalationAuditEntry 테스트."""

    def test_default_values(self):
        """기본값 생성."""
        entry = EscalationAuditEntry()

        assert entry.event_id.startswith("esc-")
        assert entry.decision_type == ""
        assert entry.decision_reason == ""
        assert entry.namespace == ""
        assert entry.effective_state == {}
        assert entry.overridden_state is None
        assert entry.triggered_by == ""
        assert entry.precedence is None
        assert entry.timestamp is not None

    def test_event_id_format(self):
        """이벤트 ID 형식."""
        entry = EscalationAuditEntry()

        assert entry.event_id.startswith("esc-")
        assert len(entry.event_id) == 16  # "esc-" + 12 hex chars

    def test_to_dict(self):
        """딕셔너리 변환."""
        entry = EscalationAuditEntry(
            decision_type="GLOBAL_OVERRIDE",
            decision_reason="Test reason",
            namespace="seoul",
            effective_state={"governance_mode": "STRICT"},
            triggered_by="system",
        )

        d = entry.to_dict()

        assert d["decision_type"] == "GLOBAL_OVERRIDE"
        assert d["decision_reason"] == "Test reason"
        assert d["namespace"] == "seoul"
        assert d["effective_state"]["governance_mode"] == "STRICT"
        assert d["triggered_by"] == "system"

    def test_from_dict(self):
        """딕셔너리에서 생성."""
        data = {
            "event_id": "esc-test123456",
            "decision_type": "ADMIN_OVERRIDE",
            "decision_reason": "Admin override test",
            "namespace": "tokyo",
            "effective_state": {"governance_mode": "NORMAL"},
            "triggered_by": "admin@test.com",
            "precedence": "ADMIN_OVERRIDE",
            "ttl_minutes": 60,
        }

        entry = EscalationAuditEntry.from_dict(data)

        assert entry.event_id == "esc-test123456"
        assert entry.decision_type == "ADMIN_OVERRIDE"
        assert entry.namespace == "tokyo"
        assert entry.ttl_minutes == 60


class TestEscalationDecisionType:
    """EscalationDecisionType 상수 테스트."""

    def test_all_decision_types_defined(self):
        """모든 의사결정 유형 정의 확인."""
        assert EscalationDecisionType.GLOBAL_OVERRIDE == "GLOBAL_OVERRIDE"
        assert EscalationDecisionType.ADMIN_OVERRIDE == "ADMIN_OVERRIDE"
        assert EscalationDecisionType.SAFETY_MAX == "SAFETY_MAX"
        assert EscalationDecisionType.REGIONAL_DEFAULT == "REGIONAL_DEFAULT"
        assert EscalationDecisionType.CASCADE_ESCALATION == "CASCADE_ESCALATION"
        assert EscalationDecisionType.PARTITION_FALLBACK == "PARTITION_FALLBACK"
        assert EscalationDecisionType.REGIONAL_STRICT == "REGIONAL_STRICT"
        assert EscalationDecisionType.FALLBACK == "FALLBACK"


class TestEscalationAuditTrail:
    """EscalationAuditTrail 테스트."""

    @pytest.fixture
    def audit_trail(self):
        """새 AuditTrail 인스턴스."""
        return EscalationAuditTrail()

    def teardown_method(self):
        """테스트 후 싱글톤 리셋."""
        reset_escalation_audit_trail()

    # =========================================================================
    # log_decision 테스트
    # =========================================================================

    def test_log_decision_returns_event_id(self, audit_trail):
        """log_decision이 이벤트 ID 반환."""
        event_id = audit_trail.log_decision(
            decision_type="TEST",
            decision_reason="Test reason",
            namespace="test",
            effective_state={"test": True},
        )

        assert event_id.startswith("esc-")

    def test_log_decision_stores_entry(self, audit_trail):
        """log_decision이 엔트리 저장."""
        audit_trail.log_decision(
            decision_type="TEST",
            decision_reason="Test reason",
            namespace="test",
            effective_state={"test": True},
        )

        decisions = audit_trail.get_recent_decisions()
        assert len(decisions) == 1
        assert decisions[0]["decision_type"] == "TEST"

    def test_log_decision_with_all_fields(self, audit_trail):
        """log_decision 모든 필드."""
        event_id = audit_trail.log_decision(
            decision_type="GLOBAL_OVERRIDE",
            decision_reason="Full test",
            namespace="seoul",
            effective_state={"governance_mode": "STRICT"},
            overridden_state={"governance_mode": "NORMAL"},
            triggered_by="system",
            precedence="AUTO",
            global_state={"scope": "global"},
            regional_state={"scope": "regional"},
            ttl_minutes=120,
        )

        decision = audit_trail.get_decision_by_id(event_id)

        assert decision["decision_type"] == "GLOBAL_OVERRIDE"
        assert decision["namespace"] == "seoul"
        assert decision["overridden_state"]["governance_mode"] == "NORMAL"
        assert decision["precedence"] == "AUTO"
        assert decision["ttl_minutes"] == 120

    # =========================================================================
    # log_global_override 테스트
    # =========================================================================

    def test_log_global_override(self, audit_trail):
        """Global 오버라이드 기록."""
        global_state = {"governance_mode": "STRICT", "emergency_level": 3}
        regional_state = {"governance_mode": "NORMAL", "emergency_level": "normal"}

        event_id = audit_trail.log_global_override(
            namespace="seoul",
            global_state=global_state,
            regional_state=regional_state,
            triggered_by="system",
        )

        assert event_id.startswith("esc-")

        decisions = audit_trail.get_recent_decisions(namespace="seoul")
        assert len(decisions) == 1
        assert decisions[0]["decision_type"] == "GLOBAL_OVERRIDE"
        assert "overrides regional seoul" in decisions[0]["decision_reason"]

    def test_log_global_override_preserves_overridden_state(self, audit_trail):
        """Global 오버라이드 시 덮어씌워진 상태 보존."""
        global_state = {"governance_mode": "STRICT", "emergency_level": 3}
        regional_state = {"governance_mode": "NORMAL", "emergency_level": "normal"}

        event_id = audit_trail.log_global_override(
            namespace="oregon",
            global_state=global_state,
            regional_state=regional_state,
        )

        decision = audit_trail.get_decision_by_id(event_id)

        assert decision["overridden_state"] == regional_state
        assert decision["global_state_snapshot"] == global_state
        assert decision["regional_state_snapshot"] == regional_state

    # =========================================================================
    # log_admin_override 테스트
    # =========================================================================

    def test_log_admin_override(self, audit_trail):
        """Admin 오버라이드 기록."""
        regional_state = {"governance_mode": "NORMAL"}
        global_state = {"governance_mode": "STRICT"}

        audit_trail.log_admin_override(
            namespace="tokyo",
            regional_state=regional_state,
            global_state=global_state,
            triggered_by="admin@company.com",
            precedence="ADMIN_OVERRIDE",
        )

        decisions = audit_trail.get_recent_decisions()
        assert decisions[0]["decision_type"] == "ADMIN_OVERRIDE"
        assert decisions[0]["triggered_by"] == "admin@company.com"

    def test_log_admin_override_with_ttl(self, audit_trail):
        """Admin 오버라이드 TTL 포함."""
        event_id = audit_trail.log_admin_override(
            namespace="tokyo",
            regional_state={"governance_mode": "NORMAL"},
            global_state={"governance_mode": "STRICT"},
            triggered_by="admin@company.com",
            precedence="KILL_SWITCH",
            ttl_minutes=60,
        )

        decision = audit_trail.get_decision_by_id(event_id)

        assert "[TTL: 60m]" in decision["decision_reason"]
        assert decision["ttl_minutes"] == 60

    # =========================================================================
    # log_regional_strict 테스트
    # =========================================================================

    def test_log_regional_strict(self, audit_trail):
        """Regional STRICT 기록."""
        regional_state = {"governance_mode": "STRICT", "emergency_level": 2}
        global_state = {"governance_mode": "NORMAL"}

        audit_trail.log_regional_strict(
            namespace="osaka",
            regional_state=regional_state,
            global_state=global_state,
        )

        decisions = audit_trail.get_recent_decisions()
        assert decisions[0]["decision_type"] == "REGIONAL_STRICT"
        assert "Regional STRICT active" in decisions[0]["decision_reason"]

    # =========================================================================
    # log_cascade_escalation 테스트
    # =========================================================================

    def test_log_cascade_escalation(self, audit_trail):
        """Cascade Escalation 기록."""
        audit_trail.log_cascade_escalation(
            affected_regions=["seoul", "tokyo", "osaka"],
            triggered_by="RegionalCascadeDetector",
        )

        decisions = audit_trail.get_recent_decisions()
        assert decisions[0]["decision_type"] == "CASCADE_ESCALATION"
        assert "3 regions affected" in decisions[0]["decision_reason"]
        assert "seoul, tokyo, osaka" in decisions[0]["decision_reason"]

    # =========================================================================
    # log_fallback 테스트
    # =========================================================================

    def test_log_fallback(self, audit_trail):
        """Fallback 기록."""
        audit_trail.log_fallback(
            namespace="nagoya",
            error="Connection refused",
            triggered_by="AtomicStateQuery",
        )

        decisions = audit_trail.get_recent_decisions()
        assert decisions[0]["decision_type"] == "FALLBACK"
        assert "Connection refused" in decisions[0]["decision_reason"]
        assert decisions[0]["effective_state"]["governance_mode"] == "NORMAL"

    # =========================================================================
    # get_recent_decisions 테스트
    # =========================================================================

    def test_get_recent_decisions_limit(self, audit_trail):
        """최근 의사결정 개수 제한."""
        for i in range(10):
            audit_trail.log_decision(
                decision_type="TEST",
                decision_reason=f"Test {i}",
                namespace="test",
                effective_state={},
            )

        decisions = audit_trail.get_recent_decisions(limit=5)
        assert len(decisions) == 5

    def test_get_recent_decisions_filter_by_namespace(self, audit_trail):
        """네임스페이스별 필터링."""
        audit_trail.log_decision(
            decision_type="TEST",
            decision_reason="Seoul",
            namespace="seoul",
            effective_state={},
        )
        audit_trail.log_decision(
            decision_type="TEST",
            decision_reason="Tokyo",
            namespace="tokyo",
            effective_state={},
        )
        audit_trail.log_decision(
            decision_type="TEST",
            decision_reason="Seoul 2",
            namespace="seoul",
            effective_state={},
        )

        seoul_decisions = audit_trail.get_recent_decisions(namespace="seoul")
        assert len(seoul_decisions) == 2
        assert all(d["namespace"] == "seoul" for d in seoul_decisions)

    def test_get_recent_decisions_filter_by_type(self, audit_trail):
        """의사결정 유형별 필터링."""
        audit_trail.log_decision(
            decision_type="GLOBAL_OVERRIDE",
            decision_reason="GO",
            namespace="test",
            effective_state={},
        )
        audit_trail.log_decision(
            decision_type="ADMIN_OVERRIDE",
            decision_reason="AO",
            namespace="test",
            effective_state={},
        )
        audit_trail.log_decision(
            decision_type="GLOBAL_OVERRIDE",
            decision_reason="GO2",
            namespace="test",
            effective_state={},
        )

        go_decisions = audit_trail.get_recent_decisions(decision_type="GLOBAL_OVERRIDE")
        assert len(go_decisions) == 2
        assert all(d["decision_type"] == "GLOBAL_OVERRIDE" for d in go_decisions)

    def test_get_recent_decisions_order(self, audit_trail):
        """최신순 정렬."""
        audit_trail.log_decision(
            decision_type="TEST",
            decision_reason="First",
            namespace="test",
            effective_state={},
        )
        audit_trail.log_decision(
            decision_type="TEST",
            decision_reason="Second",
            namespace="test",
            effective_state={},
        )
        audit_trail.log_decision(
            decision_type="TEST",
            decision_reason="Third",
            namespace="test",
            effective_state={},
        )

        decisions = audit_trail.get_recent_decisions()

        # 최신순 (Third, Second, First)
        assert decisions[0]["decision_reason"] == "Third"
        assert decisions[1]["decision_reason"] == "Second"
        assert decisions[2]["decision_reason"] == "First"

    # =========================================================================
    # get_decision_by_id 테스트
    # =========================================================================

    def test_get_decision_by_id_found(self, audit_trail):
        """ID로 의사결정 조회 - 발견."""
        event_id = audit_trail.log_decision(
            decision_type="TEST",
            decision_reason="Find me",
            namespace="test",
            effective_state={},
        )

        decision = audit_trail.get_decision_by_id(event_id)

        assert decision is not None
        assert decision["event_id"] == event_id
        assert decision["decision_reason"] == "Find me"

    def test_get_decision_by_id_not_found(self, audit_trail):
        """ID로 의사결정 조회 - 미발견."""
        decision = audit_trail.get_decision_by_id("esc-nonexistent")
        assert decision is None

    # =========================================================================
    # get_stats 테스트
    # =========================================================================

    def test_get_stats(self, audit_trail):
        """통계 조회."""
        audit_trail.log_global_override(
            namespace="seoul",
            global_state={"governance_mode": "STRICT"},
            regional_state={"governance_mode": "NORMAL"},
        )
        audit_trail.log_admin_override(
            namespace="tokyo",
            regional_state={"governance_mode": "NORMAL"},
            global_state={"governance_mode": "STRICT"},
            triggered_by="admin",
            precedence="ADMIN_OVERRIDE",
        )
        audit_trail.log_global_override(
            namespace="seoul",
            global_state={"governance_mode": "STRICT"},
            regional_state={"governance_mode": "NORMAL"},
        )

        stats = audit_trail.get_stats()

        assert stats["total_entries"] == 3
        assert stats["by_type"]["GLOBAL_OVERRIDE"] == 2
        assert stats["by_type"]["ADMIN_OVERRIDE"] == 1
        assert stats["by_namespace"]["seoul"] == 2
        assert stats["by_namespace"]["tokyo"] == 1

    # =========================================================================
    # 버퍼 관리 테스트
    # =========================================================================

    def test_buffer_size_limit(self):
        """버퍼 크기 제한."""
        audit_trail = EscalationAuditTrail(max_buffer_size=5)

        for i in range(10):
            audit_trail.log_decision(
                decision_type="TEST",
                decision_reason=f"Entry {i}",
                namespace="test",
                effective_state={},
            )

        decisions = audit_trail.get_recent_decisions(limit=100)
        assert len(decisions) == 5
        # 최신 5개만 유지
        assert decisions[0]["decision_reason"] == "Entry 9"
        assert decisions[4]["decision_reason"] == "Entry 5"

    def test_clear(self, audit_trail):
        """버퍼 초기화."""
        audit_trail.log_decision(
            decision_type="TEST",
            decision_reason="Clear me",
            namespace="test",
            effective_state={},
        )

        assert len(audit_trail.get_recent_decisions()) == 1

        audit_trail.clear()

        assert len(audit_trail.get_recent_decisions()) == 0

    # =========================================================================
    # 스레드 안전성 테스트
    # =========================================================================

    def test_thread_safety(self, audit_trail):
        """스레드 안전성."""
        import threading

        def log_entries(count):
            for i in range(count):
                audit_trail.log_decision(
                    decision_type="THREAD_TEST",
                    decision_reason=f"Thread entry {i}",
                    namespace="thread_test",
                    effective_state={},
                )

        threads = [threading.Thread(target=log_entries, args=(10,)) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        decisions = audit_trail.get_recent_decisions(limit=100)
        assert len(decisions) == 50  # 5 threads * 10 entries


class TestEscalationAuditTrailSingleton:
    """EscalationAuditTrail 싱글톤 테스트."""

    def teardown_method(self):
        reset_escalation_audit_trail()

    def test_singleton_returns_same_instance(self):
        """싱글톤이 같은 인스턴스 반환."""
        trail1 = get_escalation_audit_trail()
        trail2 = get_escalation_audit_trail()

        assert trail1 is trail2

    def test_reset_clears_singleton(self):
        """리셋 후 새 인스턴스 생성."""
        trail1 = get_escalation_audit_trail()
        reset_escalation_audit_trail()
        trail2 = get_escalation_audit_trail()

        assert trail1 is not trail2
