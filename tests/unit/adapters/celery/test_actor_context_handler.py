"""
Unit tests for ActorContextHandler.

Tests the automatic injection of ActorContext headers into outbound Celery messages
before task publication. Follows CausationHandler pattern.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from baldur.adapters.celery.handlers.actor_context_handler import (
    ActorContextHandler,
)
from baldur.adapters.celery.signal_config import SignalHooksSettings
from baldur.context.actor_context import (
    CELERY_HEADER_ACTOR_ID,
    CELERY_HEADER_ACTOR_IP,
    CELERY_HEADER_ACTOR_ROLES,
    CELERY_HEADER_ACTOR_SESSION,
    CELERY_HEADER_ACTOR_SOURCE,
    CELERY_HEADER_ACTOR_TYPE,
    ActorContext,
    _current_actor,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def reset_actor_context():
    """Reset ActorContext before and after each test."""
    token = _current_actor.set(None)
    yield
    _current_actor.reset(token)


@pytest.fixture
def enabled_config() -> SignalHooksSettings:
    """Create enabled SignalHooksSettings."""
    return SignalHooksSettings(enabled=True)


@pytest.fixture
def disabled_config() -> SignalHooksSettings:
    """Create disabled SignalHooksSettings."""
    return SignalHooksSettings(enabled=False)


# =============================================================================
# Contract Tests
# =============================================================================


class TestActorContextHandlerContract:
    """ActorContextHandler design contract verification."""

    def test_handler_accepts_signal_config(self):
        """Handler constructor accepts SignalHooksSettings."""
        config = SignalHooksSettings()
        handler = ActorContextHandler(config)
        assert handler._config is config

    def test_handle_method_signature_matches_celery_signal(self):
        """handle() accepts standard Celery before_task_publish signal kwargs."""
        config = SignalHooksSettings()
        handler = ActorContextHandler(config)

        # Should accept all standard signal kwargs without error
        handler.handle(
            sender=None,
            body=None,
            exchange=None,
            routing_key=None,
            headers={},
            properties=None,
            declare=None,
            retry_policy=None,
        )


# =============================================================================
# Behavior Tests
# =============================================================================


class TestActorContextHandlerDisabledBehavior:
    """Behavior when handler is disabled."""

    def test_disabled_config_does_not_inject_headers(self, disabled_config):
        """When config.enabled=False, no headers are injected."""
        handler = ActorContextHandler(disabled_config)
        headers = {}

        with ActorContext.set_actor(
            actor_id="test-user",
            actor_type="user",
            source="web",
        ):
            handler.handle(headers=headers)

        assert headers == {}

    def test_disabled_config_early_return_no_side_effects(self, disabled_config):
        """Disabled handler returns early without any side effects."""
        handler = ActorContextHandler(disabled_config)
        headers = {"existing_key": "existing_value"}

        with ActorContext.set_actor(
            actor_id="should-not-be-injected",
            actor_type="user",
            source="web",
        ):
            handler.handle(headers=headers)

        # Only original key should exist, no actor headers added
        assert list(headers.keys()) == ["existing_key"]


class TestActorContextHandlerNoActorBehavior:
    """Behavior when no ActorContext is set."""

    def test_no_actor_context_does_not_inject_headers(self, enabled_config):
        """When no ActorContext is set, no headers are injected."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        # No ActorContext set
        handler.handle(headers=headers)

        assert headers == {}

    def test_actor_context_is_set_false_skips_injection(self, enabled_config):
        """When ActorContext.is_set() returns False, injection is skipped."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        # Explicitly ensure no actor is set
        _current_actor.set(None)

        handler.handle(headers=headers)

        assert CELERY_HEADER_ACTOR_ID not in headers


class TestActorContextHandlerInjectionBehavior:
    """Behavior when ActorContext is active and headers should be injected."""

    def test_injects_all_required_headers(self, enabled_config):
        """All 6 actor headers are injected when ActorContext is active."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        with ActorContext.set_actor(
            actor_id="user@example.com",
            actor_type="user",
            source="web",
            ip_address="192.168.1.1",
            session_id="sess-abc123",
            roles=["admin", "operator"],
        ):
            handler.handle(headers=headers)

        # Verify all 6 headers
        assert headers[CELERY_HEADER_ACTOR_ID] == "user@example.com"
        assert headers[CELERY_HEADER_ACTOR_TYPE] == "user"
        assert headers[CELERY_HEADER_ACTOR_SOURCE] == "celery_from_web"
        assert headers[CELERY_HEADER_ACTOR_IP] == "192.168.1.1"
        assert headers[CELERY_HEADER_ACTOR_SESSION] == "sess-abc123"
        assert headers[CELERY_HEADER_ACTOR_ROLES] == json.dumps(["admin", "operator"])

    def test_source_prefixed_with_celery_from(self, enabled_config):
        """Source is prefixed with 'celery_from_' to indicate propagation."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        with ActorContext.set_actor(
            actor_id="test",
            actor_type="user",
            source="api",
        ):
            handler.handle(headers=headers)

        assert headers[CELERY_HEADER_ACTOR_SOURCE] == "celery_from_api"

    def test_optional_ip_address_only_if_present(self, enabled_config):
        """ip_address header is only set if actor has ip_address."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        with ActorContext.set_actor(
            actor_id="test",
            actor_type="user",
            source="web",
            ip_address=None,
        ):
            handler.handle(headers=headers)

        assert CELERY_HEADER_ACTOR_IP not in headers

    def test_optional_session_id_only_if_present(self, enabled_config):
        """session_id header is only set if actor has session_id."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        with ActorContext.set_actor(
            actor_id="test",
            actor_type="user",
            source="web",
            session_id=None,
        ):
            handler.handle(headers=headers)

        assert CELERY_HEADER_ACTOR_SESSION not in headers

    def test_roles_serialized_as_json_array(self, enabled_config):
        """roles are serialized as JSON array string."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        with ActorContext.set_actor(
            actor_id="test",
            actor_type="user",
            source="web",
            roles=["viewer", "editor"],
        ):
            handler.handle(headers=headers)

        roles_json = headers[CELERY_HEADER_ACTOR_ROLES]
        assert json.loads(roles_json) == ["viewer", "editor"]

    def test_empty_roles_serialized_as_empty_array(self, enabled_config):
        """Empty roles list is serialized as '[]'."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        with ActorContext.set_actor(
            actor_id="test",
            actor_type="user",
            source="web",
            roles=[],
        ):
            handler.handle(headers=headers)

        assert headers[CELERY_HEADER_ACTOR_ROLES] == "[]"


class TestActorContextHandlerIdempotencyBehavior:
    """Idempotency: existing headers are not overwritten."""

    def test_does_not_overwrite_existing_actor_headers(self, enabled_config):
        """Existing actor headers are preserved (explicit override takes precedence)."""
        handler = ActorContextHandler(enabled_config)
        headers = {
            CELERY_HEADER_ACTOR_ID: "existing-user",
        }

        with ActorContext.set_actor(
            actor_id="new-user",
            actor_type="user",
            source="web",
        ):
            handler.handle(headers=headers)

        # Original header preserved
        assert headers[CELERY_HEADER_ACTOR_ID] == "existing-user"
        # Other headers not added (skipped entirely when actor_id exists)
        assert CELERY_HEADER_ACTOR_TYPE not in headers


class TestActorContextHandlerEdgeCaseBehavior:
    """Edge cases and error handling."""

    def test_none_headers_does_not_crash(self, enabled_config):
        """When headers is None, handler returns without crashing."""
        handler = ActorContextHandler(enabled_config)

        with ActorContext.set_actor(
            actor_id="test",
            actor_type="user",
            source="web",
        ):
            # Should not raise
            handler.handle(headers=None)

    def test_exception_in_get_current_does_not_propagate(self, enabled_config):
        """Exceptions during actor retrieval are caught — signal handler must not crash."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        # Patch get_current_or_none to raise an exception
        with patch(
            "baldur.context.actor_context.ActorContext.get_current_or_none",
            side_effect=RuntimeError("actor retrieval failed"),
        ):
            # Should not raise — exception is caught in the handler
            handler.handle(headers=headers)

        # Headers should be unchanged (no injection occurred)
        assert headers == {}

    def test_exception_during_json_dumps_does_not_propagate(self, enabled_config):
        """Exceptions during roles serialization are caught."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        with ActorContext.set_actor(
            actor_id="test",
            actor_type="user",
            source="web",
            roles=["admin"],
        ):
            # Patch json.dumps to raise
            with patch(
                "baldur.adapters.celery.handlers.actor_context_handler.json.dumps",
                side_effect=TypeError("not serializable"),
            ):
                # Should not raise
                handler.handle(headers=headers)


# =============================================================================
# Side Effect Tests
# =============================================================================


class TestActorContextHandlerSideEffects:
    """Verify side effects (logging) occur correctly."""

    def test_logs_debug_when_headers_injected(self, enabled_config):
        """Debug log emitted when headers are successfully injected."""
        handler = ActorContextHandler(enabled_config)
        headers = {}

        with (
            ActorContext.set_actor(
                actor_id="logged-user",
                actor_type="user",
                source="web",
            ),
            patch(
                "baldur.adapters.celery.handlers.actor_context_handler.logger"
            ) as mock_logger,
        ):
            handler.handle(headers=headers)

        mock_logger.debug.assert_called()
        # Verify the log event name
        call_args = mock_logger.debug.call_args
        assert call_args[0][0] == "celery_signal.actor_headers_injected"

    def test_logs_debug_when_headers_already_present(self, enabled_config):
        """Debug log emitted when existing headers are preserved."""
        handler = ActorContextHandler(enabled_config)
        headers = {CELERY_HEADER_ACTOR_ID: "existing"}

        with (
            ActorContext.set_actor(
                actor_id="new-user",
                actor_type="user",
                source="web",
            ),
            patch(
                "baldur.adapters.celery.handlers.actor_context_handler.logger"
            ) as mock_logger,
        ):
            handler.handle(headers=headers)

        mock_logger.debug.assert_called_once()
        call_args = mock_logger.debug.call_args
        assert call_args[0][0] == "celery_signal.actor_headers_present"

    def test_logs_debug_when_headers_unavailable(self, enabled_config):
        """Debug log emitted when headers dict is None."""
        handler = ActorContextHandler(enabled_config)

        with (
            ActorContext.set_actor(
                actor_id="test",
                actor_type="user",
                source="web",
            ),
            patch(
                "baldur.adapters.celery.handlers.actor_context_handler.logger"
            ) as mock_logger,
        ):
            handler.handle(headers=None)

        mock_logger.debug.assert_called_once()
        call_args = mock_logger.debug.call_args
        assert call_args[0][0] == "celery_signal.actor_headers_unavailable"
