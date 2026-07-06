# Grafana Alert Webhook unit tests
"""
Grafana Alert Webhook handler unit tests.

Test targets:
1. Grafana severity -> NotificationPriority mapping
2. Grafana category -> NotificationCategory mapping
3. Metadata extraction from annotations
4. Webhook payload processing
"""

from unittest.mock import MagicMock

import pytest


class TestGrafanaSeverityMapping:
    """Grafana severity -> NotificationPriority mapping tests."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_critical_severity_maps_to_critical_priority(self):
        """critical severity -> CRITICAL priority"""
        from baldur.api.handlers.grafana_webhook import _map_severity_to_priority
        from baldur_pro.services.unified_notification import NotificationPriority

        result = _map_severity_to_priority("critical")
        assert result == NotificationPriority.CRITICAL

    def test_warning_severity_maps_to_high_priority(self):
        """warning severity -> HIGH priority"""
        from baldur.api.handlers.grafana_webhook import _map_severity_to_priority
        from baldur_pro.services.unified_notification import NotificationPriority

        result = _map_severity_to_priority("warning")
        assert result == NotificationPriority.HIGH

    def test_info_severity_maps_to_info_priority(self):
        """info severity -> INFO priority"""
        from baldur.api.handlers.grafana_webhook import _map_severity_to_priority
        from baldur_pro.services.unified_notification import NotificationPriority

        result = _map_severity_to_priority("info")
        assert result == NotificationPriority.INFO

    def test_unknown_severity_maps_to_medium_priority(self):
        """Unknown severity -> MEDIUM priority (default)"""
        from baldur.api.handlers.grafana_webhook import _map_severity_to_priority
        from baldur_pro.services.unified_notification import NotificationPriority

        result = _map_severity_to_priority("unknown")
        assert result == NotificationPriority.MEDIUM

    def test_case_insensitive_severity_mapping(self):
        """Case-insensitive mapping"""
        from baldur.api.handlers.grafana_webhook import _map_severity_to_priority
        from baldur_pro.services.unified_notification import NotificationPriority

        assert _map_severity_to_priority("CRITICAL") == NotificationPriority.CRITICAL
        assert _map_severity_to_priority("Critical") == NotificationPriority.CRITICAL
        assert _map_severity_to_priority("WARNING") == NotificationPriority.HIGH


class TestGrafanaCategoryMapping:
    """Grafana category -> NotificationCategory mapping tests."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_sla_category_maps_to_sla(self):
        """sla category -> SLA NotificationCategory"""
        from baldur.api.handlers.grafana_webhook import _map_category
        from baldur_pro.services.unified_notification import NotificationCategory

        result = _map_category("sla")
        assert result == NotificationCategory.SLA

    def test_circuit_breaker_category_maps_correctly(self):
        """circuit_breaker category -> CIRCUIT_BREAKER NotificationCategory"""
        from baldur.api.handlers.grafana_webhook import _map_category
        from baldur_pro.services.unified_notification import NotificationCategory

        result = _map_category("circuit_breaker")
        assert result == NotificationCategory.CIRCUIT_BREAKER

    def test_security_category_maps_correctly(self):
        """security category -> SECURITY NotificationCategory"""
        from baldur.api.handlers.grafana_webhook import _map_category
        from baldur_pro.services.unified_notification import NotificationCategory

        result = _map_category("security")
        assert result == NotificationCategory.SECURITY

    def test_unknown_category_maps_to_operations(self):
        """Unknown category -> OPERATIONS (default)"""
        from baldur.api.handlers.grafana_webhook import _map_category
        from baldur_pro.services.unified_notification import NotificationCategory

        result = _map_category("unknown_category")
        assert result == NotificationCategory.OPERATIONS


class TestMetadataExtraction:
    """Metadata extraction from annotations tests."""

    def test_extract_latency_metadata(self):
        """Latency-related metadata extraction"""
        from baldur.api.handlers.grafana_webhook import _extract_metadata

        annotations = {
            "current_latency_ms": "650",
            "threshold_ms": "500",
            "affected_service": "payment-service",
        }

        result = _extract_metadata(annotations)

        assert result["current_latency_ms"] == 650.0
        assert result["threshold_ms"] == 500.0
        assert result["affected_service"] == "payment-service"

    def test_extract_url_metadata(self):
        """URL-related metadata extraction"""
        from baldur.api.handlers.grafana_webhook import _extract_metadata

        annotations = {
            "runbook_url": "https://docs.example.com/runbooks/latency",
            "dashboard_url": "/d/unified-view/unified-view",
        }

        result = _extract_metadata(annotations)

        assert result["runbook_url"] == "https://docs.example.com/runbooks/latency"
        assert result["dashboard_url"] == "/d/unified-view/unified-view"

    def test_handle_invalid_numeric_values(self):
        """Invalid numeric value handling"""
        from baldur.api.handlers.grafana_webhook import _extract_metadata

        annotations = {
            "current_latency_ms": "not-a-number",
            "threshold_ms": None,
            "affected_service": "test-service",
        }

        result = _extract_metadata(annotations)

        # Invalid values are not extracted
        assert "current_latency_ms" not in result
        assert "threshold_ms" not in result
        assert result["affected_service"] == "test-service"

    def test_empty_annotations(self):
        """Empty annotations handling"""
        from baldur.api.handlers.grafana_webhook import _extract_metadata

        result = _extract_metadata({})
        assert result == {}


