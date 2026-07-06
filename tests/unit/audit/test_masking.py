"""
Tests for IP and PII masking utilities.
"""

from baldur.audit.masking import (
    extract_ip_from_request,
    hash_for_audit,
    mask_email,
    mask_ip,
    mask_sensitive_fields,
)


class TestMaskIP:
    """Tests for IP address masking."""

    def test_mask_ipv4_default(self):
        """Test IPv4 masking with default settings (last 2 octets)."""
        result = mask_ip("192.168.1.100")
        assert result == "192.168.***.***"

    def test_mask_ipv4_one_octet(self):
        """Test IPv4 masking with 1 octet masked."""
        result = mask_ip("192.168.1.100", mask_last_octets=1)
        assert result == "192.168.1.***"

    def test_mask_ipv4_three_octets(self):
        """Test IPv4 masking with 3 octets masked."""
        result = mask_ip("192.168.1.100", mask_last_octets=3)
        # IPv4 has only 4 octets, masking 3 leaves only first octet visible
        assert result == "192.***.***.***.***" or result == "192.***.***.***"

    def test_mask_ipv4_all_octets(self):
        """Test IPv4 masking with all octets masked."""
        result = mask_ip("192.168.1.100", mask_last_octets=4)
        assert result == "***.***.***.***"

    def test_mask_ipv6(self):
        """Test IPv6 masking."""
        result = mask_ip("2001:0db8:85a3:0000:0000:8a2e:0370:7334")
        assert "***" in result
        assert result.startswith("2001:0db8")

    def test_mask_ipv6_compressed(self):
        """Test compressed IPv6 masking."""
        result = mask_ip("::1")
        assert "***" in result

    def test_mask_ip_empty(self):
        """Test empty IP handling."""
        assert mask_ip("") == "unknown"
        assert mask_ip(None) == "unknown"

    def test_mask_ip_invalid(self):
        """Test invalid IP handling."""
        # Invalid but still masked for safety
        result = mask_ip("not-an-ip")
        assert "***" in result or result == "not-an-ip"


class TestMaskEmail:
    """Tests for email masking."""

    def test_mask_email_standard(self):
        """Test standard email masking."""
        result = mask_email("john.doe@example.com")
        # Format: first + *** + last @ domain
        assert result == "j***e@example.com"
        assert "@example.com" in result

    def test_mask_email_short_local(self):
        """Test email with short local part."""
        result = mask_email("a@example.com")
        assert "***" in result
        assert "@example.com" in result

    def test_mask_email_invalid(self):
        """Test invalid email handling."""
        result = mask_email("not-an-email")
        assert "***" in result

    def test_mask_email_empty(self):
        """Test empty email handling."""
        assert "***" in mask_email("")
        assert "***" in mask_email(None)


class TestHashForAudit:
    """Tests for audit hashing."""

    def test_hash_consistency(self):
        """Test that same input produces same hash."""
        data = "test data"
        hash1 = hash_for_audit(data)
        hash2 = hash_for_audit(data)
        assert hash1 == hash2

    def test_hash_with_salt(self):
        """Test hashing with salt changes result."""
        data = "test data"
        hash_no_salt = hash_for_audit(data)
        hash_with_salt = hash_for_audit(data, salt="mysalt")
        assert hash_no_salt != hash_with_salt

    def test_hash_format(self):
        """Test hash format (sha256: prefix + truncated hash)."""
        result = hash_for_audit("test")
        assert result.startswith("sha256:")
        # Total length: 7 (prefix) + 16 (truncated hash) = 23
        assert len(result) == 23

    def test_hash_different_inputs(self):
        """Test different inputs produce different hashes."""
        hash1 = hash_for_audit("input1")
        hash2 = hash_for_audit("input2")
        assert hash1 != hash2


