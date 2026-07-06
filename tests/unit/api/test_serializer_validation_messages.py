"""
Serializer ValidationError English message contract tests (doc 361 §2.11).

Tests SLOConfigSerializer and ErrorBudgetConfigSerializer raise
English ValidationError messages when cross-field validation fails.

Verification techniques:
- Contract: hardcoded English phrases from doc 361 §2.11
- Exception/edge case: validation error triggering conditions
"""

import re

import django
import pytest
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        DATABASES={},
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "rest_framework",
        ],
        REST_FRAMEWORK={},
        SECRET_KEY="test-secret-key",
    )
    django.setup()

from rest_framework.exceptions import ValidationError

from baldur.api.django.serializers.config.slo_configs import (
    ErrorBudgetConfigSerializer,
    SLOConfigSerializer,
)


class TestSLOConfigValidationMessageContract:
    """SLOConfigSerializer.validate() English message contract."""

    def test_fast_burn_rate_lte_slow_raises_english_error(self):
        """default_fast_burn_rate <= default_slow_burn_rate raises English ValidationError."""
        serializer = SLOConfigSerializer(
            data={
                "default_fast_burn_rate": 3.0,
                "default_slow_burn_rate": 5.0,
            }
        )

        with pytest.raises(ValidationError) as exc_info:
            serializer.is_valid(raise_exception=True)

        error_messages = str(exc_info.value.detail)
        assert (
            "default_fast_burn_rate must be greater than default_slow_burn_rate"
            in error_messages
        )


class TestErrorBudgetConfigValidationMessageContract:
    """ErrorBudgetConfigSerializer.validate() English message contract."""

    def test_threshold_ordering_violation_raises_english_error(self):
        """Threshold ordering violation raises English ValidationError."""
        # healthy (55) < caution (60) — passes field-level min/max but fails ordering
        serializer = ErrorBudgetConfigSerializer(
            data={
                "threshold_healthy": 55.0,
                "threshold_caution": 60.0,
            }
        )

        with pytest.raises(ValidationError) as exc_info:
            serializer.is_valid(raise_exception=True)

        error_messages = str(exc_info.value.detail)
        assert "must be greater than" in error_messages

    def test_heartbeat_timeout_lte_interval_raises_english_error(self):
        """heartbeat_timeout <= heartbeat_interval raises English ValidationError."""
        serializer = ErrorBudgetConfigSerializer(
            data={
                "heartbeat_interval_seconds": 120,
                "heartbeat_timeout_seconds": 60,
            }
        )

        with pytest.raises(ValidationError) as exc_info:
            serializer.is_valid(raise_exception=True)

        error_messages = str(exc_info.value.detail)
        assert (
            "heartbeat_timeout_seconds must be greater than heartbeat_interval_seconds"
            in error_messages
        )

    def test_no_korean_in_threshold_ordering_error(self):
        """Threshold ordering error contains no Korean characters."""
        hangul = re.compile(r"[가-힣]")
        serializer = ErrorBudgetConfigSerializer(
            data={
                "threshold_healthy": 55.0,
                "threshold_caution": 60.0,
            }
        )

        with pytest.raises(ValidationError) as exc_info:
            serializer.is_valid(raise_exception=True)

        error_text = str(exc_info.value.detail)
        assert not hangul.search(error_text), f"Korean found: {error_text}"
