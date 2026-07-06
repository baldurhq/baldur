"""
Unit Tests for SLA Timer Policy

Tests for SLA threshold configuration, breach detection,
and domain-specific timeout policies.

Risk Covered: R-012 (SLA breach undetected)
Compliance: SOC 2 (Availability & Monitoring)
"""

from datetime import timedelta

import pytest

from baldur.settings import SLASettings as SLAConfig

# Shopping domain-specific SLA thresholds for testing
SHOPPING_SLA_THRESHOLDS = {
    "payment": 1,  # 1 hour - strictest (revenue impact)
    "inventory": 2,  # 2 hours - stock accuracy
    "point": 4,  # 4 hours - customer satisfaction
    "webhook": 8,  # 8 hours - integration reliability
    "notification": 24,  # 24 hours - informational only
}


def get_shopping_sla_config() -> SLAConfig:
    """Create an SLAConfig with shopping-specific thresholds."""
    return SLAConfig(
        default_hours=24,
        thresholds_by_domain=SHOPPING_SLA_THRESHOLDS.copy(),
    )


@pytest.mark.tier1
class TestSLAConfigDefaults:
    """
    Tests for SLAConfig default configuration.

    Purpose:
        Verify that SLAConfig can be configured with domain-specific thresholds.

    Compliance:
        SOC 2 CC7.2 (Monitoring) - SLA tracking
    """

    def test_default_hours_is_24(self):
        """
        Purpose:
            Verify default SLA for unregistered domains is 24 hours.
        """
        config = SLAConfig()
        assert config.default_hours == 24, "Default SLA should be 24 hours."

    def test_empty_thresholds_by_default(self):
        """
        Purpose:
            Verify thresholds_by_domain is empty by default (framework-agnostic).
        """
        config = SLAConfig()
        assert config.thresholds_by_domain == {}, "Default thresholds should be empty."

    def test_custom_thresholds_can_be_configured(self):
        """
        Purpose:
            Verify custom thresholds can be set via constructor.
        """
        config = get_shopping_sla_config()

        assert config.thresholds_by_domain["payment"] == 1
        assert config.thresholds_by_domain["point"] == 4
        assert config.thresholds_by_domain["inventory"] == 2
        assert config.thresholds_by_domain["webhook"] == 8
        assert config.thresholds_by_domain["notification"] == 24


@pytest.mark.tier1
class TestSLAThresholdRetrieval:
    """
    Tests for retrieving SLA thresholds by domain.

    Purpose:
        Validate correct threshold lookup for each domain.
    """

    def test_get_threshold_returns_timedelta(self):
        """
        Purpose:
            Verify threshold retrieval returns timedelta object.
        """
        config = get_shopping_sla_config()
        result = config.get_threshold("payment")

        assert isinstance(result, timedelta), (
            f"Expected timedelta, got {type(result).__name__}."
        )

    def test_get_threshold_payment(self):
        """
        Purpose:
            Verify payment threshold is 1 hour timedelta.
        """
        config = get_shopping_sla_config()
        result = config.get_threshold("payment")

        assert result == timedelta(hours=1), (
            f"Payment threshold incorrect: expected 1 hour, got {result}."
        )

    def test_get_threshold_point(self):
        """
        Purpose:
            Verify point threshold is 4 hours timedelta.
        """
        config = get_shopping_sla_config()
        result = config.get_threshold("point")

        assert result == timedelta(hours=4), (
            f"Point threshold incorrect: expected 4 hours, got {result}."
        )

    def test_get_threshold_inventory(self):
        """
        Purpose:
            Verify inventory threshold is 2 hours timedelta.
        """
        config = get_shopping_sla_config()
        result = config.get_threshold("inventory")

        assert result == timedelta(hours=2), (
            f"Inventory threshold incorrect: expected 2 hours, got {result}."
        )

    def test_get_threshold_webhook(self):
        """
        Purpose:
            Verify webhook threshold is 8 hours timedelta.
        """
        config = get_shopping_sla_config()
        result = config.get_threshold("webhook")

        assert result == timedelta(hours=8), (
            f"Webhook threshold incorrect: expected 8 hours, got {result}."
        )

    def test_get_threshold_notification(self):
        """
        Purpose:
            Verify notification threshold is 24 hours timedelta.
        """
        config = get_shopping_sla_config()
        result = config.get_threshold("notification")

        assert result == timedelta(hours=24), (
            f"Notification threshold incorrect: expected 24 hours, got {result}."
        )

    def test_get_threshold_unknown_domain_returns_default(self):
        """
        Purpose:
            Verify unknown domains get default threshold.
        """
        config = get_shopping_sla_config()
        result = config.get_threshold("foobar")

        assert result == timedelta(hours=24), (
            f"Unknown domain should return default: expected 24 hours, got {result}."
        )

    def test_get_threshold_case_insensitive(self):
        """
        Purpose:
            Verify domain lookup is case-insensitive.
        """
        config = get_shopping_sla_config()

        assert config.get_threshold("Payment") == timedelta(hours=1)
        assert config.get_threshold("PAYMENT") == timedelta(hours=1)
        assert config.get_threshold("payment") == timedelta(hours=1)


