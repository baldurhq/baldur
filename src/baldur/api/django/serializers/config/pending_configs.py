"""
Pending Configuration Change Serializers.

Serializers for pending configuration changes.
"""

from rest_framework import serializers


class PendingConfigChangeSerializer(serializers.Serializer):
    """Serializer for pending configuration change."""

    id = serializers.CharField(read_only=True)
    config_type = serializers.CharField(read_only=True)
    changes = serializers.DictField(read_only=True)
    strategy = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    created_at = serializers.CharField(read_only=True)
    scheduled_at = serializers.CharField(read_only=True)
    applied_at = serializers.CharField(read_only=True, allow_null=True)
    cancelled_at = serializers.CharField(read_only=True, allow_null=True)
    previous_values = serializers.DictField(read_only=True)


class CancelPendingChangeSerializer(serializers.Serializer):
    """Serializer for cancelling a pending change."""

    reason = serializers.CharField(required=False, max_length=500)