class TestTraceIdRegexPatterns:
    """trace_id regex 패턴 테스트 (datasource.yml 설정용)"""

    def test_w3c_32_char_trace_id_pattern(self):
        """W3C 32자리 hex trace_id 패턴"""
        import re

        pattern = r'"trace_id":"([a-f0-9]{32})"'
        test_cases = [
            ('"trace_id":"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"', True),
            ('"trace_id":"A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6"', False),  # 대문자
            ('"trace_id":"a1b2c3d4"', False),  # 짧음
        ]

        for test_str, should_match in test_cases:
            match = re.search(pattern, test_str)
            assert (match is not None) == should_match, (
                f"Pattern failed for: {test_str}"
            )

    def test_clustered_trace_id_pattern(self):
        """클러스터 prefix 포함 trace_id 패턴 (req-seop-a1b2c3d4)"""
        import re

        pattern = r"req-([a-z]{3,4})-([a-f0-9]{8})"
        test_cases = [
            ("req-seop-a1b2c3d4", True),  # Seoul Production
            ("req-tokp-f5e6d7c8", True),  # Tokyo Production
            ("req-seos-12345678", True),  # Seoul Staging
            ("req-ab-12345678", False),  # prefix 너무 짧음
            ("req-a1b2c3d4", False),  # prefix 없음
        ]

        for test_str, should_match in test_cases:
            match = re.search(pattern, test_str)
            assert (match is not None) == should_match, (
                f"Pattern failed for: {test_str}"
            )

    def test_simple_trace_id_pattern(self):
        """클러스터 prefix 없는 trace_id 패턴 (req-a1b2c3d4)"""
        import re

        pattern = r"req-([a-f0-9]{8})(?![a-f0-9])"
        test_cases = [
            ("req-a1b2c3d4", True),
            ("req-12345678", True),
            ("req-a1b2c3d4e5f6", False),  # 8자리 초과
            ("req-seop-a1b2c3d4", False),  # 클러스터 prefix 있음
        ]

        for test_str, should_match in test_cases:
            match = re.search(pattern, test_str)
            assert (match is not None) == should_match, (
                f"Pattern failed for: {test_str}"
            )


class TestNotificationPayloadCreation:
    """NotificationPayload 생성 테스트"""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_create_payload_from_grafana_alert(self):
        """Grafana Alert로부터 NotificationPayload 생성"""
        from baldur_pro.services.unified_notification import (
            NotificationCategory,
            NotificationPayload,
            NotificationPriority,
        )

        # NotificationPayload 직접 생성 테스트
        payload = NotificationPayload(
            title="SLA Critical: P95 Latency Exceeded",
            message="payment-service P95 latency 650ms > 500ms threshold",
            priority=NotificationPriority.HIGH,
            category=NotificationCategory.SLA,
            source="grafana_alerting",
            metadata={
                "current_latency_ms": 650,
                "threshold_ms": 500,
                "affected_service": "payment-service",
            },
            tags=["alert:LatencyP95SlaCritical", "severity:critical", "category:sla"],
            dedup_key="grafana_alert_LatencyP95SlaCritical_payment-service",
        )

        assert payload.title == "SLA Critical: P95 Latency Exceeded"
        assert payload.priority == NotificationPriority.HIGH
        assert payload.category == NotificationCategory.SLA
        assert payload.source == "grafana_alerting"
        assert payload.metadata["current_latency_ms"] == 650

    def test_payload_to_dict(self):
        """NotificationPayload의 to_dict() 메서드"""
        from baldur_pro.services.unified_notification import (
            NotificationCategory,
            NotificationPayload,
            NotificationPriority,
        )

        payload = NotificationPayload(
            title="Test Alert",
            message="Test message",
            priority=NotificationPriority.MEDIUM,
            category=NotificationCategory.OPERATIONS,
            source="test",
        )

        result = payload.to_dict()

        assert result["title"] == "Test Alert"
        assert result["message"] == "Test message"
        assert result["priority"] == "medium"
        assert result["category"] == "operations"
        assert result["source"] == "test"