@pytest.mark.tier1
class TestSLAThresholdAllDomains:
    """
    Tests for retrieving all thresholds at once.

    Purpose:
        Validate bulk threshold retrieval.
    """

    def test_get_all_thresholds_returns_dict(self):
        """
        Purpose:
            Verify all thresholds method returns dict.
        """
        config = get_shopping_sla_config()
        result = config.get_all_thresholds()

        assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}."

    def test_get_all_thresholds_contains_all_domains(self):
        """
        Purpose:
            Verify all known domains are included.
        """
        config = get_shopping_sla_config()
        result = config.get_all_thresholds()

        expected_domains = {"payment", "point", "inventory", "webhook", "notification"}
        actual_domains = set(result.keys())

        assert expected_domains == actual_domains, (
            f"Missing domains: {expected_domains - actual_domains}. "
            f"Unexpected domains: {actual_domains - expected_domains}."
        )

    def test_get_all_thresholds_values_are_timedeltas(self):
        """
        Purpose:
            Verify all values are timedelta objects.
        """
        config = get_shopping_sla_config()
        result = config.get_all_thresholds()

        for domain, td in result.items():
            assert isinstance(td, timedelta), (
                f"Domain '{domain}' has non-timedelta value: {type(td).__name__}."
            )

    def test_get_all_thresholds_values_match_individual(self):
        """
        Purpose:
            Verify bulk retrieval matches individual lookups.
        """
        config = get_shopping_sla_config()
        all_thresholds = config.get_all_thresholds()

        for domain, expected in all_thresholds.items():
            individual = config.get_threshold(domain)
            assert individual == expected, (
                f"Mismatch for domain '{domain}': "
                f"bulk={expected}, individual={individual}."
            )


@pytest.mark.tier1
class TestSLAThresholdPriority:
    """
    Tests for SLA priority ordering.

    Purpose:
        Validate that critical domains have stricter SLAs.
    """

    def test_payment_is_strictest(self):
        """
        Purpose:
            Verify payment has the shortest SLA (highest priority).
        """
        config = get_shopping_sla_config()
        all_thresholds = config.get_all_thresholds()

        payment_sla = all_thresholds["payment"]

        for domain, sla in all_thresholds.items():
            if domain != "payment":
                assert payment_sla < sla, (
                    f"Payment SLA ({payment_sla}) should be stricter than "
                    f"{domain} SLA ({sla})."
                )

    def test_sla_ordering_matches_business_criticality(self):
        """
        Purpose:
            Verify SLA ordering matches business priority.

        Expected Order (strictest to most lenient):
            1. payment (1h) - revenue impact
            2. inventory (2h) - stock accuracy
            3. point (4h) - customer satisfaction
            4. webhook (8h) - integration reliability
            5. notification (24h) - informational only
        """
        config = get_shopping_sla_config()
        all_thresholds = config.get_all_thresholds()

        expected_order = [
            ("payment", 1),
            ("inventory", 2),
            ("point", 4),
            ("webhook", 8),
            ("notification", 24),
        ]

        for domain, expected_hours in expected_order:
            actual = all_thresholds[domain]
            expected = timedelta(hours=expected_hours)
            assert actual == expected, (
                f"Domain '{domain}' SLA mismatch: expected {expected}, got {actual}."
            )


@pytest.mark.tier1
class TestSLAConfigModifiability:
    """
    Tests for SLAConfig modifiability.

    Purpose:
        Verify that SLAConfig allows runtime configuration while recommending immutability.
    """

    def test_sla_config_is_dataclass(self):
        """
        Purpose:
            Verify SLAConfig is a dataclass that can be instantiated.
        """
        config = SLAConfig()
        assert hasattr(config, "default_hours")
        assert hasattr(config, "thresholds_by_domain")
        assert hasattr(config, "get_threshold")
        assert hasattr(config, "get_all_thresholds")

    def test_thresholds_can_be_updated_at_runtime(self):
        """
        Purpose:
            Verify thresholds can be added/updated at runtime (for adapter configuration).
        """
        config = SLAConfig()

        # Add custom thresholds
        config.thresholds_by_domain["custom_domain"] = 12

        result = config.get_threshold("custom_domain")
        assert result == timedelta(hours=12)

    def test_empty_config_returns_default_in_get_all(self):
        """
        Purpose:
            Verify empty config returns default entry in get_all_thresholds.
        """
        config = SLAConfig()
        result = config.get_all_thresholds()

        assert "default" in result
        assert result["default"] == timedelta(hours=24)
