"""Unit tests for PendingConfigService cross-worker reload + actor (665 D3/D7).

D3 — the leader applier must see pending changes created on other workers/pods:
``get_due_changes`` reloads the in-memory mirror from the shared backend first,
so the apply path (which re-reads the mirror via ``get_pending_change`` /
``mark_applied``) acts on backend-authoritative state.

D7 — the requesting operator's identity is preserved through the deferred-apply
trail: ``PendingConfigChange.requested_by`` (defaulted to "system", symmetric to
``cancelled_by``) is stored by ``create_pending_change`` and round-trips through
serialization; a legacy blob lacking the key loads as "system".

Cross-worker behavior is exercised by two directly-constructed services over one
shared ``MemoryStateBackend`` — no infra, so this stays a unit test.
"""

from __future__ import annotations

import pytest

from baldur.core.apply_strategy import ApplyOptions, ApplyStrategy
from baldur.core.state_backend import (
    FileStateBackend,
    ListCapableBackend,
    MemoryStateBackend,
    configure_state_backend,
    reset_state_backend,
)
from baldur.services.pending_config import (
    PendingConfigChange,
    PendingConfigService,
    PendingStatus,
    reset_pending_config_service,
)


@pytest.fixture
def shared_backend():
    """Install one shared in-memory backend that every service instance reads.

    Both ``PendingConfigService`` instances resolve ``get_state_backend()`` in
    their ctor, so configuring the singleton here makes them share state — the
    multi-worker topology, mock-backed.
    """
    backend = MemoryStateBackend()
    configure_state_backend(backend)
    reset_pending_config_service()
    yield backend
    reset_pending_config_service()
    reset_state_backend()


def _immediate() -> ApplyOptions:
    """Apply options that schedule the change for *now* (immediately due)."""
    return ApplyOptions(strategy=ApplyStrategy.IMMEDIATE)


# =============================================================================
# Contract — requested_by field (D7)
# =============================================================================


class TestPendingConfigChangeContract:
    """PendingConfigChange.requested_by contract (665 D7)."""

    def test_requested_by_defaults_to_system(self):
        """A change built without an actor defaults requested_by to 'system'."""
        change = PendingConfigChange(
            id="x1",
            config_type="circuit_breaker",
            changes={"failure_threshold": 10},
            strategy="delayed",
        )

        assert change.requested_by == "system"

    def test_requested_by_round_trips_through_serialization(self):
        """to_dict/from_dict preserves the requested_by actor."""
        change = PendingConfigChange(
            id="x2",
            config_type="retry",
            changes={"max_attempts": 5},
            strategy="delayed",
            requested_by="alice",
        )

        blob = change.to_dict()
        assert "requested_by" in blob

        restored = PendingConfigChange.from_dict(blob)
        assert restored.requested_by == "alice"

    def test_legacy_blob_without_key_loads_as_system(self):
        """A pre-D7 serialized blob lacking the key loads with the 'system' default."""
        legacy = {
            "id": "old1",
            "config_type": "retry",
            "changes": {"max_attempts": 5},
            "strategy": "delayed",
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
            "scheduled_at": "2026-01-01T00:00:00+00:00",
        }

        change = PendingConfigChange.from_dict(legacy)

        assert change.requested_by == "system"


# =============================================================================
# Behavior — create_pending_change actor passthrough (D7)
# =============================================================================


class TestCreatePendingChangeActor:
    """create_pending_change stores the requesting operator (665 D7)."""

    def test_create_stores_requested_by(self, shared_backend):
        """A passed requested_by is persisted on the stored change."""
        service = PendingConfigService()

        change = service.create_pending_change(
            config_type="circuit_breaker",
            changes={"failure_threshold": 10},
            apply_options=_immediate(),
            requested_by="alice",
        )

        stored = service.get_pending_change(change.id)
        assert stored is not None
        assert stored.requested_by == "alice"

    def test_create_defaults_requested_by_to_system(self, shared_backend):
        """Omitting requested_by stores 'system' (system-initiated change)."""
        service = PendingConfigService()

        change = service.create_pending_change(
            config_type="circuit_breaker",
            changes={"failure_threshold": 10},
            apply_options=_immediate(),
        )

        stored = service.get_pending_change(change.id)
        assert stored is not None
        assert stored.requested_by == "system"