class TestAlertProcessing:
    """Alert processing logic tests."""

    def test_firing_alert_triggers_notification(self):
        """Firing alert triggers notification"""
        pytest.importorskip("baldur_pro")
        from baldur.api.handlers.grafana_webhook import _process_single_alert
        from baldur_pro.services.unified_notification import NotificationResult

        # Mock NotificationManager
        mock_manager = MagicMock()
        mock_manager.notify.return_value = NotificationResult(
            success=True,
            channels_sent=["slack"],
        )

        alert = {
            "status": "firing",
            "labels": {
                "alertname": "TestAlert",
                "severity": "warning",
                "category": "sla",
            },
            "annotations": {
                "summary": "Test Summary",
                "description": "Test Description",
            },
        }

        _process_single_alert(alert, mock_manager)

        mock_manager.notify.assert_called_once()

    def test_resolved_alert_does_not_trigger_notification(self):
        """Resolved alert does not trigger notification"""
        from baldur.api.handlers.grafana_webhook import _process_single_alert

        mock_manager = MagicMock()

        alert = {
            "status": "resolved",
            "labels": {"alertname": "TestAlert"},
            "annotations": {},
        }

        _process_single_alert(alert, mock_manager)

        mock_manager.notify.assert_not_called()


class TestGrafanaWebhookNotificationUnavailableBehavior:
    """Notification module unavailable behavior for grafana_alert_webhook handler (392-V5a)."""

    @staticmethod
    def _make_request_ctx(json_body):
        """Create a RequestContext for handler testing."""
        from baldur.interfaces.web_framework import HttpMethod, RequestContext

        return RequestContext(
            method=HttpMethod.POST,
            path="/webhook/grafana/alert",
            json_body=json_body,
        )

    def test_returns_503_when_notification_import_fails(self):
        """Returns HTTP 503 when notification import fails."""
        import builtins
        from unittest.mock import patch

        from baldur.api.handlers.grafana_webhook import grafana_alert_webhook

        ctx = self._make_request_ctx(
            {"alerts": [{"status": "firing", "labels": {}, "annotations": {}}]}
        )

        original_import = builtins.__import__

        def import_mock(name, *args, **kwargs):
            if "unified_notification" in name:
                raise ImportError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=import_mock):
            response = grafana_alert_webhook(ctx)

        assert response.status_code == 503
        assert response.body["error"] == "Notification service unavailable"

    def test_returns_503_response_body_format(self):
        """503 response body contains error field with correct message."""
        import builtins
        from unittest.mock import patch

        from baldur.api.handlers.grafana_webhook import grafana_alert_webhook

        ctx = self._make_request_ctx({"alerts": [{"status": "firing"}]})

        original_import = builtins.__import__

        def import_mock(name, *args, **kwargs):
            if "unified_notification" in name:
                raise ImportError()
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=import_mock):
            response = grafana_alert_webhook(ctx)

        assert "error" in response.body
        assert isinstance(response.body["error"], str)

    def test_returns_503_when_specific_type_missing_from_module(self):
        """Returns 503 when module is importable but a specific type is missing.

        grafana_alert_webhook guard validates UnifiedNotificationManager import,
        so missing types should result in 503 fail-fast.
        """
        import builtins
        import types
        from unittest.mock import patch

        from baldur.api.handlers.grafana_webhook import grafana_alert_webhook

        ctx = self._make_request_ctx(
            {"alerts": [{"status": "firing", "labels": {}, "annotations": {}}]}
        )

        original_import = builtins.__import__

        # Given: module exists but UnifiedNotificationManager is missing
        fake_module = types.ModuleType("baldur_pro.services.unified_notification")
        fake_module.NotificationCategory = MagicMock()
        fake_module.NotificationPriority = MagicMock()
        fake_module.NotificationPayload = MagicMock()
        # UnifiedNotificationManager intentionally omitted

        def import_mock(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "baldur_pro.services.unified_notification" and fromlist:
                for attr in fromlist:
                    if not hasattr(fake_module, attr):
                        raise ImportError(
                            f"cannot import name '{attr}' from "
                            f"'baldur_pro.services.unified_notification'"
                        )
                return fake_module
            return original_import(name, globals, locals, fromlist, level)

        with patch.object(builtins, "__import__", side_effect=import_mock):
            response = grafana_alert_webhook(ctx)

        assert response.status_code == 503
        assert response.body["error"] == "Notification service unavailable"

    def test_processes_normally_when_notification_available(self):
        """Normal processing when notification module is available."""
        pytest.importorskip("baldur_pro")
        from baldur.api.handlers.grafana_webhook import grafana_alert_webhook

        ctx = self._make_request_ctx({"alerts": []})

        response = grafana_alert_webhook(ctx)

        assert response.status_code == 200
