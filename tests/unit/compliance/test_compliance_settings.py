"""
ComplianceSettings Unit Tests.

Tests for ComplianceSettings Pydantic model: defaults, validators, boundaries.

Test Categories:
    A. Contract: Default values from doc §7.1, §7.2, §349
    B. Behavior: Boundary analysis, validators, singleton lifecycle
    C. Contract (349): ComplianceStandard Enum sync, exemption field defaults
    D. Behavior (349): Domain exemption validators, exemption_reasons coverage warning
"""

import pytest
from pydantic import ValidationError

from baldur.settings.compliance import (
    ComplianceSettings,
    get_compliance_settings,
    reset_compliance_settings,
)

# =============================================================================
# A. Contract Tests
# =============================================================================


class TestComplianceSettingsDefaultsContract:
    """ComplianceSettings default values from doc §7.1."""

    def test_enabled_default(self):
        """enabled default is False (Dormant tier per V1_LAUNCH_MANIFEST)."""
        assert ComplianceSettings().enabled is False

    def test_standards_default(self):
        """standards default is ['DORA_2025']."""
        assert ComplianceSettings().standards == ["DORA_2025"]

    def test_auto_check_schedule_hour_default(self):
        """auto_check_schedule_hour default is 7."""
        assert ComplianceSettings().auto_check_schedule_hour == 7

    def test_report_retention_days_default(self):
        """report_retention_days default is 365."""
        assert ComplianceSettings().report_retention_days == 365

    def test_critical_failure_threshold_default(self):
        """critical_failure_threshold default is 3."""
        assert ComplianceSettings().critical_failure_threshold == 3

    def test_evidence_collection_enabled_default(self):
        """evidence_collection_enabled default is False (Dormant tier)."""
        assert ComplianceSettings().evidence_collection_enabled is False


class TestComplianceSettingsPerStandardContract:
    """Per-standard settings defaults from doc §7.2."""

    def test_dora_resilience_window_days_default(self):
        """dora_resilience_window_days default is 30."""
        assert ComplianceSettings().dora_resilience_window_days == 30

    def test_dora_min_experiments_default(self):
        """dora_min_experiments default is 4."""
        assert ComplianceSettings().dora_min_experiments == 4

    def test_resilience_test_history_limit_default(self):
        """resilience_test_history_limit default is 100."""
        assert ComplianceSettings().resilience_test_history_limit == 100

    def test_pci_log_retention_days_default(self):
        """pci_log_retention_days default is 365."""
        assert ComplianceSettings().pci_log_retention_days == 365

    def test_hipaa_retention_days_default(self):
        """hipaa_retention_days default is 2190 (6 years)."""
        assert ComplianceSettings().hipaa_retention_days == 2190


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestComplianceSettingsBoundaryBehavior:
    """ComplianceSettings field boundary validation."""

    def test_auto_check_schedule_hour_min_boundary(self):
        """auto_check_schedule_hour minimum is 0 (midnight)."""
        s = ComplianceSettings(auto_check_schedule_hour=0)
        assert s.auto_check_schedule_hour == 0

    def test_auto_check_schedule_hour_max_boundary(self):
        """auto_check_schedule_hour maximum is 23."""
        s = ComplianceSettings(auto_check_schedule_hour=23)
        assert s.auto_check_schedule_hour == 23

    def test_auto_check_schedule_hour_above_max_raises(self):
        """auto_check_schedule_hour above 23 raises ValidationError."""
        with pytest.raises(ValidationError):
            ComplianceSettings(auto_check_schedule_hour=24)

    def test_report_retention_days_below_min_raises(self):
        """report_retention_days below 30 raises ValidationError."""
        with pytest.raises(ValidationError):
            ComplianceSettings(report_retention_days=29)

    def test_report_retention_days_at_min_passes(self):
        """report_retention_days at 30 is valid."""
        s = ComplianceSettings(report_retention_days=30)
        assert s.report_retention_days == 30

    def test_critical_failure_threshold_min_boundary(self):
        """critical_failure_threshold minimum is 1."""
        s = ComplianceSettings(critical_failure_threshold=1)
        assert s.critical_failure_threshold == 1

    def test_critical_failure_threshold_below_min_raises(self):
        """critical_failure_threshold below 1 raises ValidationError."""
        with pytest.raises(ValidationError):
            ComplianceSettings(critical_failure_threshold=0)

    def test_hipaa_retention_days_below_min_raises(self):
        """hipaa_retention_days below 365 raises ValidationError."""
        with pytest.raises(ValidationError):
            ComplianceSettings(hipaa_retention_days=364)