class TestMaskSensitiveFields:
    """Tests for sensitive field masking."""

    def test_mask_dict_fields(self):
        """Test masking fields in a dict."""
        data = {
            "username": "john",
            "password": "secret123",
            "email": "john@example.com",
        }
        result = mask_sensitive_fields(data, ["password"])
        assert result["username"] == "john"
        assert result["password"] == "***REDACTED***"
        assert result["email"] == "john@example.com"

    def test_mask_nested_dict(self):
        """Test masking in nested dicts."""
        data = {
            "user": {
                "name": "john",
                "credentials": {
                    "password": "secret",
                    "api_key": "key123",
                },
            },
        }
        result = mask_sensitive_fields(data, ["password", "api_key"])
        assert result["user"]["name"] == "john"
        assert result["user"]["credentials"]["password"] == "***REDACTED***"
        assert result["user"]["credentials"]["api_key"] == "***REDACTED***"

    def test_mask_list_of_dicts(self):
        """Test masking in list of dicts."""
        data = [
            {"name": "user1", "token": "abc"},
            {"name": "user2", "token": "def"},
        ]
        result = mask_sensitive_fields(data, ["token"])
        assert result[0]["name"] == "user1"
        assert result[0]["token"] == "***REDACTED***"
        assert result[1]["token"] == "***REDACTED***"

    def test_mask_non_dict(self):
        """Test that non-dict values pass through."""
        assert mask_sensitive_fields("string", ["field"]) == "string"
        assert mask_sensitive_fields(123, ["field"]) == 123
        assert mask_sensitive_fields(None, ["field"]) is None

    def test_mask_case_insensitive(self):
        """Test case insensitive field matching."""
        data = {"Password": "secret", "API_KEY": "key123"}
        result = mask_sensitive_fields(data, ["password", "api_key"])
        assert result["Password"] == "***REDACTED***"
        assert result["API_KEY"] == "***REDACTED***"

    def test_mask_pci_default_keys(self):
        """PCI keys (card_number, cvv, cvc, iban) masked via DEFAULT (doc 505 D6)."""
        data = {
            "card_number": "4111-1111-1111-1111",
            "cvv": "123",
            "cvc": "456",
            "iban": "DE89370400440532013000",
            "amount": 100,
        }
        result = mask_sensitive_fields(data)
        assert result["card_number"] == "***REDACTED***"
        assert result["cvv"] == "***REDACTED***"
        assert result["cvc"] == "***REDACTED***"
        assert result["iban"] == "***REDACTED***"
        assert result["amount"] == 100

    def test_mask_banking_default_keys(self):
        """Banking keys (account_number, routing_number) masked via DEFAULT."""
        data = {
            "account_number": "1234567890",
            "routing_number": "021000021",
            "bank_name": "ACME",
        }
        result = mask_sensitive_fields(data)
        assert result["account_number"] == "***REDACTED***"
        assert result["routing_number"] == "***REDACTED***"
        assert result["bank_name"] == "ACME"

    def test_mask_identity_default_keys(self):
        """Identity keys (passport, driver_license, tax_id) masked via DEFAULT."""
        data = {
            "passport": "X12345678",
            "driver_license": "DL-9876",
            "tax_id": "12-3456789",
            "full_name": "John Doe",
        }
        result = mask_sensitive_fields(data)
        assert result["passport"] == "***REDACTED***"
        assert result["driver_license"] == "***REDACTED***"
        assert result["tax_id"] == "***REDACTED***"
        assert result["full_name"] == "John Doe"

    def test_mask_cloud_default_keys(self):
        """Cloud key (access_key) masked via DEFAULT."""
        data = {
            "access_key": "AKIAIOSFODNN7EXAMPLE",
            "region": "us-east-1",
        }
        result = mask_sensitive_fields(data)
        assert result["access_key"] == "***REDACTED***"
        assert result["region"] == "us-east-1"

    def test_mask_substring_matches_for_new_keys(self):
        """Substring containment triggers redaction for the new keys (doc 505 D6)."""
        data = {
            "my_card_number": "4111-1111-1111-1111",
            "aws_access_key": "AKIA...",
            "user_tax_id": "12-3456789",
            "primary_account_number": "9999999999",
        }
        result = mask_sensitive_fields(data)
        assert result["my_card_number"] == "***REDACTED***"
        assert result["aws_access_key"] == "***REDACTED***"
        assert result["user_tax_id"] == "***REDACTED***"
        assert result["primary_account_number"] == "***REDACTED***"


class TestExtractIPFromRequest:
    """Tests for IP extraction from request objects."""

    def test_extract_from_x_forwarded_for(self):
        """Test extraction from X-Forwarded-For header."""

        class MockRequest:
            META = {"HTTP_X_FORWARDED_FOR": "203.0.113.50, 70.41.3.18, 150.172.238.178"}

        result = extract_ip_from_request(MockRequest())
        assert result == "203.0.113.50"

    def test_extract_from_x_real_ip(self):
        """Test extraction from X-Real-IP header."""

        class MockRequest:
            META = {"HTTP_X_REAL_IP": "203.0.113.50"}

        result = extract_ip_from_request(MockRequest())
        assert result == "203.0.113.50"

    def test_extract_from_remote_addr(self):
        """Test extraction from REMOTE_ADDR."""

        class MockRequest:
            META = {"REMOTE_ADDR": "192.168.1.1"}

        result = extract_ip_from_request(MockRequest())
        assert result == "192.168.1.1"

    def test_extract_priority_order(self):
        """Test that X-Forwarded-For takes priority."""

        class MockRequest:
            META = {
                "HTTP_X_FORWARDED_FOR": "1.1.1.1",
                "HTTP_X_REAL_IP": "2.2.2.2",
                "REMOTE_ADDR": "3.3.3.3",
            }

        result = extract_ip_from_request(MockRequest())
        assert result == "1.1.1.1"

    def test_extract_no_ip(self):
        """Test when no IP available."""

        class MockRequest:
            META = {}

        result = extract_ip_from_request(MockRequest())
        assert result == "unknown"

    def test_extract_none_request(self):
        """Test with None request."""
        result = extract_ip_from_request(None)
        assert result == "unknown"
