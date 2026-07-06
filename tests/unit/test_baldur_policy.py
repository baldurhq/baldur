"""
Unit tests for IdempotencyKey generation.

Tests core factory methods: for_operation, for_event, for_resource_action, custom.
"""

from baldur.services import (
    IdempotencyDomain,
    IdempotencyKey,
)


class TestIdempotencyKey:
    """Tests for IdempotencyKey generation using generic baldur API."""

    def test_operation_key_format(self):
        """Verify generic operation idempotency key format."""
        key = IdempotencyKey.for_operation(
            entity_type="order",
            entity_id=12345,
            operation="process",
        )

        assert key.domain == IdempotencyDomain.EXTERNAL_SERVICE
        assert "order" in key.key
        assert "12345" in key.key
        assert "process" in key.key
        assert "idempotency:external_service:" in key.cache_key

    def test_operation_with_custom_domain(self):
        """Verify operation key with custom domain."""
        key = IdempotencyKey.for_operation(
            entity_type="payment",
            entity_id=12345,
            operation="confirm",
            domain=IdempotencyDomain.INTERNAL_PROCESS,
        )

        assert key.domain == IdempotencyDomain.INTERNAL_PROCESS
        assert "payment" in key.key
        assert "confirm" in key.key

    def test_event_key_format(self):
        """Verify event idempotency key format."""
        key = IdempotencyKey.for_event(event_id="evt_webhook_123")

        assert key.domain == IdempotencyDomain.EVENT
        assert key.key == "evt_webhook_123"
        assert "idempotency:event:" in key.cache_key

    def test_resource_action_key_format(self):
        """Verify resource action key format."""
        key = IdempotencyKey.for_resource_action(
            resource_type="point",
            resource_id=12345,
            action="earn",
            amount=500,
        )

        assert key.domain == IdempotencyDomain.INTERNAL_PROCESS
        assert "point" in key.key
        assert "12345" in key.key
        assert "earn" in key.key
        assert "500" in key.key

    def test_resource_action_without_amount(self):
        """Verify resource action key without amount."""
        key = IdempotencyKey.for_resource_action(
            resource_type="inventory",
            resource_id=999,
            action="deduct",
        )

        assert key.domain == IdempotencyDomain.INTERNAL_PROCESS
        assert "inventory" in key.key
        assert "999" in key.key
        assert "deduct" in key.key

    def test_custom_key_format(self):
        """Verify custom idempotency key format."""
        key = IdempotencyKey.custom(
            key="custom:operation:12345",
            entity_type="order",
            entity_id=12345,
        )

        assert key.domain == IdempotencyDomain.CUSTOM
        assert key.key == "custom:operation:12345"
        assert "entity_type" in key.components
        assert key.components["entity_id"] == 12345

    def test_key_hash_is_consistent(self):
        """Same inputs produce same hash."""
        key1 = IdempotencyKey.for_operation(
            entity_type="order", entity_id=123, operation="process"
        )
        key2 = IdempotencyKey.for_operation(
            entity_type="order", entity_id=123, operation="process"
        )

        assert key1.hash == key2.hash

    def test_different_inputs_produce_different_hash(self):
        """Different inputs produce different hash."""
        key1 = IdempotencyKey.for_operation(
            entity_type="order", entity_id=123, operation="process"
        )
        key2 = IdempotencyKey.for_operation(
            entity_type="order", entity_id=124, operation="process"
        )

        assert key1.hash != key2.hash