# =============================================================================
# Behavior — cross-worker mirror reload (D3)
# =============================================================================


class TestPendingConfigReload:
    """get_due_changes reloads from the shared backend so the leader applier
    sees pending changes created on other workers (665 D3)."""

    def test_due_change_created_on_other_instance_is_visible(self, shared_backend):
        """A change created by worker A is returned by worker B's get_due_changes."""
        # Given two workers over one backend, B built BEFORE the change exists.
        worker_b = PendingConfigService()
        worker_a = PendingConfigService()

        # When A creates a due change after B's mirror was already loaded empty
        change = worker_a.create_pending_change(
            config_type="circuit_breaker",
            changes={"failure_threshold": 10},
            apply_options=_immediate(),
            requested_by="alice",
        )

        # Then B's reload-on-read surfaces it (not present at B's construction).
        due_ids = [c.id for c in worker_b.get_due_changes()]
        assert change.id in due_ids

    def test_followon_mark_applied_succeeds_cross_instance(self, shared_backend):
        """After B sees the cross-worker change, B can mark it applied."""
        worker_a = PendingConfigService()
        worker_b = PendingConfigService()

        change = worker_a.create_pending_change(
            config_type="dlq",
            changes={"max_replay_attempts": 5},
            apply_options=_immediate(),
        )

        # B sees it via reload, then marks it applied (the apply path's re-read
        # hits the refreshed mirror, not a stale "not found").
        assert change.id in [c.id for c in worker_b.get_due_changes()]
        applied = worker_b.mark_applied(change.id)

        assert applied is not None
        assert applied.status == PendingStatus.APPLIED.value
        # And it is no longer due on either worker (key deleted in the backend).
        assert change.id not in [c.id for c in worker_a.get_due_changes()]

    def test_get_all_pending_reflects_cross_instance_creation(self, shared_backend):
        """get_all_pending_changes also reloads, surfacing A's change on B."""
        worker_a = PendingConfigService()
        worker_b = PendingConfigService()

        change = worker_a.create_pending_change(
            config_type="retry",
            changes={"max_attempts": 7},
            apply_options=_immediate(),
        )

        all_ids = [c.id for c in worker_b.get_all_pending_changes()]
        assert change.id in all_ids


# =============================================================================
# Behavior — per-key storage eliminates the shared-blob lost update (666 D5)
# =============================================================================


