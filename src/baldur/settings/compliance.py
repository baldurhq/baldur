"""
Compliance Settings - Pydantic v2.

Single Source of Truth for compliance service configuration.

Environment Variables:
    BALDUR_COMPLIANCE_ENABLED=true
    BALDUR_COMPLIANCE_STANDARDS='["DORA_2025"]'
    BALDUR_COMPLIANCE_AUTO_CHECK_SCHEDULE_HOUR=7
    BALDUR_COMPLIANCE_REPORT_RETENTION_DAYS=365
    BALDUR_COMPLIANCE_CRITICAL_FAILURE_THRESHOLD=3
    BALDUR_COMPLIANCE_EVIDENCE_COLLECTION_ENABLED=true

    # Per-Standard Settings
    BALDUR_COMPLIANCE_DORA_RESILIENCE_WINDOW_DAYS=30
    BALDUR_COMPLIANCE_DORA_MIN_EXPERIMENTS=4
    BALDUR_COMPLIANCE_RESILIENCE_TEST_HISTORY_LIMIT=100
    BALDUR_COMPLIANCE_PCI_LOG_RETENTION_DAYS=365
    BALDUR_COMPLIANCE_HIPAA_RETENTION_DAYS=2190

    # Domain Exemption Settings
    BALDUR_COMPLIANCE_EXCLUDED_STANDARDS_BY_DOMAIN='{"internal_admin": ["GDPR", "HIPAA"]}'
    BALDUR_COMPLIANCE_EXEMPTION_REASONS='{"internal_admin": {"GDPR": "No EU PII processed"}}'
"""

import re
import warnings
from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import MediumCount


class ComplianceSettings(BaseSettings):
    """
    Compliance service configuration with validation.

    Controls which regulatory standards are active, scheduling,
    and per-standard thresholds.
    """

    model_config = make_settings_config("BALDUR_COMPLIANCE_")

    # ==========================================================================
    # Core Settings
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable compliance service",
    )
    standards: list[str] = Field(
        default_factory=lambda: ["DORA_2025"],
        description="List of compliance standards to enforce",
    )
    auto_check_schedule_hour: int = Field(
        default=7,
        ge=0,
        le=23,
        description="Hour of day for daily automatic compliance check (UTC)",
    )
    report_retention_days: int = Field(
        default=365,
        ge=30,
        le=3650,
        description="Report retention period in days",
    )
    critical_failure_threshold: MediumCount = Field(
        default=3,
        description="Number of AUTO failures to trigger critical alert",
    )
    evidence_collection_enabled: bool = Field(
        default=False,
        description="Enable EVIDENCE type check evidence collection",
    )

    # ==========================================================================
    # Domain Exemption Settings
    # ==========================================================================
    excluded_standards_by_domain: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Domain-level standard exclusions. Keys must be lowercase_snake_case.",
    )

    exemption_reasons: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Justification text per domain/standard pair for audit trail.",
    )

    # ==========================================================================
    # Shared Resilience Testing Settings
    # ==========================================================================
    dora_resilience_window_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="DORA-012 resilience testing verification window (days)",
    )
    dora_min_experiments: MediumCount = Field(
        default=4,
        description="DORA-012 minimum chaos experiments required in window",
    )
    resilience_test_history_limit: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Maximum chaos experiment history entries to query (shared across all standards)",
    )

    # ==========================================================================
    # PCI-DSS Per-Standard Settings
    # ==========================================================================
    pci_log_retention_days: int = Field(
        default=365,
        ge=90,
        le=3650,
        description="PCI-007 audit log retention verification (days)",
    )

    # ==========================================================================
    # HIPAA Per-Standard Settings
    # ==========================================================================
    hipaa_retention_days: int = Field(
        default=2190,
        ge=365,
        le=7300,
        description="HIPAA-009 retention verification (days, 6 years = 2190)",
    )

    @field_validator("standards")
    @classmethod
    def validate_standards(cls, v: list[str]) -> list[str]:
        """Validate that all standards are recognized."""
        from baldur.models.compliance import ComplianceStandard

        valid = {e.value for e in ComplianceStandard}
        for std in v:
            if std not in valid:
                raise ValueError(
                    f"Unknown compliance standard '{std}'. "
                    f"Valid options: {sorted(valid)}"
                )
        return v

    @field_validator("excluded_standards_by_domain")
    @classmethod
    def validate_excluded_standards(
        cls, v: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        """Enforce lowercase_snake_case for domain names and valid standard names."""
        from baldur.models.compliance import ComplianceStandard

        valid = {e.value for e in ComplianceStandard}
        for domain, standards in v.items():
            if not re.match(r"^[a-z][a-z0-9_]*$", domain):
                raise ValueError(f"Domain '{domain}' must be lowercase_snake_case")
            for std in standards:
                if std not in valid:
                    raise ValueError(
                        f"Unknown standard '{std}' in domain '{domain}'. "
                        f"Valid: {sorted(valid)}"
                    )
        return v

    @model_validator(mode="after")
    def validate_exemption_reasons_coverage(self) -> Self:
        """Warn if excluded standards lack a corresponding exemption reason."""
        for domain, standards in self.excluded_standards_by_domain.items():
            reasons = self.exemption_reasons.get(domain, {})
            for std in standards:
                if std not in reasons:
                    warnings.warn(
                        f"No exemption_reason for domain='{domain}', standard='{std}'. "
                        "SOC2/ISO auditors require documented justification.",
                        stacklevel=2,
                    )
        return self


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_compliance_settings() -> ComplianceSettings:
    from baldur.settings.root import get_config

    return get_config().services_group.compliance


def reset_compliance_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["compliance"]
    except KeyError:
        pass


__all__ = [
    "ComplianceSettings",
    "get_compliance_settings",
    "reset_compliance_settings",
]
