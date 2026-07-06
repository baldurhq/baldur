"""
Tiering Configuration Validator.

Validates tier configurations with Safe Boundary rules.
Prevents dangerous configurations that could break the system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from ipaddress import ip_address, ip_network

from baldur.core.serializable import SerializableMixin

from .enums import OverrideIdentifierType, TierMatchType
from .models import TierDefinition, TierMapping, TierOverride


@dataclass
class TierValidationResult(SerializableMixin):
    """Validation result for tier configuration."""

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class TierConfigValidator:
    """
    Tier configuration validator with Safe Boundary rules.

    Prevents dangerous configurations that could break the system.
    """

    # Safe Boundary Rules
    RULES = {
        "max_multiplier": 1.0,  # Multiplier cannot exceed 1.0
        "min_tiers": 1,  # At least 1 tier required
        "max_tiers": 10,  # Maximum 10 tiers
        "require_critical_tier": True,  # Critical tier is recommended
        "min_critical_multiplier": 0.1,  # Critical tier minimum 10% access
    }

    def validate_tiers(
        self, tier_definitions: list[TierDefinition]
    ) -> TierValidationResult:
        """
        Validate tier definitions.

        Args:
            tier_definitions: List of tier definitions

        Returns:
            TierValidationResult with errors and warnings
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Rule 1: Minimum tier count
        if len(tier_definitions) < self.RULES["min_tiers"]:
            errors.append(f"At least {self.RULES['min_tiers']} tier(s) required.")

        # Rule 2: Maximum tier count
        if len(tier_definitions) > self.RULES["max_tiers"]:
            errors.append(
                f"At most {self.RULES['max_tiers']} tiers are allowed. "
                f"(current: {len(tier_definitions)})"
            )

        # Check for duplicate IDs
        tier_ids = [t.id for t in tier_definitions]
        if len(tier_ids) != len(set(tier_ids)):
            errors.append("Duplicate tier IDs found.")

        for tier in tier_definitions:
            # Rule 3: Multiplier range
            if tier.multiplier < 0:
                errors.append(
                    f"Tier '{tier.id}': multiplier must be >= 0. "
                    f"(current: {tier.multiplier})"
                )
            if tier.multiplier > self.RULES["max_multiplier"]:
                errors.append(
                    f"Tier '{tier.id}': multiplier cannot exceed "
                    f"{self.RULES['max_multiplier']}. (current: {tier.multiplier})"
                )

        # Rule 4: Critical tier recommendation
        if self.RULES["require_critical_tier"]:
            critical_tiers = [t for t in tier_definitions if t.id == "critical"]
            if not critical_tiers:
                warnings.append(
                    "No 'critical' tier defined. "
                    "Core API protection during an emergency may be difficult."
                )
            elif critical_tiers[0].multiplier < self.RULES["min_critical_multiplier"]:
                warnings.append(
                    f"'critical' tier multiplier is too low. "
                    f"Minimum {self.RULES['min_critical_multiplier']} recommended. "
                    f"(current: {critical_tiers[0].multiplier})"
                )

        return TierValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    def validate_mappings(
        self, mappings: list[TierMapping], tier_ids: list[str]
    ) -> TierValidationResult:
        """
        Validate tier mappings.

        Args:
            mappings: List of tier mappings
            tier_ids: List of valid tier IDs

        Returns:
            TierValidationResult with errors and warnings
        """
        errors: list[str] = []
        warnings: list[str] = []

        for mapping in mappings:
            # Check tier exists
            if mapping.tier_id not in tier_ids:
                errors.append(
                    f"Mapping '{mapping.pattern}': references non-existent "
                    f"tier ID '{mapping.tier_id}'."
                )

            # Validate regex patterns
            if mapping.pattern_type == TierMatchType.REGEX:
                try:
                    re.compile(mapping.pattern)
                except re.error as e:
                    errors.append(f"Mapping '{mapping.pattern}': invalid regex - {e}")

        # Check for overlapping patterns (warning only)
        patterns = [m.pattern for m in mappings]
        if len(patterns) != len(set(patterns)):
            warnings.append("Duplicate patterns found. They are resolved by priority.")

        return TierValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    def validate_overrides(
        self, overrides: list[TierOverride], tier_ids: list[str]
    ) -> TierValidationResult:
        """
        Validate tier overrides.

        Args:
            overrides: List of tier overrides
            tier_ids: List of valid tier IDs

        Returns:
            TierValidationResult with errors and warnings
        """
        errors: list[str] = []
        warnings: list[str] = []

        for override in overrides:
            # Check tier exists
            if override.tier_id not in tier_ids:
                errors.append(
                    f"Override '{override.identifier}': references non-existent "
                    f"tier ID '{override.tier_id}'."
                )

            # Validate IP format
            if override.identifier_type == OverrideIdentifierType.IP:
                try:
                    if "/" in override.identifier:
                        ip_network(override.identifier, strict=False)
                    else:
                        ip_address(override.identifier)
                except ValueError as e:
                    errors.append(
                        f"Override '{override.identifier}': invalid IP format - {e}"
                    )

            # Check for expired overrides
            if override.is_expired():
                warnings.append(f"Override '{override.identifier}' has expired.")

        return TierValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    def validate_all(
        self,
        tiers: list[TierDefinition],
        mappings: list[TierMapping],
        overrides: list[TierOverride],
    ) -> TierValidationResult:
        """
        Validate all tier configurations.

        Args:
            tiers: Tier definitions
            mappings: Tier mappings
            overrides: Tier overrides

        Returns:
            Combined TierValidationResult
        """
        tier_result = self.validate_tiers(tiers)
        tier_ids = [t.id for t in tiers]

        mapping_result = self.validate_mappings(mappings, tier_ids)
        override_result = self.validate_overrides(overrides, tier_ids)

        return TierValidationResult(
            is_valid=(
                tier_result.is_valid
                and mapping_result.is_valid
                and override_result.is_valid
            ),
            errors=tier_result.errors + mapping_result.errors + override_result.errors,
            warnings=tier_result.warnings
            + mapping_result.warnings
            + override_result.warnings,
        )
