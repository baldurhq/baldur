"""
GlobalConfigPropagator Unit Tests.

"""

from unittest.mock import MagicMock


class TestConfigScope:
    """ConfigScope 테스트."""

    def test_config_scope_values(self):
        """ConfigScope enum 값 확인."""
        from baldur.services.config.propagator import ConfigScope

        assert ConfigScope.LOCAL.value == "local"
        assert ConfigScope.REGIONAL.value == "regional"
        assert ConfigScope.GLOBAL.value == "global"


class TestPropagationTier:
    """PropagationTier 테스트."""

    def test_propagation_tier_values(self):
        """PropagationTier enum 값 확인."""
        from baldur.services.config.propagator import PropagationTier

        assert PropagationTier.TIER_1_IMMEDIATE.value == "tier_1"
        assert PropagationTier.TIER_2_EVENTUAL.value == "tier_2"


class TestGlobalConfigChange:
    """GlobalConfigChange 테스트."""

    def test_basic_creation(self):
        """기본 생성 테스트."""
        from baldur.services.config.propagator import (
            ConfigScope,
            GlobalConfigChange,
            PropagationTier,
        )

        change = GlobalConfigChange(
            config_type="circuit_breaker",
            config_key="failure_threshold",
            new_value=10,
            previous_value=5,
            scope=ConfigScope.GLOBAL,
            tier=PropagationTier.TIER_1_IMMEDIATE,
            source_cluster="seoul-prod-01",
        )

        assert change.config_type == "circuit_breaker"
        assert change.config_key == "failure_threshold"
        assert change.new_value == 10
        assert change.previous_value == 5
        assert change.scope == ConfigScope.GLOBAL
        assert change.tier == PropagationTier.TIER_1_IMMEDIATE
        assert change.source_cluster == "seoul-prod-01"
        assert change.timestamp is not None

    def test_to_dict(self):
        """to_dict 변환 테스트."""
        from baldur.services.config.propagator import (
            ConfigScope,
            GlobalConfigChange,
            PropagationTier,
        )

        change = GlobalConfigChange(
            config_type="dlq",
            config_key="max_retries",
            new_value=5,
            previous_value=3,
            scope=ConfigScope.REGIONAL,
            tier=PropagationTier.TIER_2_EVENTUAL,
            source_cluster="tokyo-prod-01",
        )

        data = change.to_dict()

        assert data["config_type"] == "dlq"
        assert data["config_key"] == "max_retries"
        assert data["new_value"] == 5
        assert data["previous_value"] == 3
        assert data["scope"] == "regional"
        assert data["tier"] == "tier_2"
        assert data["source_cluster"] == "tokyo-prod-01"
        assert "timestamp" in data

    def test_from_dict(self):
        """from_dict 변환 테스트."""
        from baldur.services.config.propagator import (
            ConfigScope,
            GlobalConfigChange,
            PropagationTier,
        )

        data = {
            "config_type": "emergency",
            "config_key": "level",
            "new_value": 3,
            "previous_value": 0,
            "scope": "global",
            "tier": "tier_1",
            "source_cluster": "seoul-prod-01",
            "timestamp": "2026-01-19T10:00:00+00:00",
        }

        change = GlobalConfigChange.from_dict(data)

        assert change.config_type == "emergency"
        assert change.config_key == "level"
        assert change.new_value == 3
        assert change.scope == ConfigScope.GLOBAL
        assert change.tier == PropagationTier.TIER_1_IMMEDIATE


class TestGlobalConfigPropagator:
    """GlobalConfigPropagator 테스트."""

    def setup_method(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.core.cluster_identity import reset_cluster_identity
        from baldur.services.config.propagator import (
            reset_global_config_propagator,
        )

        reset_global_config_propagator()
        reset_cluster_identity()

    def teardown_method(self):
        """각 테스트 후에 싱글톤 리셋."""
        from baldur.core.cluster_identity import reset_cluster_identity
        from baldur.services.config.propagator import (
            reset_global_config_propagator,
        )

        reset_global_config_propagator()
        reset_cluster_identity()

    def test_propagate_local_scope_skipped(self):
        """LOCAL scope는 전파 건너뜀."""
        from baldur.services.config.propagator import (
            ConfigScope,
            GlobalConfigChange,
            GlobalConfigPropagator,
            PropagationTier,
        )

        propagator = GlobalConfigPropagator(redis_client=None)

        change = GlobalConfigChange(
            config_type="test",
            config_key="key",
            new_value=1,
            previous_value=0,
            scope=ConfigScope.LOCAL,
            tier=PropagationTier.TIER_2_EVENTUAL,
            source_cluster="test",
        )

        # LOCAL scope는 True 반환 (전파 불필요)
        result = propagator.propagate(change)
        assert result is True

    def test_propagate_global_scope(self):
        """GLOBAL scope 전파 테스트."""
        from baldur.services.config.propagator import (
            ConfigScope,
            GlobalConfigChange,
            GlobalConfigPropagator,
            PropagationTier,
        )

        mock_redis = MagicMock()
        mock_redis.publish.return_value = 2  # 2 subscribers

        propagator = GlobalConfigPropagator(redis_client=mock_redis)

        change = GlobalConfigChange(
            config_type="circuit_breaker",
            config_key="failure_threshold",
            new_value=10,
            previous_value=5,
            scope=ConfigScope.GLOBAL,
            tier=PropagationTier.TIER_1_IMMEDIATE,
            source_cluster="seoul-prod-01",
        )

        result = propagator.propagate(change)

        assert result is True
        mock_redis.publish.assert_called_once()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "baldur:global:config"

    def test_subscribe_and_handlers(self):
        """구독 및 핸들러 등록 테스트."""
        from baldur.services.config.propagator import (
            GlobalConfigChange,
            GlobalConfigPropagator,
        )

        propagator = GlobalConfigPropagator(redis_client=None)

        received_changes = []

        def handler(change: GlobalConfigChange):
            received_changes.append(change)

        propagator.subscribe("circuit_breaker", handler)

        # 내부적으로 핸들러가 등록되었는지 확인
        assert "circuit_breaker" in propagator._handlers
        assert handler in propagator._handlers["circuit_breaker"]

    def test_unsubscribe(self):
        """구독 해제 테스트."""
        from baldur.services.config.propagator import GlobalConfigPropagator

        propagator = GlobalConfigPropagator(redis_client=None)

        def handler(change):
            pass

        propagator.subscribe("test", handler)
        assert handler in propagator._handlers["test"]

        propagator.unsubscribe("test", handler)
        assert handler not in propagator._handlers["test"]

    def test_singleton(self):
        """싱글톤 패턴 테스트."""
        from baldur.services.config.propagator import (
            get_global_config_propagator,
            reset_global_config_propagator,
        )

        reset_global_config_propagator()

        p1 = get_global_config_propagator()
        p2 = get_global_config_propagator()

        assert p1 is p2
