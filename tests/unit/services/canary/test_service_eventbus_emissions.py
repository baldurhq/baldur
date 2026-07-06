"""
CanaryRolloutService — EventBus emission unit tests (doc 483).

Verifies the 7 lifecycle EventBus emissions added by 483 (D2/D4a):
- ``start_rollout()``  → ``CANARY_ROLLOUT_STARTED``
- ``promote()``        → ``CANARY_ROLLOUT_PROMOTED`` (mid-stage)
                       → ``CANARY_ROLLOUT_COMPLETED`` (final stage)
- ``rollback()``       → ``CANARY_ROLLOUT_ROLLED_BACK``
- ``pause()``          → ``CANARY_ROLLOUT_PAUSED``
- ``resume()``         → ``CANARY_ROLLOUT_RESUMED``
- ``cancel()``         → ``CANARY_ROLLOUT_CANCELLED``

Data payload contract (D2): exactly the key set
``{"rollout_id", "state", "current_stage_index", "config_type", "previous_state"}``.

Negative path: when ``_save_rollout`` returns False (version conflict),
no event is emitted (D4a contract).

Mock pattern follows ``tests/unit/circuit_breaker/test_cb_eventbus_emissions.py``:
``patch.object(service, "_emit_event", side_effect=capture)`` to capture
``(event_type, data)`` tuples without exercising the real EventBus.

Reference: 483
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

import pytest

from baldur.models.canary import CanaryStage, CanaryState
from baldur.services.event_bus.bus.event_types import EventType
from baldur_pro.services.canary.models import CanaryRollout
from baldur_pro.services.canary.service import CanaryRolloutService

LIFECYCLE_DATA_KEYS = {
    "rollout_id",
    "state",
    "current_stage_index",
    "config_type",
    "previous_state",
}


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def service():
    """CanaryRolloutService with chaos_guard / runtime_config_manager / store
    mocked."""
    svc = CanaryRolloutService(store=None)

    mock_guard = MagicMock()
    mock_guard.check_conflict.return_value = MagicMock(
        has_conflict=False,
        can_proceed=True,
        safe_clusters=["seoul"],
        chaos_clusters=[],
        policy_applied=MagicMock(value="smart"),
        warning_message=None,
    )
    svc._chaos_guard = mock_guard

    # Runtime-config manager mock isolates the in-process config apply from the
    # real config StateBackend (start/promote/rollback route through it).
    mock_manager = MagicMock()
    mock_manager.get_all_config.return_value = {
        "circuit_breaker": {"failure_threshold": 5},
    }
    svc._runtime_config_manager = mock_manager

    return svc


def _make_rollout(
    *,
    state: CanaryState = CanaryState.CREATED,
    current_stage_index: int = 0,
    rollout_id: str = "rollout-1",
    version: int = 0,
    stages: list[CanaryStage] | None = None,
) -> CanaryRollout:
    if stages is None:
        stages = [
            CanaryStage(name="canary", clusters=["seoul"], percentage=10.0),
            CanaryStage(name="full", clusters=["tokyo"], percentage=100.0),
        ]
    return CanaryRollout(
        id=rollout_id,
        config_type="circuit_breaker",
        previous_values={"failure_threshold": 5},
        new_values={"failure_threshold": 3},
        state=state,
        current_stage_index=current_stage_index,
        stages=stages,
        created_by="admin@example.com",
        version=version,
    )


def _capture(captured: list):
    def _side_effect(event_type, data, **kwargs):
        captured.append((event_type, data))

    return _side_effect


# =============================================================================
# Lifecycle event data shape (Contract — _lifecycle_event_data)
# =============================================================================


class TestLifecycleEventDataContract:
    """Contract: ``_lifecycle_event_data`` produces the expected payload shape."""

    def test_lifecycle_event_data_has_exact_keys(self):
        rollout = _make_rollout(state=CanaryState.CANARY, current_stage_index=1)

        data = CanaryRolloutService._lifecycle_event_data(
            rollout, previous_state="created"
        )

        assert set(data.keys()) == LIFECYCLE_DATA_KEYS

    def test_lifecycle_event_data_field_values(self):
        rollout = _make_rollout(
            state=CanaryState.CANARY,
            current_stage_index=2,
            rollout_id="abc123",
        )

        data = CanaryRolloutService._lifecycle_event_data(
            rollout, previous_state="paused"
        )

        assert data["rollout_id"] == "abc123"
        assert data["state"] == "canary"  # CanaryState.CANARY.value
        assert data["current_stage_index"] == 2
        assert data["config_type"] == "circuit_breaker"
        assert data["previous_state"] == "paused"


# =============================================================================
# start_rollout — CANARY_ROLLOUT_STARTED
# =============================================================================


class TestStartRolloutEventBusEmissionBehavior:
    """``start_rollout()`` emits CANARY_ROLLOUT_STARTED after successful save."""

    def test_start_rollout_emits_started_with_previous_state_created(self, service):
        from baldur.adapters.memory.canary_rollout import InMemoryCanaryRolloutStore

        rollout = _make_rollout(state=CanaryState.CREATED, version=0)
        # A CREATED rollout about to start owns its config lock; the start-time
        # ownership guard reads it via the store.
        service._store = InMemoryCanaryRolloutStore()
        service._store.acquire_config_lock(rollout.config_type, rollout.id)
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_check_governance_gate", return_value=True),
            patch.object(service, "_check_shadow_evaluation", return_value=None),
            patch.object(service, "_apply_config_in_process", return_value=True),
            patch.object(service, "_save_rollout", return_value=True),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.start_rollout(rollout.id)

        assert result is True
        assert len(captured) == 1
        event_type, data = captured[0]
        assert event_type == EventType.CANARY_ROLLOUT_STARTED
        assert set(data.keys()) == LIFECYCLE_DATA_KEYS
        assert data["previous_state"] == CanaryState.CREATED.value
        assert data["state"] == CanaryState.CANARY.value
        assert data["rollout_id"] == rollout.id

    def test_start_rollout_no_emit_on_version_conflict(self, service):
        """D4a: ``_save_rollout`` returns False → zero emissions."""
        rollout = _make_rollout(state=CanaryState.CREATED, version=0)
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_check_governance_gate", return_value=True),
            patch.object(service, "_check_shadow_evaluation", return_value=None),
            patch.object(service, "_apply_config_in_process", return_value=True),
            patch.object(service, "_save_rollout", return_value=False),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.start_rollout(rollout.id)

        assert result is False
        assert captured == []


# =============================================================================
# promote — PROMOTED vs COMPLETED (parametrized over branch)
# =============================================================================


class TestPromoteEventBusEmissionBehavior:
    """``promote()`` emits PROMOTED on mid-stage, COMPLETED on final stage."""

    @pytest.mark.parametrize(
        ("is_final_stage", "expected_event", "expected_state"),
        [
            (False, EventType.CANARY_ROLLOUT_PROMOTED, CanaryState.CANARY),
            (True, EventType.CANARY_ROLLOUT_COMPLETED, CanaryState.COMPLETED),
        ],
    )
    def test_promote_emits_correct_event_per_branch(
        self, service, is_final_stage, expected_event, expected_state
    ):
        # 2-stage rollout: mid-stage = current_stage_index=0, final = index=1
        current_index = 1 if is_final_stage else 0
        rollout = _make_rollout(
            state=CanaryState.CANARY,
            current_stage_index=current_index,
            version=0,
        )
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_check_governance_gate", return_value=True),
            patch.object(service, "_check_live_canary_evaluation", return_value=None),
            patch.object(service, "_apply_config_in_process", return_value=True),
            patch.object(service, "_save_rollout", return_value=True),
            patch.object(service, "_remove_from_active"),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.promote(rollout.id, force=True)

        assert result is True
        assert len(captured) == 1
        event_type, data = captured[0]
        assert event_type == expected_event
        assert rollout.state == expected_state
        assert set(data.keys()) == LIFECYCLE_DATA_KEYS
        # previous_state captured before mutation; rollout was CANARY
        assert data["previous_state"] == CanaryState.CANARY.value
        assert data["state"] == expected_state.value

    def test_promote_no_emit_on_version_conflict(self, service):
        rollout = _make_rollout(
            state=CanaryState.CANARY, current_stage_index=0, version=0
        )
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_check_governance_gate", return_value=True),
            patch.object(service, "_check_live_canary_evaluation", return_value=None),
            patch.object(service, "_apply_config_in_process", return_value=True),
            patch.object(service, "_save_rollout", return_value=False),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.promote(rollout.id, force=True)

        assert result is False
        assert captured == []


# =============================================================================
# rollback — CANARY_ROLLOUT_ROLLED_BACK
# =============================================================================


class TestRollbackEventBusEmissionBehavior:
    """``rollback()`` emits CANARY_ROLLOUT_ROLLED_BACK after successful save."""

    def test_rollback_emits_rolled_back(self, service):
        rollout = _make_rollout(
            state=CanaryState.CANARY, current_stage_index=1, version=0
        )
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_check_governance_gate", return_value=True),
            patch.object(service, "_apply_config_in_process", return_value=True),
            patch.object(service, "_save_rollout", return_value=True),
            patch.object(service, "_remove_from_active"),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.rollback(rollout.id, reason="error spike")

        assert result is True
        assert len(captured) == 1
        event_type, data = captured[0]
        assert event_type == EventType.CANARY_ROLLOUT_ROLLED_BACK
        assert set(data.keys()) == LIFECYCLE_DATA_KEYS
        assert data["previous_state"] == CanaryState.CANARY.value
        assert data["state"] == CanaryState.ROLLED_BACK.value

    def test_rollback_no_emit_on_version_conflict(self, service):
        rollout = _make_rollout(
            state=CanaryState.CANARY, current_stage_index=1, version=0
        )
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_check_governance_gate", return_value=True),
            patch.object(service, "_apply_config_in_process", return_value=True),
            patch.object(service, "_save_rollout", return_value=False),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.rollback(rollout.id, reason="error spike")

        assert result is False
        assert captured == []


# =============================================================================
# pause — CANARY_ROLLOUT_PAUSED
# =============================================================================


class TestPauseEventBusEmissionBehavior:
    """``pause()`` emits CANARY_ROLLOUT_PAUSED after successful save."""

    def test_pause_emits_paused(self, service):
        rollout = _make_rollout(state=CanaryState.CANARY, version=0)
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_save_rollout", return_value=True),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.pause(rollout.id, reason="manual")

        assert result is True
        assert len(captured) == 1
        event_type, data = captured[0]
        assert event_type == EventType.CANARY_ROLLOUT_PAUSED
        assert set(data.keys()) == LIFECYCLE_DATA_KEYS
        assert data["previous_state"] == CanaryState.CANARY.value
        assert data["state"] == CanaryState.PAUSED.value

    def test_pause_no_emit_on_version_conflict(self, service):
        rollout = _make_rollout(state=CanaryState.CANARY, version=0)
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_save_rollout", return_value=False),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.pause(rollout.id, reason="manual")

        assert result is False
        assert captured == []


# =============================================================================
# resume — CANARY_ROLLOUT_RESUMED
# =============================================================================


class TestResumeEventBusEmissionBehavior:
    """``resume()`` emits CANARY_ROLLOUT_RESUMED after successful save."""

    def test_resume_emits_resumed(self, service):
        rollout = _make_rollout(state=CanaryState.PAUSED, version=0)
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_check_governance_gate", return_value=True),
            patch.object(service, "_save_rollout", return_value=True),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.resume(rollout.id)

        assert result is True
        assert len(captured) == 1
        event_type, data = captured[0]
        assert event_type == EventType.CANARY_ROLLOUT_RESUMED
        assert set(data.keys()) == LIFECYCLE_DATA_KEYS
        assert data["previous_state"] == CanaryState.PAUSED.value
        assert data["state"] == CanaryState.CANARY.value

    def test_resume_no_emit_on_version_conflict(self, service):
        rollout = _make_rollout(state=CanaryState.PAUSED, version=0)
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_check_governance_gate", return_value=True),
            patch.object(service, "_save_rollout", return_value=False),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.resume(rollout.id)

        assert result is False
        assert captured == []


# =============================================================================
# cancel — CANARY_ROLLOUT_CANCELLED
# =============================================================================


class TestCancelEventBusEmissionBehavior:
    """``cancel()`` emits CANARY_ROLLOUT_CANCELLED after save."""

    def test_cancel_emits_cancelled(self, service):
        rollout = _make_rollout(state=CanaryState.CREATED)
        captured: list = []

        with (
            patch.object(service, "get_rollout", return_value=rollout),
            patch.object(service, "_save_rollout"),
            patch.object(service, "_remove_from_active"),
            patch("baldur_pro.services.canary.service.log_canary_action"),
            patch.object(service, "_emit_event", side_effect=_capture(captured)),
        ):
            result = service.cancel(rollout.id, reason="ops cancel")

        assert result is True
        assert len(captured) == 1
        event_type, data = captured[0]
        assert event_type == EventType.CANARY_ROLLOUT_CANCELLED
        assert set(data.keys()) == LIFECYCLE_DATA_KEYS
        assert data["previous_state"] == CanaryState.CREATED.value
        assert data["state"] == CanaryState.CANCELLED.value


# =============================================================================
# Cross-cutting contract — _event_source identity
# =============================================================================


class TestCanaryEventSourceContract:
    def test_event_source_is_canary_rollout(self):
        assert CanaryRolloutService._event_source == "canary_rollout"
