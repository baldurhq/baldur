"""676 — OSS ReplayService replay provenance (D5/D6) + config fallback (D2).

Target: ``baldur.services.replay_service.service`` and the
``ResolutionTrigger`` enum in ``baldur.interfaces.repositories``.

    - ``_execute_replay`` stamps ``resolution_type`` from the ``trigger`` on
      success (no longer a blanket ``auto_replay``), and records the acting
      principal — explicit ``actor_id`` wins, else the ambient
      ``ActorContext`` (``system`` for background paths).
    - ``replay_single`` / ``replay_batch`` thread ``trigger`` + ``actor_id``
      straight through to ``_execute_replay``.
    - ``ResolutionTrigger`` is a ``(str, Enum)`` whose members are the stamped
      strings; ``_resolution_type_for`` normalizes enum-or-string.
    - The six 617 settings-backed helpers honor ``ReplayAutomationSettings``
      (env-honoring) on the RuntimeConfig-absent path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.repositories import ResolutionTrigger
from baldur.models.governance import GovernanceCheckResult
from baldur.services.replay_service import (
    ReplayHandler,
    ReplayService,
    register_replay_handler,
)
from baldur.services.replay_service.models import ReplayResult
from baldur.services.replay_service.service import _resolution_type_for
from baldur.settings.replay_automation import reset_replay_automation_settings

_AUDIT = "baldur.services.replay_service.service.log_dlq_replay_audit"


@dataclass
class _Op:
    """Minimal FailedOperationData stand-in for the replay path."""

    id: str = "7"
    domain: str = "payment"
    retry_count: int = 0
    status: str = "pending"
    request_data: dict = field(default_factory=dict)


class _SuccessHandler(ReplayHandler):
    """Replay handler that always succeeds (drives the success stamp path)."""

    @property
    def domain(self) -> str:
        return "payment"

    def can_replay(self, failed_op):
        return True, ""

    def replay(self, failed_op):
        return ReplayResult.succeeded(failed_op.id, "Replayed OK")


def _gov_allow() -> MagicMock:
    gov = MagicMock()
    gov.check_all_governance.return_value = GovernanceCheckResult(allowed=True)
    return gov


# =============================================================================
# ResolutionTrigger enum contract
# =============================================================================


class TestResolutionTriggerContract:
    """The enum members ARE the stamped ``resolution_type`` strings."""

    def test_member_values(self):
        assert ResolutionTrigger.MANUAL_REPLAY.value == "manual_replay"
        assert (
            ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE.value
            == "auto_replay_circuit_close"
        )
        assert ResolutionTrigger.SCHEDULED_BATCH.value == "scheduled_batch"
        assert ResolutionTrigger.TRAFFIC_AWARE.value == "traffic_aware"
        assert ResolutionTrigger.THROTTLE_REPLAY.value == "throttle_replay"
        assert ResolutionTrigger.DLQ_CONSUMER.value == "dlq_consumer"

    def test_is_str_enum_and_json_serializable(self):
        # (str, Enum) — members are strings, so json.dumps yields the value.
        assert isinstance(ResolutionTrigger.MANUAL_REPLAY, str)
        assert (
            json.dumps({"t": ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE})
            == '{"t": "auto_replay_circuit_close"}'
        )

    def test_resolution_type_for_normalizes_enum_and_raw_string(self):
        assert _resolution_type_for(ResolutionTrigger.TRAFFIC_AWARE) == "traffic_aware"
        # A raw string passes through unchanged (Track-2 beat task kwarg path).
        assert _resolution_type_for("custom_trigger") == "custom_trigger"


# =============================================================================
# _execute_replay — trigger stamp + actor
# =============================================================================


class TestExecuteReplayProvenanceBehavior:
    """The success write site stamps ``resolution_type`` from the trigger and
    records the resolved acting principal.
    """

    def setup_method(self):
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
        from baldur.core.idempotency_gate import (
            IdempotencyGate,
            configure_idempotency_gate,
        )
        from baldur.services.replay_service import _replay_handlers

        _replay_handlers.clear()
        configure_idempotency_gate(
            IdempotencyGate(cache=InMemoryCacheAdapter(key_prefix="prov_676:"))
        )
        register_replay_handler(_SuccessHandler())

    def teardown_method(self):
        from baldur.core.idempotency_gate import reset_idempotency_gate
        from baldur.services.replay_service import _replay_handlers

        reset_idempotency_gate()
        _replay_handlers.clear()

    def _service(self):
        op = _Op()
        repo = MagicMock()
        repo.try_acquire_for_replay.return_value = op
        repo.get_by_id.return_value = op
        repo.complete_replay.return_value = None
        return ReplayService(repository=repo), repo

    @pytest.mark.parametrize("trigger", list(ResolutionTrigger))
    def test_trigger_is_stamped_into_resolution_type_on_success(self, trigger):
        svc, repo = self._service()

        with patch(_AUDIT):
            result = svc._execute_replay("7", trigger=trigger)

        assert result.success is True
        repo.complete_replay.assert_called_once()
        kwargs = repo.complete_replay.call_args.kwargs
        assert kwargs["resolution_type"] == trigger.value
        assert kwargs["success"] is True

    def test_default_trigger_stamps_manual_replay(self):
        svc, repo = self._service()

        with patch(_AUDIT):
            svc._execute_replay("7")

        assert repo.complete_replay.call_args.kwargs["resolution_type"] == (
            "manual_replay"
        )

    def test_raw_string_trigger_passes_through_to_stamp(self):
        svc, repo = self._service()

        with patch(_AUDIT):
            svc._execute_replay("7", trigger="scheduled_batch")

        assert repo.complete_replay.call_args.kwargs["resolution_type"] == (
            "scheduled_batch"
        )

    def test_explicit_actor_id_recorded_in_audit(self):
        svc, _ = self._service()

        with patch(_AUDIT) as audit:
            svc._execute_replay("7", actor_id="alice@example.com")

        assert audit.call_args.kwargs["actor_id"] == "alice@example.com"

    def test_absent_actor_falls_back_to_system(self):
        # No explicit actor + no ambient context => system (background posture).
        svc, _ = self._service()

        with patch(_AUDIT) as audit:
            svc._execute_replay("7", actor_id=None)

        assert audit.call_args.kwargs["actor_id"] == "system"

    def test_absent_actor_falls_back_to_ambient_actor_context(self):
        from baldur.context.actor_context import ActorContext

        svc, _ = self._service()

        with patch(_AUDIT) as audit:
            with ActorContext.set_actor(actor_id="ops@example.com", actor_type="user"):
                svc._execute_replay("7", actor_id=None)

        assert audit.call_args.kwargs["actor_id"] == "ops@example.com"


# =============================================================================
# replay_single / replay_batch — trigger + actor pass-through
# =============================================================================


class TestReplayTriggerThreadingBehavior:
    """``trigger`` + ``actor_id`` are forwarded verbatim to ``_execute_replay``."""

    def test_replay_single_forwards_trigger_and_actor(self):
        svc = ReplayService(repository=MagicMock())
        svc._get_governance = MagicMock(return_value=_gov_allow())
        svc._execute_replay = MagicMock(return_value=ReplayResult.succeeded("7"))

        svc.replay_single("7", trigger=ResolutionTrigger.DLQ_CONSUMER, actor_id="alice")

        svc._execute_replay.assert_called_once_with(
            "7", trigger=ResolutionTrigger.DLQ_CONSUMER, actor_id="alice"
        )

    def test_replay_batch_forwards_trigger_and_actor_per_entry(self):
        entry = _Op(id="42")
        repo = MagicMock()
        repo.find_replayable.return_value = [entry]
        svc = ReplayService(repository=repo)
        svc._get_governance = MagicMock(return_value=_gov_allow())
        svc._is_adaptive_enabled = MagicMock(return_value=False)
        svc._is_priority_enabled = MagicMock(return_value=False)
        svc._execute_replay = MagicMock(return_value=ReplayResult.succeeded("42"))

        svc.replay_batch(
            domain="payment",
            trigger=ResolutionTrigger.TRAFFIC_AWARE,
            actor_id="scheduler",
        )

        svc._execute_replay.assert_called_once_with(
            "42",
            replay_type="batch",
            trigger=ResolutionTrigger.TRAFFIC_AWARE,
            actor_id="scheduler",
        )


# =============================================================================
# D2 — settings-backed helper fallback (RuntimeConfig absent)
# =============================================================================


class TestReplayServiceSettingsFallbackBehavior:
    """On the RuntimeConfig-absent path the 617 helpers resolve from
    ``ReplayAutomationSettings`` (env-honoring), not hardcoded literals.
    """

    def _absent_service(self) -> ReplayService:
        svc = ReplayService(repository=MagicMock())
        # RuntimeConfig absent => the reader returns None => settings fallback.
        svc._get_replay_automation_config = MagicMock(return_value=None)
        return svc

    def test_load_failure_type_map_honors_env_when_runtime_config_absent(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            "BALDUR_REPLAY_AUTOMATION_SERVICE_FAILURE_TYPE_MAP",
            json.dumps({"payment_api": ["TIMEOUT"]}),
        )
        reset_replay_automation_settings()
        try:
            svc = self._absent_service()
            assert svc._load_failure_type_map() == {"payment_api": ["TIMEOUT"]}
        finally:
            reset_replay_automation_settings()

    def test_is_adaptive_enabled_honors_env_when_runtime_config_absent(
        self, monkeypatch
    ):
        monkeypatch.setenv("BALDUR_REPLAY_AUTOMATION_ADAPTIVE_ENABLED", "true")
        reset_replay_automation_settings()
        try:
            svc = self._absent_service()
            assert svc._is_adaptive_enabled() is True
        finally:
            reset_replay_automation_settings()
