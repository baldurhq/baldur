"""
PropagationSettings Unit Tests.

"""


class TestPropagationSettings:
    """PropagationSettings 테스트."""

    def setup_method(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.settings.propagation import reset_propagation_settings

        reset_propagation_settings()

    def teardown_method(self):
        """각 테스트 후에 싱글톤 리셋."""
        from baldur.settings.propagation import reset_propagation_settings

        reset_propagation_settings()

    def test_default_values(self):
        """기본값 테스트."""
        from baldur.settings.propagation import PropagationSettings

        settings = PropagationSettings()

        assert settings.tier1_max_latency_ms == 1000
        assert settings.tier2_max_latency_ms == 30000
        assert settings.auto_start_listener is False
        assert settings.retry_count == 3
        assert settings.retry_delay_ms == 500
        assert settings.health_score_weight == 0.3
        assert settings.tier1_penalty_points == 5
        assert settings.tier2_penalty_points == 1

    def test_custom_values(self):
        """커스텀 값 테스트."""
        from baldur.settings.propagation import PropagationSettings

        settings = PropagationSettings(
            tier1_max_latency_ms=2000,
            tier2_max_latency_ms=60000,
            health_score_weight=0.5,
        )

        assert settings.tier1_max_latency_ms == 2000
        assert settings.tier2_max_latency_ms == 60000
        assert settings.health_score_weight == 0.5

    def test_validation_constraints(self):
        """값 범위 검증 테스트."""
        from baldur.settings.propagation import PropagationSettings

        # 정상 범위
        settings = PropagationSettings(
            tier1_max_latency_ms=100,  # min
            tier2_max_latency_ms=1000,  # min
            health_score_weight=0.0,  # min
        )

        assert settings.tier1_max_latency_ms == 100
        assert settings.tier2_max_latency_ms == 1000
        assert settings.health_score_weight == 0.0

    def test_singleton(self):
        """싱글톤 패턴 테스트."""
        from baldur.settings.propagation import (
            get_propagation_settings,
            reset_propagation_settings,
        )

        reset_propagation_settings()

        s1 = get_propagation_settings()
        s2 = get_propagation_settings()

        assert s1 is s2
