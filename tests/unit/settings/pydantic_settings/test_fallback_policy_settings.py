"""
FallbackPolicy enum and BaldurSettings.fallback_policy tests (commit 0b59f932).

Tests for:
- FallbackPolicy enum values and str inheritance
- BaldurSettings.fallback_policy default and boundary validation

Test Categories:
    A. Contract: Enum member values, default field value
    B. Behavior: Enum from string, settings field usage
"""

import pytest

from baldur.settings.root import BaldurSettings, FallbackPolicy

# =============================================================================
# A. Contract Tests
# =============================================================================


class TestFallbackPolicyContract:
    """Verify FallbackPolicy enum member values and structure."""

    def test_fallback_policy_allow_value(self):
        """FallbackPolicy.ALLOW == 'allow'."""
        assert FallbackPolicy.ALLOW == "allow"
        assert FallbackPolicy.ALLOW.value == "allow"

    def test_fallback_policy_warn_and_allow_value(self):
        """FallbackPolicy.WARN_AND_ALLOW == 'warn'."""
        assert FallbackPolicy.WARN_AND_ALLOW == "warn"
        assert FallbackPolicy.WARN_AND_ALLOW.value == "warn"

    def test_fallback_policy_fail_fast_value(self):
        """FallbackPolicy.FAIL_FAST == 'fail_fast'."""
        assert FallbackPolicy.FAIL_FAST == "fail_fast"
        assert FallbackPolicy.FAIL_FAST.value == "fail_fast"

    def test_fallback_policy_has_exactly_three_members(self):
        """FallbackPolicy has exactly 3 members."""
        assert len(FallbackPolicy) == 3

    def test_fallback_policy_is_str_enum(self):
        """FallbackPolicy inherits from str."""
        assert issubclass(FallbackPolicy, str)

    def test_settings_fallback_policy_default_is_allow(self):
        """BaldurSettings.fallback_policy default is ALLOW."""
        settings = BaldurSettings()
        assert settings.fallback_policy == FallbackPolicy.ALLOW

    @pytest.mark.parametrize(
        ("env_value", "marked_set"),
        [(None, False), ("allow", True), ("warn", True)],
        ids=["unset", "explicit_allow", "explicit_warn"],
    )
    def test_settings_fallback_policy_is_marked_set_only_when_supplied(
        self, monkeypatch, env_value, marked_set
    ):
        """801 D4 derives production's WARN_AND_ALLOW from this mark: an unset
        policy must read as unset although its value equals the default, and
        an explicit ``allow`` must read as the operator's choice."""
        if env_value is None:
            monkeypatch.delenv("FALLBACK_POLICY", raising=False)
        else:
            monkeypatch.setenv("FALLBACK_POLICY", env_value)

        settings = BaldurSettings()

        assert ("fallback_policy" in settings.model_fields_set) is marked_set


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestFallbackPolicyBehavior:
    """Verify FallbackPolicy usage in settings."""

    def test_fallback_policy_from_string_allow(self):
        """FallbackPolicy('allow') == FallbackPolicy.ALLOW."""
        assert FallbackPolicy("allow") == FallbackPolicy.ALLOW

    def test_fallback_policy_from_string_warn(self):
        """FallbackPolicy('warn') == FallbackPolicy.WARN_AND_ALLOW."""
        assert FallbackPolicy("warn") == FallbackPolicy.WARN_AND_ALLOW

    def test_fallback_policy_from_string_fail_fast(self):
        """FallbackPolicy('fail_fast') == FallbackPolicy.FAIL_FAST."""
        assert FallbackPolicy("fail_fast") == FallbackPolicy.FAIL_FAST

    def test_invalid_fallback_policy_raises_value_error(self):
        """Invalid string raises ValueError."""
        with pytest.raises(ValueError):
            FallbackPolicy("invalid_policy")

    def test_settings_accepts_env_var_for_fallback_policy(self, monkeypatch):
        """Settings can be configured via FALLBACK_POLICY env var."""
        monkeypatch.setenv("FALLBACK_POLICY", "fail_fast")
        settings = BaldurSettings()
        assert settings.fallback_policy == FallbackPolicy.FAIL_FAST

    def test_fallback_policy_string_comparison(self):
        """FallbackPolicy members compare equal to their string values."""
        assert FallbackPolicy.ALLOW == "allow"
        assert FallbackPolicy.WARN_AND_ALLOW == "warn"
        assert FallbackPolicy.FAIL_FAST == "fail_fast"
