"""Unit tests for ``AuditEntry.from_wal_dict()`` (669 D3).

The audit WAL drained by the background sync worker (the recovery-replay
path, Pipeline A) holds the **native WAL schema** written by the
``log_*_audit`` helpers — ``event_type`` (not ``action``) and a float-epoch
``timestamp`` (not an ISO string). Running a plain ``from_dict()`` over that
shape drops the action and resets the timestamp to now, corrupting the two
most audit-critical fields for a compliance trail.

``from_wal_dict()`` dispatches on shape: the native shape routes to
``_from_native_wal_dict()`` (faithful field map), the ``to_dict()`` shape
delegates to ``from_dict()`` as defense-in-depth. Covers:
- Shape dispatch (native / ``to_dict()`` / neither).
- Native field fidelity (timestamp typing, ``event_type``->action,
  native-only-keys overflow into ``details``, totality).
- Audit-critical preservation (action derived from ``event_type``,
  timestamp equals the original epoch — never reset-to-now).

The native payload fixture mirrors ``baldur_pro.services.audit.base``
``_write_to_wal`` output verbatim so the converter is exercised against the
real recovery-replay drain input, not a fiction. (This is an OSS test — it
builds plain dicts and imports no ``baldur_pro`` symbol.)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from baldur.interfaces.audit_adapter import AuditAction, AuditEntry
from tests.factories.time_helpers import freeze_time

# A fixed, clearly-past epoch so "timestamp preserved" is distinguishable from
# any reset-to-now. Whole seconds so float<->datetime round-trips exactly.
_FIXED_DT = datetime(2020, 6, 15, 12, 0, 0, tzinfo=UTC)
_FIXED_EPOCH = _FIXED_DT.timestamp()


def _native_wal_entry(**overrides) -> dict:
    """A faithful native WAL payload mirroring ``_write_to_wal`` output.

    Kept in lockstep with the PRO writer's key set (record_id, event_type,
    trace_id, source, details, success, error_message, domain, target_id,
    actor_id, actor_type, actor_roles, celery_context, float-epoch timestamp,
    synced) so the converter is tested against the real drain input.
    """
    entry = {
        "record_id": "audit-abc123def456",
        "event_type": "CB_STATE_CHANGE",
        "trace_id": "trace-001",
        "source": "CircuitBreaker",
        "details": {"old_state": "closed", "new_state": "open"},
        "success": True,
        "error_message": None,
        "domain": "payment",
        "target_id": "payment-service",
        "actor_id": "alice",
        "actor_type": "user",
        "actor_roles": ["admin"],
        "celery_context": None,
        "timestamp": _FIXED_EPOCH,
        "synced": False,
    }
    entry.update(overrides)
    return entry


# =============================================================================
# Shape dispatch — native vs to_dict() vs neither
# =============================================================================


class TestFromWalDictDispatch:
    """``from_wal_dict()`` routes each schema to its own converter (669 D3)."""

    def test_native_shape_routes_to_native_converter(self):
        """A dict with ``event_type`` and no top-level ``action`` takes the
        native path — proven by the action deriving from ``event_type`` (a
        plain ``from_dict`` would read the absent ``action`` key -> "")."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry())

        # Native path mapped event_type -> action; from_dict would give "".
        assert entry.action == "CB_STATE_CHANGE"
        # ...and preserved the float epoch instead of resetting it.
        assert entry.timestamp == _FIXED_DT

    def test_to_dict_shape_delegates_to_from_dict(self):
        """A ``to_dict()`` shape (has ``action``) delegates to ``from_dict()``
        — the result must equal a direct ``from_dict()`` call."""
        data = {
            "action": "cb_force_open",
            "timestamp": "2020-06-15T12:00:00+00:00",
            "actor_id": "bob",
            "details": {"k": "v"},
        }

        assert AuditEntry.from_wal_dict(data) == AuditEntry.from_dict(data)

    def test_action_present_wins_even_with_event_type(self):
        """When BOTH ``action`` and ``event_type`` are present, ``action``
        present routes to ``from_dict`` — ``event_type`` overflows into
        ``details`` (native path would instead map it to ``action``)."""
        data = {
            "action": "cb_force_open",
            "event_type": "CB_STATE_CHANGE",
            "timestamp": "2020-06-15T12:00:00+00:00",
        }

        entry = AuditEntry.from_wal_dict(data)

        assert entry.action == AuditAction.CB_FORCE_OPEN
        # from_dict overflowed the unknown key rather than mapping it.
        assert entry.details["event_type"] == "CB_STATE_CHANGE"

    def test_neither_shape_delegates_to_from_dict(self):
        """No ``event_type`` and no ``action`` -> ``from_dict`` (action "")."""
        entry = AuditEntry.from_wal_dict({"actor_id": "x"})

        assert entry.action == ""
        assert entry.actor_id == "x"

    def test_conversion_is_idempotent_for_native_shape(self):
        """Same native dict in -> equal ``AuditEntry`` out across repeat calls
        (deterministic: fixed epoch, no reset-to-now nondeterminism)."""
        data = _native_wal_entry()

        assert AuditEntry.from_wal_dict(data) == AuditEntry.from_wal_dict(data)


