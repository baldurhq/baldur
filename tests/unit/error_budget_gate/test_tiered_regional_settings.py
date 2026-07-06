"""
ErrorBudgetGateSettings 티어별/리전별 임계치 설정 테스트.

tier_thresholds, regional_thresholds 필드 및
get_thresholds_for_tier(), get_thresholds_for_region(), get_effective_thresholds() 동작 검증.
"""

from baldur.settings.error_budget_gate import ErrorBudgetGateSettings

# =============================================================================
# 계약 검증: 기본값
# =============================================================================


class TestTieredThresholdsDefaultContract:
    """티어별 임계치 기본값 계약 검증."""

    def test_tier_thresholds_disabled_by_default(self):
        """티어별 임계치는 기본적으로 비활성화."""
        settings = ErrorBudgetGateSettings()
        assert settings.tier_thresholds_enabled is False

    def test_critical_tier_default_thresholds(self):
        """critical 티어 기본 critical=15.0, warning=30.0."""
        settings = ErrorBudgetGateSettings()
        assert (
            settings.tier_thresholds["critical"]["critical_threshold_percent"] == 15.0
        )
        assert settings.tier_thresholds["critical"]["warning_threshold_percent"] == 30.0

    def test_standard_tier_default_thresholds(self):
        """standard 티어 기본 critical=10.0, warning=20.0."""
        settings = ErrorBudgetGateSettings()
        assert (
            settings.tier_thresholds["standard"]["critical_threshold_percent"] == 10.0
        )
        assert settings.tier_thresholds["standard"]["warning_threshold_percent"] == 20.0

    def test_non_essential_tier_default_thresholds(self):
        """non_essential 티어 기본 critical=5.0, warning=10.0."""
        settings = ErrorBudgetGateSettings()
        assert (
            settings.tier_thresholds["non_essential"]["critical_threshold_percent"]
            == 5.0
        )
        assert (
            settings.tier_thresholds["non_essential"]["warning_threshold_percent"]
            == 10.0
        )

    def test_three_tiers_defined(self):
        """기본 tier_thresholds에 critical/standard/non_essential 3개 정의."""
        settings = ErrorBudgetGateSettings()
        assert set(settings.tier_thresholds.keys()) == {
            "critical",
            "standard",
            "non_essential",
        }


class TestRegionalThresholdsDefaultContract:
    """리전별 임계치 기본값 계약 검증."""

    def test_regional_thresholds_disabled_by_default(self):
        """리전별 임계치는 기본적으로 비활성화."""
        settings = ErrorBudgetGateSettings()
        assert settings.regional_thresholds_enabled is False

    def test_regional_thresholds_empty_by_default(self):
        """리전별 임계치는 기본적으로 빈 딕셔너리."""
        settings = ErrorBudgetGateSettings()
        assert settings.regional_thresholds == {}


# =============================================================================
# 동작 검증: get_thresholds_for_tier()
# =============================================================================


class TestGetThresholdsForTierBehavior:
    """get_thresholds_for_tier() 동작 검증."""

    def test_returns_global_when_tier_disabled(self):
        """tier_thresholds_enabled=False이면 글로벌 임계치 반환."""
        settings = ErrorBudgetGateSettings(
            tier_thresholds_enabled=False,
            critical_threshold_percent=10.0,
            warning_threshold_percent=20.0,
        )
        critical, warning = settings.get_thresholds_for_tier("critical")
        assert critical == settings.critical_threshold_percent
        assert warning == settings.warning_threshold_percent

    def test_returns_tier_values_when_enabled(self):
        """tier_thresholds_enabled=True이면 해당 티어 임계치 반환."""
        settings = ErrorBudgetGateSettings(tier_thresholds_enabled=True)
        critical, warning = settings.get_thresholds_for_tier("critical")
        assert (
            critical
            == settings.tier_thresholds["critical"]["critical_threshold_percent"]
        )
        assert (
            warning == settings.tier_thresholds["critical"]["warning_threshold_percent"]
        )

    def test_unknown_tier_falls_back_to_global(self):
        """존재하지 않는 tier_id는 글로벌 임계치로 폴백."""
        settings = ErrorBudgetGateSettings(tier_thresholds_enabled=True)
        critical, warning = settings.get_thresholds_for_tier("unknown_tier")
        assert critical == settings.critical_threshold_percent
        assert warning == settings.warning_threshold_percent

    def test_partial_tier_config_uses_global_for_missing_key(self):
        """티어에 일부 키만 정의되면 나머지는 글로벌 값 사용."""
        settings = ErrorBudgetGateSettings(
            tier_thresholds_enabled=True,
            tier_thresholds={
                "custom": {"critical_threshold_percent": 25.0},
            },
        )
        critical, warning = settings.get_thresholds_for_tier("custom")
        assert critical == 25.0
        assert warning == settings.warning_threshold_percent


# =============================================================================
# 동작 검증: get_thresholds_for_region()
# =============================================================================


