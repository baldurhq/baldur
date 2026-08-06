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

from baldur.api.django.serializers.control import (
    MetricsResponseSerializer,
    ServiceMetricsSerializer,
)

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


# =============================================================================
# Failure-rate honest-absence contract (746 D5/D12)
# =============================================================================

_AGGREGATE_FIELDS = {
    "total_services": 2,
    "healthy_services": 1,
    "degraded_services": 0,
    "last_5m_request_count": 0,
    "avg_time_to_recovery": None,
    "auto_allowed_count_24h": 0,
    "auto_blocked_count_24h": 0,
    "total_dlq_pending": 3,
    "dlq_by_service": {"payment": 3},
    "services": [{**_MEASURED_FIELDS, "retry_success_rate": None}],
    "timestamp": "2026-08-06T00:00:00Z",
    "collection_duration_ms": 4,
}


class TestServiceMetricsSerializerFailureRateNullContract:
    """The per-service failure rate and circuit state model "not measured".

    Both fields carried a fabricated constant before a producer existed — 0.0 on
    every row, and "closed" for every breaker this worker cannot see. A
    ``FloatField`` and a ``CharField`` reject ``None`` by default, so without
    ``allow_null`` the honest value would fail validation on the way out and the
    fabricated one would again be the only renderable answer.
    """

    def test_failure_rate_5m_null_is_valid(self):
        """An unmeasured service must survive serialization as null."""
        serializer = ServiceMetricsSerializer(
            data={
                **_MEASURED_FIELDS,
                "retry_success_rate": None,
                "failure_rate_5m": None,
            }
        )

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["failure_rate_5m"] is None

    def test_failure_rate_5m_measured_value_still_validates(self):
        """Null-tolerance must not stop a real producer's number from landing."""
        serializer = ServiceMetricsSerializer(
            data={
                **_MEASURED_FIELDS,
                "retry_success_rate": None,
                "failure_rate_5m": 0.75,
            }
        )

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["failure_rate_5m"] == 0.75

    def test_failure_rate_5m_field_declares_allow_null(self):
        """Stated on the field, so the contract is visible in the schema."""
        assert ServiceMetricsSerializer().fields["failure_rate_5m"].allow_null is True

    def test_circuit_state_null_is_valid(self):
        """A breaker this worker holds no evidence for renders null, not closed."""
        serializer = ServiceMetricsSerializer(
            data={
                **_MEASURED_FIELDS,
                "retry_success_rate": None,
                "circuit_state": None,
            }
        )

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["circuit_state"] is None

    def test_circuit_state_field_declares_allow_null(self):
        """The absence contract is on the field, not only in the payload builder."""
        assert ServiceMetricsSerializer().fields["circuit_state"].allow_null is True

    def test_failure_rate_help_text_discloses_the_null_meaning(self):
        """The published schema has to say what null means.

        The description is derived from ``help_text``, so a reader of the API
        docs learns that absence is absence rather than a healthy zero.
        """
        help_text = str(ServiceMetricsSerializer().fields["failure_rate_5m"].help_text)

        assert "Null when nothing was measured" in help_text

    def test_circuit_state_help_text_discloses_the_per_worker_scope(self):
        """The state is this worker's view, which the description must say."""
        help_text = str(ServiceMetricsSerializer().fields["circuit_state"].help_text)

        assert "this worker" in help_text.lower()
        assert "null" in help_text.lower()


class TestMetricsResponseAggregateNullRateContract:
    """The aggregate five-minute pair, on the same honest-absence terms.

    The rate was the DLQ backlog's pending/total share and the count was its
    all-time capture total. The rate is now nullable because a worker with no
    observed admissions has no rate; the count stays non-nullable because 0 is
    the honest count for "nothing observed".
    """

    def test_last_5m_failure_rate_null_is_valid(self):
        """A boot-fresh worker's aggregate must serialize as null."""
        serializer = MetricsResponseSerializer(
            data={**_AGGREGATE_FIELDS, "last_5m_failure_rate": None}
        )

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["last_5m_failure_rate"] is None

    def test_last_5m_failure_rate_measured_value_still_validates(self):
        """A real cross-key ratio still lands."""
        serializer = MetricsResponseSerializer(
            data={**_AGGREGATE_FIELDS, "last_5m_failure_rate": 0.375}
        )

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["last_5m_failure_rate"] == 0.375

    def test_last_5m_failure_rate_field_declares_allow_null(self):
        """Visible in the schema, not only in the payload builder."""
        assert (
            MetricsResponseSerializer().fields["last_5m_failure_rate"].allow_null
            is True
        )

    def test_last_5m_request_count_still_rejects_null(self):
        """0 is the honest count, so the counter is deliberately not nullable.

        Making it nullable would let "no observed admissions" and "we did not
        count" render identically, which is the distinction the rate field
        already carries.
        """
        serializer = MetricsResponseSerializer(
            data={
                **_AGGREGATE_FIELDS,
                "last_5m_failure_rate": None,
                "last_5m_request_count": None,
            }
        )

        assert serializer.is_valid() is False
        assert "last_5m_request_count" in serializer.errors

    def test_aggregate_help_text_discloses_the_null_meaning(self):
        """The published description states what a null aggregate means."""
        help_text = str(
            MetricsResponseSerializer().fields["last_5m_failure_rate"].help_text
        )

        assert "Null when nothing was measured" in help_text
