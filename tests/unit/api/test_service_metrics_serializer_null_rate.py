"""``ServiceMetricsSerializer`` accepts an unmeasured retry success rate.

No adapter computes per-domain retry success rates, so the comprehensive-metrics
payload renders ``retry_success_rate: null`` instead of the fabricated 100.0 it
used to carry. The serializer is the second half of that change: a
``FloatField`` rejects ``None`` by default, so without ``allow_null`` the honest
value would fail validation on the way out and the fabricated one would be the
only renderable answer.

Reference:
    src/baldur/api/django/serializers/control.py
"""

from __future__ import annotations

import django
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

from baldur.api.django.serializers.control import ServiceMetricsSerializer

_MEASURED_FIELDS = {
    "service_name": "payment",
    "failure_rate_5m": 0.0,
    "dlq_count": 3,
    "circuit_state": "closed",
    # The sibling field that already models "not computable" as null — the
    # shape `retry_success_rate` was changed to match.
    "avg_recovery_time_seconds": None,
}


class TestServiceMetricsSerializerNullRateContract:
    """The retry-success-rate field models "not measured" as null."""

    def test_retry_success_rate_null_is_valid(self):
        """The honest-absence value must survive serialization."""
        serializer = ServiceMetricsSerializer(
            data={**_MEASURED_FIELDS, "retry_success_rate": None}
        )

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["retry_success_rate"] is None

    def test_retry_success_rate_measured_value_still_validates(self):
        """Null-tolerance must not stop a real producer's number from landing."""
        serializer = ServiceMetricsSerializer(
            data={**_MEASURED_FIELDS, "retry_success_rate": 95.0}
        )

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["retry_success_rate"] == 95.0

    def test_retry_success_rate_field_declares_allow_null(self):
        """Stated on the field, so the contract is visible in the schema."""
        assert (
            ServiceMetricsSerializer().fields["retry_success_rate"].allow_null is True
        )

    def test_dlq_count_still_rejects_null(self):
        """Only the rate field changed — the sibling integer contract is intact.

        The per-service ``dlq_count`` renders 0 when the breakdown is
        unavailable; making it nullable is a separate, parked contract change.
        """
        serializer = ServiceMetricsSerializer(
            data={**_MEASURED_FIELDS, "retry_success_rate": None, "dlq_count": None}
        )

        assert serializer.is_valid() is False
        assert "dlq_count" in serializer.errors