class TestComplianceSettingsValidatorBehavior:
    """ComplianceSettings standards validator behavior."""

    def test_valid_standards_accepted(self):
        """Known standards are accepted."""
        s = ComplianceSettings(standards=["DORA_2025", "SOC2", "PCI_DSS"])
        assert len(s.standards) == 3

    def test_unknown_standard_raises_validation_error(self):
        """Unknown standard name raises ValidationError."""
        with pytest.raises(ValidationError, match="Unknown compliance standard"):
            ComplianceSettings(standards=["INVALID_STANDARD"])

    def test_empty_standards_list_accepted(self):
        """Empty standards list is valid (no checks configured)."""
        s = ComplianceSettings(standards=[])
        assert s.standards == []

    def test_all_six_standards_accepted(self):
        """All 6 real standards are accepted together."""
        all_standards = [
            "DORA_2025",
            "SOC2",
            "PCI_DSS",
            "HIPAA",
            "GDPR",
            "ISO27001",
        ]
        s = ComplianceSettings(standards=all_standards)
        assert len(s.standards) == 6


class TestComplianceSettingsSingletonBehavior:
    """ComplianceSettings singleton get/reset lifecycle."""

    def setup_method(self):
        reset_compliance_settings()

    def teardown_method(self):
        reset_compliance_settings()

    def test_get_returns_cached_instance(self):
        """get_compliance_settings returns same instance."""
        first = get_compliance_settings()
        second = get_compliance_settings()
        assert first is second

    def test_reset_allows_new_instance(self):
        """reset_compliance_settings clears cache."""
        first = get_compliance_settings()
        reset_compliance_settings()
        second = get_compliance_settings()
        assert first is not second


# =============================================================================
# C. Contract Tests (349): ComplianceStandard Enum Sync and Exemption Defaults
# =============================================================================


class TestValidStandardsEnumSyncContract:
    """Validators derive valid standards from ComplianceStandard Enum (doc §349 §2)."""

    def test_validators_accept_all_enum_values(self):
        """All ComplianceStandard Enum values are accepted by validate_standards."""
        from baldur.models.compliance import ComplianceStandard

        all_values = [e.value for e in ComplianceStandard]
        s = ComplianceSettings(standards=all_values)
        assert len(s.standards) == len(ComplianceStandard)

    def test_enum_contains_expected_standards(self):
        """ComplianceStandard Enum has all expected members."""
        from baldur.models.compliance import ComplianceStandard

        expected = {
            "DORA_2025",
            "SOC2",
            "PCI_DSS",
            "HIPAA",
            "GDPR",
            "ISO27001",
            "CUSTOM",
        }
        actual = {e.value for e in ComplianceStandard}
        assert actual == expected


class TestExemptionFieldDefaultsContract:
    """Exemption field defaults from doc §349 §2."""

    def test_excluded_standards_by_domain_default_empty_dict(self):
        """excluded_standards_by_domain defaults to empty dict."""
        assert ComplianceSettings().excluded_standards_by_domain == {}

    def test_exemption_reasons_default_empty_dict(self):
        """exemption_reasons defaults to empty dict."""
        assert ComplianceSettings().exemption_reasons == {}


# =============================================================================
# D. Behavior Tests (349): Domain Exemption Validators
# =============================================================================


