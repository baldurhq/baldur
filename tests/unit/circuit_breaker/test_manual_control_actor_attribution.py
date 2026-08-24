"""A manual circuit-breaker reset is attributed to the operator who ran it.

The recorded audit action is derived from ``actor_type`` — a non-system actor
yields ``CB_FORCE_CLOSE``, the system actor yields ``CB_AUTO_CLOSE``. So a
state-change audit call that omits the actor files every reset as automatic,
whoever ran it. ``force_open`` / ``force_close`` already read the actor from
context; ``reset`` did not, which left the one operator-driven path that still
looked automatic in the compliance ledger.

Both directions are pinned here: an operator's reset must carry their identity,
and an automatic caller's reset must keep the system attribution rather than
being promoted into a "forced" one.
"""

from __future__ import annotations

from unittest.mock import create_autospec, patch

import pytest

# The audit helper these tests observe is PRO-backed.
pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro

AUDIT = "baldur_pro.services.audit.log_cb_state_change_audit"
SYSTEM_ENABLED = "baldur.services.circuit_breaker.manual_control._is_system_enabled"


@pytest.fixture
def service():
    """A circuit-breaker service whose repository reports a real transition."""
    from baldur.interfaces.repositories import CircuitBreakerStateRepository
    from baldur.services.circuit_breaker.config import CircuitBreakerConfig
    from baldur.services.circuit_breaker.service import CircuitBreakerService

    config = create_autospec(CircuitBreakerConfig, instance=True)
    config.enabled = True
    config.manual_override_ttl_minutes = 90
    config.recovery_timeout = 60
    config.success_threshold = 2

    repository = create_autospec(CircuitBreakerStateRepository, instance=True)
    repository.atomic_reset.return_value = (True, "open", "closed")

    return CircuitBreakerService(config=config, repository=repository)


class TestResetActorAttributionBehavior:
    """``reset()`` records the actor the invocation is running under."""

    @patch(SYSTEM_ENABLED, autospec=True, return_value=True)
    @patch(AUDIT, autospec=True)
    def test_an_operator_reset_carries_the_operator_identity(
        self, mock_audit, _system_enabled, service
    ):
        """The defect in one line: without this the row says the system did it."""
        from baldur.context.actor_context import ActorContext

        with ActorContext.set_actor(
            actor_id="alice@ops-box", actor_type="cli", source="baldur cb reset"
        ):
            service.reset("payment-pg", reason="post-incident")

        kwargs = mock_audit.call_args.kwargs
        assert kwargs["actor_id"] == "alice@ops-box"
        assert kwargs["actor_type"] == "cli"

    @patch(SYSTEM_ENABLED, autospec=True, return_value=True)
    @patch(AUDIT, autospec=True)
    def test_an_unattributed_reset_stays_the_system_actor(
        self, mock_audit, _system_enabled, service
    ):
        """The negative half. Reset is also reached from automatic recovery, so
        reading the context must not promote those into operator actions —
        otherwise the distinction the ledger exists to make is lost the other
        way round."""
        service.reset("payment-pg", reason="auto")

        assert mock_audit.call_args.kwargs["actor_type"] == "system"

    @patch(SYSTEM_ENABLED, autospec=True, return_value=True)
    @patch(AUDIT, autospec=True)
    def test_a_reset_that_changes_nothing_records_nothing(
        self, mock_audit, _system_enabled, service
    ):
        """Unchanged from before: the audit call is gated on a real transition,
        and reading the actor must not move that gate."""
        service.repository.atomic_reset.return_value = (True, "closed", "closed")

        service.reset("payment-pg", reason="noop")

        mock_audit.assert_not_called()
