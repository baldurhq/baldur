"""
Emergency Postmortem Deep Links & CascadeEvent Integration Tests.

_generate_emergency_postmortem_data()에서 deep_links, cascade_event_id,
causation_chain, evidence_hash 필드가 올바르게 생성되는지 테스트합니다.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest


class TestEmergencyPostmortemDeepLinks:
    """_generate_emergency_postmortem_data()의 deep_links 통합 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, monkeypatch):
        """테스트 전후로 빌더 + settings 싱글톤 리셋.

        impl 527 (v1.1 deferred) flipped deep_links_enabled default to
        False; re-enable here so the deep-link populated test passes.
        """
        pytest.importorskip("baldur_pro")
        from baldur.settings.postmortem import reset_postmortem_settings
        from baldur_pro.services.postmortem.deep_links import (
            reset_postmortem_deep_link_builder,
        )

        monkeypatch.setenv("BALDUR_POSTMORTEM_DEEP_LINKS_ENABLED", "true")
        reset_postmortem_deep_link_builder()
        reset_postmortem_settings()
        yield
        reset_postmortem_deep_link_builder()
        reset_postmortem_settings()

    def test_deep_links_field_exists_in_emergency_postmortem(self):
        """Emergency postmortem 반환값에 deep_links 필드가 존재."""
        from baldur.services.event_bus import _generate_emergency_postmortem_data

        session_data = {
            "session_id": "recovery-test-001",
            "namespace": "seoul",
            "trigger_level": "LEVEL_3",
            "started_at": "2026-01-28T10:00:00Z",
            "completed_at": "2026-01-28T10:30:00Z",
            "duration_seconds": 1800,
            "steps_executed": 4,
            "total_steps": 4,
            "requires_approval": False,
        }
        event_bus_history = []
        snapshot = {"status": "recovered"}

        result = _generate_emergency_postmortem_data(
            session_data=session_data,
            event_bus_history=event_bus_history,
            snapshot=snapshot,
        )

        assert "deep_links" in result
        assert isinstance(result["deep_links"], dict)

    def test_cascade_event_fields_exist_in_emergency_postmortem(self):
        """Emergency postmortem 반환값에 CascadeEvent 관련 필드가 존재."""
        from baldur.services.event_bus import _generate_emergency_postmortem_data

        session_data = {
            "session_id": "recovery-test-002",
            "namespace": "global",
            "trigger_level": "LEVEL_2",
            "started_at": "2026-01-28T10:00:00Z",
            "completed_at": "2026-01-28T10:15:00Z",
            "duration_seconds": 900,
            "steps_executed": 3,
            "total_steps": 4,
        }
        event_bus_history = []
        snapshot = {}

        result = _generate_emergency_postmortem_data(
            session_data=session_data,
            event_bus_history=event_bus_history,
            snapshot=snapshot,
        )

        assert "cascade_event_id" in result
        assert "causation_chain" in result
        assert "evidence_hash" in result

    @mock.patch.dict(
        os.environ,
        {
            "BALDUR_POSTMORTEM_POSTMORTEM_BASE_URL": "https://pm.internal",
            "CB_DASHBOARD_URL": "https://grafana.internal/d/emergency",
        },
    )
    def test_emergency_postmortem_deep_links_populated(self):
        """환경변수 설정 시 Emergency postmortem의 deep_links에 URL 생성됨."""
        from baldur.services.event_bus import _generate_emergency_postmortem_data
        from baldur_pro.services.postmortem.deep_links import (
            reset_postmortem_deep_link_builder,
        )

        reset_postmortem_deep_link_builder()

        session_data = {
            "session_id": "recovery-test-003",
            "namespace": "production",
            "trigger_level": "LEVEL_3",
            "started_at": "2026-01-28T10:00:00Z",
            "completed_at": "2026-01-28T10:45:00Z",
            "duration_seconds": 2700,
            "steps_executed": 4,
            "total_steps": 4,
        }

        result = _generate_emergency_postmortem_data(
            session_data=session_data,
            event_bus_history=[],
            snapshot={},
        )

        deep_links = result["deep_links"]
        assert deep_links.get("postmortem_url") is not None
        assert "EMERGENCY-production" in deep_links["postmortem_url"]
        assert deep_links.get("dashboard_url") is not None

    @mock.patch("baldur.audit.cascade_auditor.get_cascade_event_auditor")
    def test_emergency_cascade_event_found(self, mock_get_auditor):
        """Emergency 관련 CascadeEvent가 있을 때 연결됨."""
        from baldur.audit.cascade_event import (
            CascadeEffect,
            CascadeEvent,
            CascadeTrigger,
        )
        from baldur.services.event_bus import _generate_emergency_postmortem_data

        mock_event = CascadeEvent(
            id="cascade-emergency-001",
            trigger=CascadeTrigger(
                trigger_type="EMERGENCY_LEVEL_CHANGED",
                event_id="evt-emg-001",
                details={"level": 3, "previous_level": 0},
            ),
            effects=[
                CascadeEffect(
                    event_id="evt-emg-002",
                    action_type="GOVERNANCE_STRICT",
                    caused_by="evt-emg-001",
                    success=True,
                ),
            ],
            namespace="seoul",
            timestamp="2026-01-28T10:00:00Z",
            current_hash="sha256:emergency123",
        )

        mock_auditor = mock.MagicMock()
        mock_auditor.get_recent_events.return_value = [mock_event]
        mock_get_auditor.return_value = mock_auditor

        session_data = {
            "session_id": "recovery-test-004",
            "namespace": "seoul",
            "trigger_level": "LEVEL_3",
            "started_at": "2026-01-28T10:00:00Z",
            "completed_at": "2026-01-28T10:30:00Z",
            "duration_seconds": 1800,
            "steps_executed": 4,
            "total_steps": 4,
        }

        result = _generate_emergency_postmortem_data(
            session_data=session_data,
            event_bus_history=[],
            snapshot={},
        )

        assert result["cascade_event_id"] == "cascade-emergency-001"
        assert result["causation_chain"] == ["evt-emg-001", "evt-emg-002"]
        assert result["evidence_hash"] == "sha256:emergency123"

    @mock.patch("baldur.audit.cascade_auditor.get_cascade_event_auditor")
    def test_emergency_cascade_event_not_found_no_error(self, mock_get_auditor):
        """CascadeEvent가 없어도 에러 없이 진행됨."""
        mock_auditor = mock.MagicMock()
        mock_auditor.get_recent_events.return_value = []
        mock_get_auditor.return_value = mock_auditor

        from baldur.services.event_bus import _generate_emergency_postmortem_data

        session_data = {
            "session_id": "recovery-test-005",
            "namespace": "test",
            "trigger_level": "LEVEL_1",
            "started_at": "2026-01-28T10:00:00Z",
            "completed_at": "2026-01-28T10:05:00Z",
            "duration_seconds": 300,
            "steps_executed": 2,
            "total_steps": 4,
        }

        result = _generate_emergency_postmortem_data(
            session_data=session_data,
            event_bus_history=[],
            snapshot={},
        )

        assert result["cascade_event_id"] is None
        assert result["causation_chain"] == []
        assert result["evidence_hash"] is None