class TestExcludedStandardsValidatorBehavior:
    """validate_excluded_standards validator behavior from doc §349 §2."""

    def test_valid_domain_and_standards_accepted(self):
        """Valid lowercase_snake_case domain with known standards passes."""
        with pytest.warns(UserWarning, match="exemption_reason"):
            s = ComplianceSettings(
                excluded_standards_by_domain={"internal_admin": ["GDPR", "HIPAA"]},
            )
        assert s.excluded_standards_by_domain == {"internal_admin": ["GDPR", "HIPAA"]}

    def test_empty_dict_accepted(self):
        """Empty excluded_standards_by_domain is valid."""
        s = ComplianceSettings(excluded_standards_by_domain={})
        assert s.excluded_standards_by_domain == {}

    def test_multiple_domains_accepted(self):
        """Multiple domains with different standards accepted."""
        with pytest.warns(UserWarning, match="exemption_reason"):
            s = ComplianceSettings(
                excluded_standards_by_domain={
                    "internal_admin": ["GDPR"],
                    "analytics": ["PCI_DSS", "HIPAA"],
                },
            )
        assert len(s.excluded_standards_by_domain) == 2

    def test_uppercase_domain_raises_validation_error(self):
        """Domain with uppercase letters raises ValidationError."""
        with pytest.raises(ValidationError, match="must be lowercase_snake_case"):
            ComplianceSettings(
                excluded_standards_by_domain={"InternalAdmin": ["GDPR"]},
            )

    def test_domain_starting_with_number_raises_validation_error(self):
        """Domain starting with number raises ValidationError."""
        with pytest.raises(ValidationError, match="must be lowercase_snake_case"):
            ComplianceSettings(
                excluded_standards_by_domain={"1admin": ["GDPR"]},
            )

    def test_domain_with_hyphen_raises_validation_error(self):
        """Domain with hyphen raises ValidationError (only underscore allowed)."""
        with pytest.raises(ValidationError, match="must be lowercase_snake_case"):
            ComplianceSettings(
                excluded_standards_by_domain={"internal-admin": ["GDPR"]},
            )

    def test_unknown_standard_in_exclusion_raises_validation_error(self):
        """Unknown standard name in exclusion raises ValidationError."""
        with pytest.raises(ValidationError, match="Unknown standard"):
            ComplianceSettings(
                excluded_standards_by_domain={"admin": ["FAKE_STANDARD"]},
            )

    def test_domain_with_numbers_and_underscores_accepted(self):
        """Domain like 'region2_admin' is valid lowercase_snake_case."""
        with pytest.warns(UserWarning, match="exemption_reason"):
            s = ComplianceSettings(
                excluded_standards_by_domain={"region2_admin": ["GDPR"]},
            )
        assert "region2_admin" in s.excluded_standards_by_domain


class TestExemptionReasonsCoverageValidatorBehavior:
    """validate_exemption_reasons_coverage model_validator from doc §349 §2."""

    def test_full_coverage_no_warning(self):
        """No warning when all exclusions have reasons."""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            ComplianceSettings(
                excluded_standards_by_domain={"admin": ["GDPR"]},
                exemption_reasons={"admin": {"GDPR": "No EU PII processed"}},
            )
            exemption_warnings = [x for x in w if "exemption_reason" in str(x.message)]
            assert len(exemption_warnings) == 0

    def test_missing_reason_emits_warning(self):
        """Warning emitted when exclusion has no corresponding reason."""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            ComplianceSettings(
                excluded_standards_by_domain={"admin": ["GDPR", "HIPAA"]},
                exemption_reasons={"admin": {"GDPR": "No EU PII"}},
            )
            exemption_warnings = [x for x in w if "exemption_reason" in str(x.message)]
            assert len(exemption_warnings) == 1
            assert "domain='admin'" in str(exemption_warnings[0].message)
            assert "standard='HIPAA'" in str(exemption_warnings[0].message)

    def test_no_exclusions_no_warning(self):
        """No warning when excluded_standards_by_domain is empty."""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            ComplianceSettings()
            exemption_warnings = [x for x in w if "exemption_reason" in str(x.message)]
            assert len(exemption_warnings) == 0

    def test_multiple_missing_reasons_emit_multiple_warnings(self):
        """Each missing reason emits a separate warning."""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            ComplianceSettings(
                excluded_standards_by_domain={
                    "admin": ["GDPR", "HIPAA"],
                    "analytics": ["PCI_DSS"],
                },
                exemption_reasons={},
            )
            exemption_warnings = [x for x in w if "exemption_reason" in str(x.message)]
            assert len(exemption_warnings) == 3
