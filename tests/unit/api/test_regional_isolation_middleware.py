"""
317: CellTaggingMiddleware._check_regional_isolation 단위 테스트.

테스트 대상:
- 설정 비활성화 시 None 반환
- 리전 격리 활성 시 503 JsonResponse 반환
- 리전 정상 시 None 반환
- ImportError 시 Fail-Open (None)
- 일반 예외 시 Fail-Open (None)
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

import django

django.setup()

from unittest.mock import MagicMock, patch

from django.test import override_settings

from baldur.api.django.cell.middleware import CellTaggingMiddleware

# =============================================================================
# Behavior: _check_regional_isolation 동작 검증
# =============================================================================


class TestCheckRegionalIsolationBehavior:
    """317: _check_regional_isolation 동작 검증."""

    def test_disabled_setting_returns_none(self):
        """BALDUR_REGIONAL_ISOLATION_ENABLED=False 시 None 반환."""
        mock_request = MagicMock()

        with override_settings(BALDUR_REGIONAL_ISOLATION_ENABLED=False):
            result = CellTaggingMiddleware._check_regional_isolation(mock_request)

        assert result is None

    def test_missing_setting_returns_none(self):
        """BALDUR_REGIONAL_ISOLATION_ENABLED 없으면 getattr default로 None."""
        mock_request = MagicMock()
        result = CellTaggingMiddleware._check_regional_isolation(mock_request)
        assert result is None

    def test_isolated_region_returns_503(self):
        """격리된 리전에서 503 JsonResponse 반환."""
        mock_request = MagicMock()

        mock_gate = MagicMock()
        mock_gate.is_current_region_isolated.return_value = (
            True,
            "failover_in_progress",
        )

        with (
            override_settings(BALDUR_REGIONAL_ISOLATION_ENABLED=True),
            patch(
                "baldur.services.isolation.regional_gate.get_regional_isolation_gate",
                return_value=mock_gate,
            ),
        ):
            result = CellTaggingMiddleware._check_regional_isolation(mock_request)

        assert result is not None
        assert result.status_code == 503

    def test_non_isolated_region_returns_none(self):
        """정상 리전에서 None 반환."""
        mock_request = MagicMock()

        mock_gate = MagicMock()
        mock_gate.is_current_region_isolated.return_value = (False, "")

        with (
            override_settings(BALDUR_REGIONAL_ISOLATION_ENABLED=True),
            patch(
                "baldur.services.isolation.regional_gate.get_regional_isolation_gate",
                return_value=mock_gate,
            ),
        ):
            result = CellTaggingMiddleware._check_regional_isolation(mock_request)

        assert result is None

    def test_import_error_returns_none(self):
        """regional_gate import 실패 시 Fail-Open (None)."""
        mock_request = MagicMock()

        with (
            override_settings(BALDUR_REGIONAL_ISOLATION_ENABLED=True),
            patch(
                "baldur.services.isolation.regional_gate.get_regional_isolation_gate",
                side_effect=ImportError("no module"),
            ),
        ):
            result = CellTaggingMiddleware._check_regional_isolation(mock_request)

        assert result is None

    def test_runtime_error_returns_none(self):
        """gate 호출 예외 시 Fail-Open (None)."""
        mock_request = MagicMock()

        mock_gate = MagicMock()
        mock_gate.is_current_region_isolated.side_effect = RuntimeError("gate broken")

        with (
            override_settings(BALDUR_REGIONAL_ISOLATION_ENABLED=True),
            patch(
                "baldur.services.isolation.regional_gate.get_regional_isolation_gate",
                return_value=mock_gate,
            ),
        ):
            result = CellTaggingMiddleware._check_regional_isolation(mock_request)

        assert result is None


# =============================================================================
# Contract: 503 응답 본문 구조
# =============================================================================


class TestRegionalIsolationResponseContract:
    """317: 격리 응답 본문 계약값 검증."""

    def test_response_body_contains_required_fields(self):
        """503 응답에 error, reason, detail 필드 포함."""
        mock_request = MagicMock()

        mock_gate = MagicMock()
        mock_gate.is_current_region_isolated.return_value = (True, "test_reason")

        with (
            override_settings(BALDUR_REGIONAL_ISOLATION_ENABLED=True),
            patch(
                "baldur.services.isolation.regional_gate.get_regional_isolation_gate",
                return_value=mock_gate,
            ),
        ):
            result = CellTaggingMiddleware._check_regional_isolation(mock_request)

        body = json.loads(result.content)
        assert body["error"] == "service_unavailable"
        assert body["reason"] == "regional_isolation"
        assert body["detail"] == "test_reason"