# =============================================================================
# Native field fidelity — _from_native_wal_dict via from_wal_dict
# =============================================================================


class TestFromNativeWalDict:
    """Field-by-field mapping of the native ``_write_to_wal`` schema."""

    # --- timestamp typing (boundary analysis) --------------------------------

    def test_timestamp_float_epoch_preserved(self):
        """A float epoch is parsed as UTC and preserved exactly."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(timestamp=_FIXED_EPOCH))

        assert entry.timestamp == _FIXED_DT

    def test_timestamp_int_epoch_preserved(self):
        """An int epoch (whole seconds) parses identically to the float."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(timestamp=int(_FIXED_EPOCH)))

        assert entry.timestamp == _FIXED_DT

    def test_timestamp_iso_string_parsed(self):
        """A defensive ISO-string timestamp is parsed (Z-suffix normalized)."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(timestamp="2020-06-15T12:00:00Z")
        )

        assert entry.timestamp == _FIXED_DT

    def test_timestamp_datetime_passes_through(self):
        """A ``datetime`` timestamp passes through untouched."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(timestamp=_FIXED_DT))

        assert entry.timestamp == _FIXED_DT

    def test_timestamp_none_falls_back_to_now(self):
        """A missing/None timestamp falls back to ``utc_now()``."""
        with freeze_time("2021-03-03 09:00:00"):
            entry = AuditEntry.from_wal_dict(_native_wal_entry(timestamp=None))

        assert entry.timestamp == datetime(2021, 3, 3, 9, 0, 0, tzinfo=UTC)

    def test_timestamp_bool_is_not_treated_as_epoch(self):
        """``True``/``False`` are ``int`` subclasses; the bool guard must run
        BEFORE the int/float branch so a bool never becomes epoch 0/1."""
        with freeze_time("2021-03-03 09:00:00"):
            entry = AuditEntry.from_wal_dict(_native_wal_entry(timestamp=True))

        # Fell back to now, NOT datetime.fromtimestamp(1).
        assert entry.timestamp == datetime(2021, 3, 3, 9, 0, 0, tzinfo=UTC)
        assert entry.timestamp != datetime.fromtimestamp(1, tz=UTC)

    # --- event_type -> action (equivalence) ----------------------------------

    def test_event_type_matching_enum_becomes_enum_member(self):
        """A native ``event_type`` that matches an ``AuditAction`` value is
        promoted to the enum member."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(event_type="cb_force_open"))

        assert entry.action is AuditAction.CB_FORCE_OPEN

    @pytest.mark.parametrize("event_type", ["CB_STATE_CHANGE", "GOVERNANCE_BLOCKED"])
    def test_event_type_uppercase_miss_kept_verbatim(self, event_type):
        """Upper-cased native event types miss the (lowercase) enum values and
        are kept as the verbatim recorded string, not dropped."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(event_type=event_type))

        assert entry.action == event_type
        assert not isinstance(entry.action, AuditAction)

    # --- native-only keys -> details (set membership) ------------------------

    def test_native_only_keys_folded_into_details(self):
        """``record_id``/``source``/``synced``/``celery_context``/``trace_id``
        are folded into ``details`` (they have no first-class home)."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(
                record_id="audit-xyz",
                source="CircuitBreaker",
                synced=False,
                celery_context={"task": "t1"},
                trace_id="trace-42",
            )
        )

        assert entry.details["record_id"] == "audit-xyz"
        assert entry.details["source"] == "CircuitBreaker"
        assert entry.details["synced"] is False
        assert entry.details["celery_context"] == {"task": "t1"}
        assert entry.details["trace_id"] == "trace-42"

    def test_inner_details_preserved_alongside_native_keys(self):
        """The helper's own inner ``details`` payload survives the fold."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(details={"old_state": "closed", "new_state": "open"})
        )

        assert entry.details["old_state"] == "closed"
        assert entry.details["new_state"] == "open"
        # Native-only key still folded in beside the inner payload.
        assert entry.details["record_id"] == "audit-abc123def456"

    def test_native_key_does_not_clobber_inner_details(self):
        """The fold is non-destructive: an inner ``details`` value shadows a
        same-named top-level native key."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(
                source="OUTER",
                details={"source": "INNER"},
            )
        )

        assert entry.details["source"] == "INNER"

    def test_event_type_and_timestamp_not_duplicated_into_details(self):
        """``event_type``/``timestamp`` are first-class-mapped, not also
        overflowed into ``details``."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry())

        assert "event_type" not in entry.details
        assert "timestamp" not in entry.details

    # --- totality ------------------------------------------------------------

    @pytest.mark.parametrize(
        "data",
        [
            {"event_type": "X"},
            {"event_type": "X", "timestamp": None},
            {"event_type": "X", "timestamp": True},
            {"event_type": "X", "details": None},
            {"event_type": "X", "actor_roles": None},
            {"event_type": "X", "success": None},
        ],
        ids=[
            "only-event-type",
            "none-timestamp",
            "bool-timestamp",
            "none-details",
            "none-actor-roles",
            "none-success",
        ],
    )
    def test_conversion_is_total_for_realistic_native_dicts(self, data):
        """A malformed-but-realistic native dict never raises — a poison entry
        cannot stall the sync cursor. (Missing/None fields fall to defaults.)"""
        entry = AuditEntry.from_wal_dict(data)

        assert isinstance(entry, AuditEntry)

    @pytest.mark.parametrize(
        "data",
        [
            {"event_type": "X", "timestamp": "garbage"},
            {"event_type": "X", "timestamp": "1592222400"},
            {"event_type": "X", "timestamp": float("inf")},
            {"event_type": "X", "timestamp": float("nan")},
            {"event_type": "X", "details": [1, 2, 3]},
            {"event_type": "X", "actor_roles": 5},
        ],
        ids=[
            "non-iso-string-ts",
            "numeric-string-ts",
            "inf-epoch-ts",
            "nan-epoch-ts",
            "non-mapping-details",
            "non-list-actor-roles",
        ],
    )
    def test_conversion_is_total_for_corrupted_native_dicts(self, data):
        """A CORRUPTED native WAL entry (bad-type / out-of-range field — e.g.
        a torn write or JSON bit-flip) never raises — the guarantee that keeps
        a malformed entry from becoming a poison entry stalling the sync cursor.

        Regression: the timestamp parse, the ``details`` fold, and the
        ``actor_roles`` coerce were unguarded and raised
        ``ValueError``/``OverflowError``/``TypeError`` on these inputs.
        """
        entry = AuditEntry.from_wal_dict(data)

        assert isinstance(entry, AuditEntry)

    def test_corrupted_timestamp_falls_back_to_now(self):
        """A non-parseable timestamp degrades to ``utc_now()`` — not a crash,
        not a bogus epoch."""
        with freeze_time("2021-03-03 09:00:00"):
            entry = AuditEntry.from_wal_dict(
                _native_wal_entry(timestamp="not-a-timestamp")
            )

        assert entry.timestamp == datetime(2021, 3, 3, 9, 0, 0, tzinfo=UTC)

    def test_corrupted_details_degrades_to_empty_but_still_folds_native_keys(self):
        """A non-mapping ``details`` degrades to an empty dict, yet the
        native-only keys still fold in — totality does not drop the overflow."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(details=[1, 2, 3], record_id="audit-keep")
        )

        assert entry.details["record_id"] == "audit-keep"


# =============================================================================
# Audit-critical preservation — action + original timestamp
# =============================================================================


class TestFromWalDictPreservation:
    """The two fields the bug corrupted: action and timestamp (669 G2)."""

    def test_action_derived_from_event_type(self):
        """The recovered action reflects the native ``event_type``."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(event_type="RETRY_EXHAUSTED")
        )

        assert entry.action == "RETRY_EXHAUSTED"

    def test_timestamp_equals_original_epoch_not_reset_to_now(self):
        """The recovered timestamp equals the original float epoch — the core
        forensic guarantee (never silently reset to now)."""
        with freeze_time("2026-07-01 00:00:00"):
            entry = AuditEntry.from_wal_dict(_native_wal_entry())

        assert entry.timestamp == _FIXED_DT
        # Explicitly NOT the frozen "now" — the reset-to-now bug is closed.
        assert entry.timestamp != datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)

    def test_plain_from_dict_would_corrupt_the_native_shape(self):
        """Regression anchor: this is WHY ``from_wal_dict`` exists. A plain
        ``from_dict`` over the native shape drops the action (no ``action``
        key) and resets the timestamp (float is not parsed) — the two-field
        corruption the converter prevents."""
        with freeze_time("2026-07-01 00:00:00"):
            corrupted = AuditEntry.from_dict(_native_wal_entry())

        assert corrupted.action == ""  # event_type ignored -> action lost
        assert corrupted.timestamp == datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)

    def test_direct_map_fields_preserved(self):
        """actor / target / domain fields map straight through."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(
                actor_id="alice",
                actor_type="user",
                actor_roles=["admin", "auditor"],
                target_id="payment-service",
                domain="payment",
            )
        )

        assert entry.actor_id == "alice"
        assert entry.actor_type == "user"
        assert entry.actor_roles == ["admin", "auditor"]
        assert entry.target_id == "payment-service"
        assert entry.domain == "payment"

    def test_failure_result_fields_preserved(self):
        """A failed audited action preserves ``success=False`` + message."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(success=False, error_message="central rejected")
        )

        assert entry.success is False
        assert entry.error_message == "central rejected"
