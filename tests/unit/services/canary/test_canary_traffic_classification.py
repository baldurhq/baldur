"""
Tests for UU2: Canary traffic classification + exclude_canary.

Covers:
- is_canary_operation() with domain_tag decorator and DomainContext
- DLQ store_failure domain context injection of is_canary metadata
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro

# =============================================================================
# is_canary_operation() — traffic classification
# =============================================================================


class TestCanaryTrafficClassificationBehavior:
    """is_canary_operation() returns correct result based on domain context."""

    def test_is_canary_operation_returns_false_when_no_domain_tag(self):
        """Without any domain tag set, is_canary_operation() returns False."""
        from baldur.decorators.domain_tag import clear_domain_context
        from baldur_pro.services.canary.traffic_classification import (
            is_canary_operation,
        )

        # Given — no domain context active
        clear_domain_context()

        # When
        result = is_canary_operation()

        # Then
        assert result is False

    def test_is_canary_operation_returns_true_inside_canary_domain_tag(self):
        """Inside @domain_tag('canary'), is_canary_operation() returns True."""
        from baldur.decorators.domain_tag import domain_tag
        from baldur_pro.services.canary.traffic_classification import (
            is_canary_operation,
        )

        @domain_tag("canary")
        def canary_func():
            return is_canary_operation()

        # When
        result = canary_func()

        # Then
        assert result is True

    def test_is_canary_operation_returns_false_inside_different_domain_tag(self):
        """Inside @domain_tag('payment'), is_canary_operation() returns False."""
        from baldur.decorators.domain_tag import domain_tag
        from baldur_pro.services.canary.traffic_classification import (
            is_canary_operation,
        )

        @domain_tag("payment")
        def payment_func():
            return is_canary_operation()

        # When
        result = payment_func()

        # Then
        assert result is False

    def test_is_canary_operation_returns_true_inside_domain_context_manager(self):
        """DomainContext('canary') also makes is_canary_operation() return True."""
        from baldur.decorators.domain_tag import DomainContext
        from baldur_pro.services.canary.traffic_classification import (
            is_canary_operation,
        )

        # When
        with DomainContext("canary"):
            result = is_canary_operation()

        # Then
        assert result is True


# =============================================================================
# DLQ store_failure domain context injection
# =============================================================================


class TestDLQCanaryMetadataInjectionBehavior:
    """DLQ store_failure injects is_canary metadata from domain context."""

    def test_store_failure_injects_is_canary_true_when_domain_is_canary(self):
        """When get_current_domain() returns 'canary', metadata gets is_canary: True."""
        from baldur.decorators.domain_tag import DomainContext

        # Given — create a minimal mixin instance with all required attributes
        from baldur.models.dlq import DLQConfig
        from baldur_pro.services.dlq.store_operations import StoreOperationsMixin

        mock_repo = MagicMock()
        mock_failed_op = MagicMock()
        mock_failed_op.id = "test-id"
        mock_repo.create.return_value = mock_failed_op
        mixin = StoreOperationsMixin(config=DLQConfig(), repository=mock_repo)

        # When — call store_failure inside canary domain context
        with DomainContext("canary"):
            mixin.store_failure(
                domain="payment",
                failure_type="timeout",
                error_code="E001",
                error_message="Connection timed out",
                mode="sync",
            )

        # Then — verify repository.create was called with is_canary in metadata
        call_kwargs = mock_repo.create.call_args
        assert call_kwargs is not None
        metadata = call_kwargs.kwargs.get("metadata") or call_kwargs[1].get("metadata")
        assert metadata is not None
        assert metadata.get("is_canary") is True