class TestGetThresholdsForRegionBehavior:
    """get_thresholds_for_region() 동작 검증."""

    def test_returns_global_when_region_disabled(self):
        """regional_thresholds_enabled=False이면 글로벌 임계치 반환."""
        settings = ErrorBudgetGateSettings(
            regional_thresholds_enabled=False,
            regional_thresholds={"seoul": {"critical_threshold_percent": 15.0}},
        )
        critical, warning = settings.get_thresholds_for_region("seoul")
        assert critical == settings.critical_threshold_percent
        assert warning == settings.warning_threshold_percent

    def test_returns_region_values_when_enabled(self):
        """regional_thresholds_enabled=True이면 해당 리전 임계치 반환."""
        settings = ErrorBudgetGateSettings(
            regional_thresholds_enabled=True,
            regional_thresholds={
                "seoul": {
                    "critical_threshold_percent": 15.0,
                    "warning_threshold_percent": 30.0,
                },
            },
        )
        critical, warning = settings.get_thresholds_for_region("seoul")
        assert critical == 15.0
        assert warning == 30.0

    def test_unknown_region_falls_back_to_global(self):
        """존재하지 않는 region은 글로벌 임계치로 폴백."""
        settings = ErrorBudgetGateSettings(
            regional_thresholds_enabled=True,
            regional_thresholds={"seoul": {"critical_threshold_percent": 15.0}},
        )
        critical, warning = settings.get_thresholds_for_region("unknown_region")
        assert critical == settings.critical_threshold_percent
        assert warning == settings.warning_threshold_percent


# =============================================================================
# 동작 검증: get_effective_thresholds()
# =============================================================================


class TestGetEffectiveThresholdsBehavior:
    """get_effective_thresholds() 우선순위 동작 검증."""

    def test_global_fallback_when_both_disabled(self):
        """티어/리전 모두 비활성화면 글로벌 반환."""
        settings = ErrorBudgetGateSettings(
            tier_thresholds_enabled=False,
            regional_thresholds_enabled=False,
        )
        critical, warning = settings.get_effective_thresholds(
            tier_id="critical", region="seoul"
        )
        assert critical == settings.critical_threshold_percent
        assert warning == settings.warning_threshold_percent

    def test_tier_used_when_only_tier_enabled(self):
        """티어만 활성화면 티어 임계치 사용."""
        settings = ErrorBudgetGateSettings(
            tier_thresholds_enabled=True,
            regional_thresholds_enabled=False,
        )
        critical, warning = settings.get_effective_thresholds(tier_id="critical")
        assert (
            critical
            == settings.tier_thresholds["critical"]["critical_threshold_percent"]
        )
        assert (
            warning == settings.tier_thresholds["critical"]["warning_threshold_percent"]
        )

    def test_region_overrides_tier(self):
        """리전과 티어 모두 활성화면 리전이 우선."""
        settings = ErrorBudgetGateSettings(
            tier_thresholds_enabled=True,
            regional_thresholds_enabled=True,
            regional_thresholds={
                "seoul": {
                    "critical_threshold_percent": 25.0,
                    "warning_threshold_percent": 40.0,
                },
            },
        )
        critical, warning = settings.get_effective_thresholds(
            tier_id="critical", region="seoul"
        )
        # 리전 오버라이드가 티어보다 우선
        assert critical == 25.0
        assert warning == 40.0

    def test_tier_used_when_region_not_found(self):
        """리전 활성화이나 해당 리전 설정 없으면 티어로 폴백."""
        settings = ErrorBudgetGateSettings(
            tier_thresholds_enabled=True,
            regional_thresholds_enabled=True,
            regional_thresholds={},
        )
        critical, warning = settings.get_effective_thresholds(
            tier_id="critical", region="unknown"
        )
        assert (
            critical
            == settings.tier_thresholds["critical"]["critical_threshold_percent"]
        )
        assert (
            warning == settings.tier_thresholds["critical"]["warning_threshold_percent"]
        )

    def test_none_tier_and_region_returns_global(self):
        """tier_id=None, region=None이면 글로벌 반환."""
        settings = ErrorBudgetGateSettings(
            tier_thresholds_enabled=True,
            regional_thresholds_enabled=True,
        )
        critical, warning = settings.get_effective_thresholds()
        assert critical == settings.critical_threshold_percent
        assert warning == settings.warning_threshold_percent


# =============================================================================
# 동작 검증: to_dict() / from_dict()
# =============================================================================


class TestSettingsSerializationBehavior:
    """설정 직렬화/역직렬화 동작 검증."""

    def test_to_dict_includes_tier_fields(self):
        """to_dict()에 tier_thresholds 관련 필드 포함."""
        settings = ErrorBudgetGateSettings()
        d = settings.to_dict()
        assert "tier_thresholds_enabled" in d
        assert "tier_thresholds" in d

    def test_to_dict_includes_regional_fields(self):
        """to_dict()에 regional_thresholds 관련 필드 포함."""
        settings = ErrorBudgetGateSettings()
        d = settings.to_dict()
        assert "regional_thresholds_enabled" in d
        assert "regional_thresholds" in d

    def test_from_dict_round_trip(self):
        """from_dict()로 설정 복원 가능."""
        original = ErrorBudgetGateSettings(
            tier_thresholds_enabled=True,
            regional_thresholds_enabled=True,
            regional_thresholds={"seoul": {"critical_threshold_percent": 15.0}},
        )
        restored = ErrorBudgetGateSettings.from_dict(original.to_dict())
        assert restored.tier_thresholds_enabled == original.tier_thresholds_enabled
        assert (
            restored.regional_thresholds_enabled == original.regional_thresholds_enabled
        )
        assert restored.regional_thresholds == original.regional_thresholds
