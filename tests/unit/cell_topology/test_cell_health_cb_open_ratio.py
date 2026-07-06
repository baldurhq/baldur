"""
CellHealthAggregator._get_cb_open_ratio() 단위 테스트.

테스트 대상: services/cell_topology/health.py _get_cb_open_ratio()
- Composite Key 기반 Cell별 CB OPEN 비율 조회
- 수동 제어(Manual Override) CB도 감지 가능
- Cell별 물리적으로 분리된 CB만 집계
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.core.cb_namespace import (
    make_cell_scoped_cb_name,
)
from baldur.services.cell_topology.health import CellHealthAggregator


def _make_mock_settings():
    """CellHealthAggregator 초기화에 필요한 최소 설정 mock."""
    settings = MagicMock()
    settings.metrics_enabled = False
    settings.evacuation_enabled = False
    settings.health_check_interval_seconds = 10
    settings.prometheus_url = None
    settings.enabled = True
    return settings


class TestCellHealthCbOpenRatioBehavior:
    """_get_cb_open_ratio Composite Key 기반 CB OPEN 비율 계산 동작 검증."""

    def _build_aggregator(self):
        """테스트용 CellHealthAggregator 생성."""
        return CellHealthAggregator(settings=_make_mock_settings())

    def test_returns_zero_when_no_cb_for_cell(self):
        """해당 Cell에 CB가 없으면 0.0을 반환한다."""
        aggregator = self._build_aggregator()
        mock_service = MagicMock()
        mock_service.get_all_states.return_value = []

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=mock_service,
        ):
            result = aggregator._get_cb_open_ratio("cell-1")
        assert result == 0.0

    def test_returns_correct_ratio_for_one_open_out_of_two(self):
        """Cell에 CB 2개 중 1개가 OPEN이면 0.5를 반환한다."""
        aggregator = self._build_aggregator()

        states = [
            {
                "service_name": make_cell_scoped_cb_name("svc-a", "cell-3"),
                "state": "open",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-b", "cell-3"),
                "state": "closed",
            },
        ]
        mock_service = MagicMock()
        mock_service.get_all_states.return_value = states

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=mock_service,
        ):
            result = aggregator._get_cb_open_ratio("cell-3")
        assert result == pytest.approx(0.5)

    def test_ignores_cbs_from_other_cells(self):
        """다른 Cell의 CB는 비율 계산에 포함되지 않는다."""
        aggregator = self._build_aggregator()

        states = [
            {
                "service_name": make_cell_scoped_cb_name("svc-a", "cell-1"),
                "state": "open",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-b", "cell-2"),
                "state": "open",
            },
        ]
        mock_service = MagicMock()
        mock_service.get_all_states.return_value = states

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=mock_service,
        ):
            result = aggregator._get_cb_open_ratio("cell-1")
        # cell-1에는 svc-a 1개, OPEN 1개이므로 1.0
        assert result == pytest.approx(1.0)

    def test_ignores_legacy_keys_without_cell_id(self):
        """레거시 키(cell_id 없음)는 집계에서 제외된다."""
        aggregator = self._build_aggregator()

        states = [
            {"service_name": "legacy_svc", "state": "open"},  # 레거시 키
            {
                "service_name": make_cell_scoped_cb_name("svc-a", "cell-5"),
                "state": "closed",
            },
        ]
        mock_service = MagicMock()
        mock_service.get_all_states.return_value = states

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=mock_service,
        ):
            result = aggregator._get_cb_open_ratio("cell-5")
        # cell-5에는 svc-a 1개, CLOSED이므로 0.0
        assert result == pytest.approx(0.0)

    def test_all_open_returns_one(self):
        """Cell의 모든 CB가 OPEN이면 1.0을 반환한다."""
        aggregator = self._build_aggregator()

        states = [
            {
                "service_name": make_cell_scoped_cb_name("svc-a", "cell-9"),
                "state": "open",
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-b", "cell-9"),
                "state": "open",
            },
        ]
        mock_service = MagicMock()
        mock_service.get_all_states.return_value = states

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=mock_service,
        ):
            result = aggregator._get_cb_open_ratio("cell-9")
        assert result == pytest.approx(1.0)

    def test_returns_zero_on_exception(self):
        """예외 발생 시 0.0을 반환한다."""
        aggregator = self._build_aggregator()

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            side_effect=RuntimeError("boom"),
        ):
            result = aggregator._get_cb_open_ratio("cell-1")
        assert result == 0.0

    def test_detects_manually_controlled_open_cb(self):
        """수동 제어(Manual Override)로 OPEN된 CB도 감지한다."""
        aggregator = self._build_aggregator()

        states = [
            {
                "service_name": make_cell_scoped_cb_name("svc-a", "cell-4"),
                "state": "open",
                "manually_controlled": True,
            },
            {
                "service_name": make_cell_scoped_cb_name("svc-b", "cell-4"),
                "state": "closed",
                "manually_controlled": False,
            },
        ]
        mock_service = MagicMock()
        mock_service.get_all_states.return_value = states

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=mock_service,
        ):
            result = aggregator._get_cb_open_ratio("cell-4")
        assert result == pytest.approx(0.5)
