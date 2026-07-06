"""
Tests for ProviderRegistry strategy 등록/조회 — Correlation Engine 전략 관리.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: 미등록 전략 조회 시 AdapterNotFoundError
- Behavior: register/get 라운드트립, reset 후 격리

참조 소스:
- factory.py (ProviderRegistry.register_correlation_strategy,
  register_root_cause_strategy, register_graph_build_strategy,
  get_correlation_strategy, get_root_cause_strategy, get_graph_build_strategy)
"""

from __future__ import annotations

import pytest

from baldur.core.exceptions import AdapterNotFoundError
from baldur.factory import ProviderRegistry

# =============================================================================
# Fixtures (이 파일 전용)
# =============================================================================


@pytest.fixture(autouse=True)
def _isolated_registry():
    """테스트 전후로 ProviderRegistry를 초기화하여 격리한다."""
    ProviderRegistry.reset()
    yield
    ProviderRegistry.reset()


class _DummyCorrelationStrategy:
    """테스트용 Correlation 전략 더미 클래스."""

    pass


class _DummyRootCauseStrategy:
    """테스트용 Root Cause 전략 더미 클래스."""

    pass


class _DummyGraphBuildStrategy:
    """테스트용 Graph Build 전략 더미 클래스."""

    pass


# =============================================================================
# Correlation Strategy 계약 검증
# =============================================================================


class TestCorrelationStrategyRegistryContract:
    """미등록 전략 조회 시 AdapterNotFoundError 발생 계약."""

    def test_unknown_correlation_strategy_raises_adapter_not_found_error(self):
        """등록되지 않은 correlation 전략 조회 시 AdapterNotFoundError."""
        with pytest.raises(AdapterNotFoundError, match="correlation_strategy"):
            ProviderRegistry.get_correlation_strategy("nonexistent")

    def test_unknown_root_cause_strategy_raises_adapter_not_found_error(self):
        """등록되지 않은 root cause 전략 조회 시 AdapterNotFoundError."""
        with pytest.raises(AdapterNotFoundError, match="root_cause_strategy"):
            ProviderRegistry.get_root_cause_strategy("nonexistent")

    def test_unknown_graph_build_strategy_raises_adapter_not_found_error(self):
        """등록되지 않은 graph build 전략 조회 시 AdapterNotFoundError."""
        with pytest.raises(AdapterNotFoundError, match="graph_build_strategy"):
            ProviderRegistry.get_graph_build_strategy("nonexistent")


# =============================================================================
# Strategy 등록/조회 동작 검증
# =============================================================================


class TestStrategyRegistryBehavior:
    """register → get 라운드트립 동작 검증."""

    def test_register_and_get_correlation_strategy(self):
        """correlation 전략 등록 후 조회하면 동일 클래스를 반환한다."""
        ProviderRegistry.register_correlation_strategy(
            "zscore", _DummyCorrelationStrategy
        )

        result = ProviderRegistry.get_correlation_strategy("zscore")
        assert result is _DummyCorrelationStrategy

    def test_register_and_get_root_cause_strategy(self):
        """root cause 전략 등록 후 조회하면 동일 클래스를 반환한다."""
        ProviderRegistry.register_root_cause_strategy("ranker", _DummyRootCauseStrategy)

        result = ProviderRegistry.get_root_cause_strategy("ranker")
        assert result is _DummyRootCauseStrategy

    def test_register_and_get_graph_build_strategy(self):
        """graph build 전략 등록 후 조회하면 동일 클래스를 반환한다."""
        ProviderRegistry.register_graph_build_strategy("dag", _DummyGraphBuildStrategy)

        result = ProviderRegistry.get_graph_build_strategy("dag")
        assert result is _DummyGraphBuildStrategy

    def test_overwrite_registration(self):
        """동일 이름으로 재등록하면 최신 클래스로 대체된다."""
        ProviderRegistry.register_correlation_strategy(
            "test", _DummyCorrelationStrategy
        )
        ProviderRegistry.register_correlation_strategy("test", _DummyRootCauseStrategy)

        result = ProviderRegistry.get_correlation_strategy("test")
        assert result is _DummyRootCauseStrategy

    def test_reset_clears_all_strategies(self):
        """reset() 후 등록된 전략이 모두 제거된다."""
        ProviderRegistry.register_correlation_strategy(
            "test_c", _DummyCorrelationStrategy
        )
        ProviderRegistry.register_root_cause_strategy("test_r", _DummyRootCauseStrategy)
        ProviderRegistry.register_graph_build_strategy(
            "test_g", _DummyGraphBuildStrategy
        )

        ProviderRegistry.reset()

        with pytest.raises(AdapterNotFoundError):
            ProviderRegistry.get_correlation_strategy("test_c")
        with pytest.raises(AdapterNotFoundError):
            ProviderRegistry.get_root_cause_strategy("test_r")
        with pytest.raises(AdapterNotFoundError):
            ProviderRegistry.get_graph_build_strategy("test_g")

    def test_multiple_strategies_independent(self):
        """correlation, root_cause, graph_build 전략은 서로 독립적이다."""
        ProviderRegistry.register_correlation_strategy(
            "alpha", _DummyCorrelationStrategy
        )
        ProviderRegistry.register_root_cause_strategy("alpha", _DummyRootCauseStrategy)
        ProviderRegistry.register_graph_build_strategy(
            "alpha", _DummyGraphBuildStrategy
        )

        assert (
            ProviderRegistry.get_correlation_strategy("alpha")
            is _DummyCorrelationStrategy
        )
        assert (
            ProviderRegistry.get_root_cause_strategy("alpha") is _DummyRootCauseStrategy
        )
        assert (
            ProviderRegistry.get_graph_build_strategy("alpha")
            is _DummyGraphBuildStrategy
        )
