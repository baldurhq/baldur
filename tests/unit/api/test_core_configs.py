"""
RateLimitConfigSerializer field-set + bounds contract tests.

``RateLimitConfigSerializer`` validates the inbound HTTP quota family only.
The outbound 429-backoff dials (``base_delay`` / ``max_delay`` /
``jitter_percent`` / ``default_retry_after`` / ``backoff_multiplier``) were
split into ``RateLimitBackoffSettings`` (``BALDUR_RATE_LIMIT_BACKOFF_``) and
are no longer console-editable — submitting one through this serializer must
drop it rather than apply it.

Verification techniques:
- Contract: hardcoded inbound-quota field set + per-field numeric bounds
- Field-set contract: the retained (allowed) fields, and the backoff dials
  that are dropped when submitted (the rejected surface)
- Bounds vs settings: each retained field's serializer bounds equal the
  ``RateLimitSettings`` bound for the same field
- Boundary analysis: out-of-range quota values are rejected by ``is_valid()``
"""

import annotated_types
import django
import pytest
from django.conf import settings
from rest_framework import serializers

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

from baldur.api.django.serializers.config.core_configs import (
    RateLimitConfigSerializer,
)
from baldur.settings.rate_limit import RateLimitSettings

# The inbound HTTP quota fields the console serializer retains.
INBOUND_QUOTA_FIELDS = frozenset(
    {
        "control_api_rate_limit",
        "control_api_window_seconds",
        "emergency_rate_limit",
        "emergency_window_seconds",
        "middleware_rate_limit",
        "middleware_window_seconds",
        "decorator_enabled",
        "redis_ttl",
    }
)

# Apply-strategy fields injected by ApplyStrategyMixin into every config
# serializer — shared plumbing, not part of the rate-limit domain.
APPLY_STRATEGY_FIELDS = frozenset(
    {"apply_strategy", "delay_seconds", "grace_timeout_seconds", "reason"}
)

# Outbound 429-backoff dials that moved to RateLimitBackoffSettings; the
# console serializer must not declare or apply any of them.
BACKOFF_DIAL_FIELDS = frozenset(
    {
        "base_delay",
        "max_delay",
        "jitter_percent",
        "default_retry_after",
        "backoff_multiplier",
    }
)

# Canonical (min, max) each retained numeric quota field must expose — on both
# the console serializer and RateLimitSettings. decorator_enabled is boolean
# and carries no numeric bounds, so it is asserted separately.
QUOTA_FIELD_BOUNDS = {
    "control_api_rate_limit": (1, 10000),
    "control_api_window_seconds": (1, 3600),
    "emergency_rate_limit": (1, 100),
    "emergency_window_seconds": (1, 3600),
    "middleware_rate_limit": (0, 10000),
    "middleware_window_seconds": (1, 3600),
    "redis_ttl": (60, 86400),
}


def _settings_numeric_bounds(field_name: str) -> tuple[int | None, int | None]:
    """Return (min, max) declared on a RateLimitSettings field."""
    metadata = RateLimitSettings.model_fields[field_name].metadata
    minimum = next((m.ge for m in metadata if isinstance(m, annotated_types.Ge)), None)
    maximum = next((m.le for m in metadata if isinstance(m, annotated_types.Le)), None)
    return minimum, maximum


class TestRateLimitConfigSerializerContract:
    """RateLimitConfigSerializer inbound-quota field set + bounds contract."""

    def test_declares_exactly_the_inbound_quota_fields(self):
        # Given a fresh serializer
        serializer = RateLimitConfigSerializer()

        # When the apply-strategy plumbing is set aside
        domain_fields = set(serializer.fields) - APPLY_STRATEGY_FIELDS

        # Then only the inbound-quota family remains
        assert domain_fields == set(INBOUND_QUOTA_FIELDS)

    def test_config_type_targets_the_rate_limit_domain(self):
        # _config_type wires validate_with_safe_fallback to the rate_limit
        # safe-default set; a drift here silently disables the fallback.
        assert RateLimitConfigSerializer._config_type == "rate_limit"

    @pytest.mark.parametrize("backoff_field", sorted(BACKOFF_DIAL_FIELDS))
    def test_backoff_dial_submitted_to_console_is_dropped(self, backoff_field):
        # Given a payload carrying a moved backoff dial alongside a valid quota
        serializer = RateLimitConfigSerializer(
            data={backoff_field: 2.0, "control_api_rate_limit": 50}
        )

        # When validated (the undeclared dial does not fail validation)
        assert serializer.is_valid(), serializer.errors

        # Then the backoff dial is absent from the applied changes; only the
        # quota field survives.
        changes = serializer.get_config_changes()
        assert backoff_field not in changes
        assert changes["control_api_rate_limit"] == 50

    @pytest.mark.parametrize(
        ("field_name", "bounds"), sorted(QUOTA_FIELD_BOUNDS.items())
    )
    def test_serializer_field_bounds_match_spec(self, field_name, bounds):
        field = RateLimitConfigSerializer().fields[field_name]
        assert (field.min_value, field.max_value) == bounds

    @pytest.mark.parametrize(
        ("field_name", "bounds"), sorted(QUOTA_FIELD_BOUNDS.items())
    )
    def test_serializer_bounds_match_settings(self, field_name, bounds):
        # Bounds vs settings: the console serializer and RateLimitSettings must
        # agree on the acceptable range for every retained field, so a value
        # the env layer accepts is not silently rejected by the console (or
        # vice versa).
        assert _settings_numeric_bounds(field_name) == bounds

    def test_decorator_enabled_is_a_boolean_field(self):
        field = RateLimitConfigSerializer().fields["decorator_enabled"]
        assert isinstance(field, serializers.BooleanField)

    @pytest.mark.parametrize(
        ("field_name", "value", "expected_valid"),
        [
            # middleware_rate_limit uniquely allows 0 (the "disabled" sentinel)
            ("middleware_rate_limit", 0, True),
            ("middleware_rate_limit", 10001, False),
            # every other rate ceiling rejects 0 (floor is 1)
            ("control_api_rate_limit", 0, False),
            ("control_api_rate_limit", 10000, True),
            ("control_api_rate_limit", 10001, False),
            ("emergency_rate_limit", 0, False),
            ("emergency_rate_limit", 100, True),
            ("emergency_rate_limit", 101, False),
            # redis_ttl floor is 60s, not 1
            ("redis_ttl", 59, False),
            ("redis_ttl", 60, True),
        ],
    )
    def test_quota_value_boundary_enforcement(self, field_name, value, expected_valid):
        serializer = RateLimitConfigSerializer(data={field_name: value})
        assert serializer.is_valid() is expected_valid