class TestEmergencyPostmortemRequiredFields:
    """Emergency Postmortem 필수 필드 테스트."""

    def test_emergency_postmortem_has_recovery_type(self):
        """Emergency postmortem은 recovery_type=emergency를 가짐."""
        from baldur.services.event_bus import _generate_emergency_postmortem_data

        session_data = {
            "session_id": "recovery-fields-001",
            "namespace": "test",
            "trigger_level": "LEVEL_2",
            "started_at": "2026-01-28T10:00:00Z",
            "completed_at": "2026-01-28T10:10:00Z",
            "duration_seconds": 600,
            "steps_executed": 3,
            "total_steps": 4,
        }

        result = _generate_emergency_postmortem_data(
            session_data=session_data,
            event_bus_history=[],
            snapshot={},
        )

        assert result["recovery_type"] == "emergency"
        assert result["namespace"] == "test"
        assert result["trigger_level"] == "LEVEL_2"

    def test_emergency_postmortem_incident_id_format(self):
        """Emergency postmortem의 incident_id 형식 확인."""
        from baldur.services.event_bus import _generate_emergency_postmortem_data

        session_data = {
            "session_id": "recovery-format-001",
            "namespace": "seoul",
            "trigger_level": "LEVEL_3",
            "started_at": "2026-01-28T10:00:00Z",
            "completed_at": "2026-01-28T10:30:00Z",
            "duration_seconds": 1800,
            "steps_executed": 4,
            "total_steps": 4,
        }

        result = _generate_emergency_postmortem_data(
            session_data=session_data,
            event_bus_history=[],
            snapshot={},
        )

        # EMERGENCY-{namespace}-{timestamp} 형식
        assert result["incident_id"].startswith("EMERGENCY-seoul-")
