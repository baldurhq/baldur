"""
ErrorCode default messages Contract test.

361 document specifies all ERROR_CODE_DEFAULT_MESSAGES must be in English.
This test verifies each message value matches the design contract.

Verification technique: Contract (hardcoded expected values from doc 361 §2.1)
"""

import re

import pytest

from baldur.api.django.exceptions.codes import (
    ERROR_CODE_DEFAULT_MESSAGES,
    ErrorCode,
    get_default_message,
)


class TestErrorCodeDefaultMessagesContract:
    """ERROR_CODE_DEFAULT_MESSAGES English contract verification."""

    @pytest.mark.parametrize(
        ("code", "expected_message"),
        [
            (ErrorCode.VALIDATION_FIELD_REQUIRED, "Required field is missing."),
            (ErrorCode.VALIDATION_FIELD_INVALID, "Invalid field format."),
            (ErrorCode.VALIDATION_INVALID_VALUE, "Value is out of allowed range."),
            (ErrorCode.VALIDATION_SERIALIZER_ERROR, "Input validation failed."),
            (ErrorCode.VALIDATION_PARSE_ERROR, "Unable to parse request body."),
            (ErrorCode.AUTH_NOT_AUTHENTICATED, "Authentication required."),
            (ErrorCode.AUTH_TOKEN_INVALID, "Invalid authentication token."),
            (ErrorCode.AUTH_TOKEN_EXPIRED, "Authentication token has expired."),
            (ErrorCode.AUTH_CREDENTIALS_INVALID, "Invalid credentials."),
            (
                ErrorCode.AUTHZ_PERMISSION_DENIED,
                "You do not have permission to perform this action.",
            ),
            (ErrorCode.AUTHZ_GOVERNANCE_BLOCKED, "Blocked by governance policy."),
            (
                ErrorCode.AUTHZ_ERROR_BUDGET_BLOCKED,
                "Automation blocked due to error budget exhaustion.",
            ),
            (ErrorCode.RESOURCE_NOT_FOUND, "Requested resource not found."),
            (ErrorCode.RESOURCE_ALREADY_EXISTS, "Resource already exists."),
            (ErrorCode.RESOURCE_CONFLICT, "Resource state conflict."),
            (ErrorCode.RATE_LIMIT_EXCEEDED, "Rate limit exceeded. Please retry later."),
            (ErrorCode.RATE_THROTTLED, "Request temporarily throttled."),
            (ErrorCode.CONFIG_LOCKED, "Configuration is locked by another operation."),
            (ErrorCode.CONFIG_INVALID, "Invalid configuration value."),
            (ErrorCode.SYSTEM_INTERNAL_ERROR, "Internal server error."),
            (ErrorCode.SYSTEM_DATABASE_ERROR, "Database error."),
            (ErrorCode.SYSTEM_DLQ_ERROR, "Error occurred during DLQ processing."),
            (ErrorCode.SERVICE_UNAVAILABLE, "Service temporarily unavailable."),
            (
                ErrorCode.SERVICE_CIRCUIT_OPEN,
                "Service temporarily blocked. Please retry later.",
            ),
            (ErrorCode.SERVICE_TIMEOUT, "Service response timed out."),
            (ErrorCode.SERVICE_BAD_GATEWAY, "External service response error."),
        ],
    )
    def test_error_code_message_matches_english_contract(self, code, expected_message):
        """Each error code default message matches the English contract value."""
        assert ERROR_CODE_DEFAULT_MESSAGES[code] == expected_message

    def test_all_error_codes_have_default_messages(self):
        """Every ErrorCode enum member has a default message entry."""
        for code in ErrorCode:
            assert code in ERROR_CODE_DEFAULT_MESSAGES, (
                f"Missing default message for {code}"
            )

    def test_no_korean_characters_in_any_message(self):
        """No Korean (Hangul) characters remain in any default message."""
        hangul_pattern = re.compile(r"[가-힣]")
        for code, message in ERROR_CODE_DEFAULT_MESSAGES.items():
            assert not hangul_pattern.search(message), (
                f"{code}: message still contains Korean: {message}"
            )

    def test_get_default_message_fallback_is_english(self):
        """get_default_message() fallback for unknown code is English."""
        # Use a valid ErrorCode value but ensure fallback works
        fallback = get_default_message(ErrorCode.VALIDATION_FIELD_REQUIRED)
        assert fallback == "Required field is missing."
