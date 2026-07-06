"""
Masking Constant Consolidation — Contract Tests.

Contract verification for §6.5: DEFAULT_SENSITIVE_KEYS is the single
source of truth for both audit/masking.py and forensic_audit_bridge.py.
"""

from __future__ import annotations

from baldur.audit.masking import DEFAULT_SENSITIVE_KEYS

# =============================================================================
# A. DEFAULT_SENSITIVE_KEYS Contract — §6.5
# =============================================================================


class TestDefaultSensitiveKeysContract:
    """DEFAULT_SENSITIVE_KEYS canonical source contract (doc 346 §6.5)."""

    def test_contains_core_security_keys(self):
        """DEFAULT_SENSITIVE_KEYS includes all core security field names."""
        required = {
            "password",
            "secret",
            "token",
            "api_key",
            "credential",
            "private_key",
        }
        assert required.issubset(set(DEFAULT_SENSITIVE_KEYS))

    def test_contains_pii_keys(self):
        """DEFAULT_SENSITIVE_KEYS includes PII-related field names."""
        pii = {"credit_card", "ssn", "social_security"}
        assert pii.issubset(set(DEFAULT_SENSITIVE_KEYS))

    def test_contains_auth_keys(self):
        """DEFAULT_SENSITIVE_KEYS includes authentication-related field names."""
        auth = {"authorization", "auth", "apikey"}
        assert auth.issubset(set(DEFAULT_SENSITIVE_KEYS))

    def test_total_count_matches_spec(self):
        """DEFAULT_SENSITIVE_KEYS contains exactly 22 entries (doc 505 D6)."""
        assert len(DEFAULT_SENSITIVE_KEYS) == 22

    def test_contains_pci_identity_cloud_keys(self):
        """DEFAULT_SENSITIVE_KEYS includes PCI/identity/cloud keys (doc 505 D6)."""
        expanded = {
            "card_number",
            "cvv",
            "cvc",
            "iban",
            "account_number",
            "routing_number",
            "passport",
            "driver_license",
            "tax_id",
            "access_key",
        }
        assert expanded.issubset(set(DEFAULT_SENSITIVE_KEYS))