class TestPendingPerKeyBehavior:
    """Per-key storage (``pending_config:change:{id}``) makes create = set(one
    key) and apply/cancel = delete(one key), eliminating the shared-blob
    read-modify-write that silently lost a change created on another process
    during an in-flight apply cycle (666 D5, SC#2)."""

    def test_mark_applied_does_not_drop_concurrently_created_sibling(
        self, shared_backend
    ):
        # Given worker A holding change1, with its mirror loaded before id2 exists
        worker_a = PendingConfigService()
        change1 = worker_a.create_pending_change(
            "circuit_breaker", {"failure_threshold": 10}, _immediate()
        )

        # When B creates change2 in A's apply window, then A marks change1 applied
        worker_b = PendingConfigService()
        change2 = worker_b.create_pending_change(
            "retry", {"max_attempts": 7}, _immediate()
        )
        worker_a.mark_applied(change1.id)

        # Then change2 is NOT clobbered by A's terminal write (the pre-666 bug):
        # A only deleted change1's own key, never touching change2's.
        survivor = PendingConfigService().get_pending_change(change2.id)
        assert survivor is not None
        assert survivor.status == PendingStatus.PENDING.value

    def test_get_all_pending_enumerates_each_per_key_change(self, shared_backend):
        service = PendingConfigService()
        c1 = service.create_pending_change("retry", {"max_attempts": 5}, _immediate())
        c2 = service.create_pending_change(
            "dlq", {"max_replay_attempts": 3}, _immediate()
        )

        ids = {c.id for c in service.get_all_pending_changes()}
        assert {c1.id, c2.id} <= ids

        # Each lives at its own per-key entry (the pattern enumeration), and the
        # pattern matches neither the legacy blob nor the history key.
        entries = shared_backend.get_all(PendingConfigService._CHANGE_KEY_PATTERN)
        assert len(entries) == 2

    def test_requested_by_survives_cross_instance_per_key_read(self, shared_backend):
        """The per-key entry preserves the requesting operator across instances —
        a different process reads it backend-authoritatively."""
        creator = PendingConfigService()
        change = creator.create_pending_change(
            "retry", {"max_attempts": 5}, _immediate(), requested_by="alice"
        )

        reader = PendingConfigService()
        loaded = reader.get_pending_change(change.id)

        assert loaded is not None
        assert loaded.requested_by == "alice"


# =============================================================================
# Behavior — one-time legacy single-blob migration (666 D5)
# =============================================================================


class TestPendingLegacyBlobMigration:
    """``_migrate_legacy_blob`` hydrates a pre-666 single-blob into per-key
    entries once, then deletes the legacy key (cheap upgrade insurance)."""

    def test_legacy_blob_hydrated_to_per_key_and_deleted(self, shared_backend):
        # Given a legacy single-blob at the old STORAGE_KEY
        legacy_change = PendingConfigChange(
            id="legacy1",
            config_type="retry",
            changes={"max_attempts": 9},
            strategy="delayed",
            status=PendingStatus.PENDING.value,
            scheduled_at="2026-01-01T00:00:00+00:00",
        )
        shared_backend.set(
            PendingConfigService.STORAGE_KEY,
            {"pending": [legacy_change.to_dict()], "history": []},
        )

        # When a service initializes (migration runs in _load_state)
        service = PendingConfigService()

        # Then the legacy entry is hydrated to its own per-key location ...
        migrated = service.get_pending_change("legacy1")
        assert migrated is not None
        assert migrated.config_type == "retry"
        assert shared_backend.exists("pending_config:change:legacy1")
        # ... and the legacy blob key is deleted (one-time migration).
        assert shared_backend.get(PendingConfigService.STORAGE_KEY) is None


# =============================================================================
# Behavior — history append fallback on FileStateBackend (no push_limit) (666 D5)
# =============================================================================


class TestPendingHistoryFallbackBehavior:
    """``FileStateBackend`` (the OSS default) does NOT implement
    ``ListCapableBackend``, so the terminal-record history append must fall back
    to a capped get/set list — an unguarded ``push_limit`` would
    ``AttributeError`` on a default deployment (666 D5, SC#2 history half)."""

    def test_terminal_record_appends_on_file_backend_without_push_limit(self, tmp_path):
        backend = FileStateBackend(tmp_path / "state")
        # Guard precondition: the default backend genuinely lacks push_limit.
        assert not isinstance(backend, ListCapableBackend)

        configure_state_backend(backend)
        reset_pending_config_service()
        try:
            service = PendingConfigService()
            change = service.create_pending_change(
                "retry", {"max_attempts": 5}, _immediate()
            )

            # Appends a terminal record to history — must NOT raise
            # AttributeError on the push_limit-less File backend.
            service.mark_applied(change.id)

            history = service.get_history("retry")
            assert any(
                c.id == change.id and c.status == PendingStatus.APPLIED.value
                for c in history
            )
        finally:
            reset_pending_config_service()
            reset_state_backend()
