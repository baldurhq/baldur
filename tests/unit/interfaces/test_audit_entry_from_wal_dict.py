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

from baldur.interfaces.audit_adapter import AuditAction, AuditEntry, ContextType
from tests.factories.time_helpers import freeze_time

# A fixed, clearly-past epoch so "timestamp preserved" is distinguishable from
# any reset-to-now. Whole seconds so float<->datetime round-trips exactly.
_FIXED_DT = datetime(2020, 6, 15, 12, 0, 0, tzinfo=UTC)
_FIXED_EPOCH = _FIXED_DT.timestamp()


def _native_wal_entry(**overrides) -> dict:
    """A faithful native WAL payload mirroring ``_write_to_wal`` output.

    Kept in lockstep with the PRO writer's key set (record_id, event_type,
    trace_id, trace_id_full, source, details, success, error_message, domain,
    target_id, target_type, actor_id, actor_type, actor_roles, celery_context,
    float-epoch timestamp) so the converter is tested against the real drain
    input.
    """
    entry = {
        "record_id": "audit-abc123def456",
        "event_type": "CB_STATE_CHANGE",
        "trace_id": "trace-001",
        "trace_id_full": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
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
    }
    entry.update(overrides)
    return entry


# =============================================================================
# Shape dispatch — native vs to_dict() vs neither
# =============================================================================


class TestFromWalDictDispatch:
    """``from_wal_dict()`` routes each schema to its own converter (669 D3)."""

    def test_native_shape_routes_to_native_converter(self):
        """A dict with ``event_type`` takes the native path — proven by the
        action deriving from ``event_type`` (a plain ``from_dict`` would read
        the absent ``action`` key -> "")."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry())

        # Native path derived the action from event_type; from_dict -> "".
        assert entry.action == "cb_state_change"
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

    def test_event_type_selects_native_converter_despite_an_action(self):
        """``event_type`` alone selects the converter. A native payload may
        carry an explicit ``action`` — the writer resolves it when the payload,
        not the event type, decides — and that action wins over the derivation
        while the float epoch is still preserved."""
        data = _native_wal_entry(action="cb_force_open")

        entry = AuditEntry.from_wal_dict(data)

        assert entry.action == AuditAction.CB_FORCE_OPEN
        # Routed natively: from_dict would have reset the float epoch to now.
        assert entry.timestamp == _FIXED_DT

    def test_no_event_type_delegates_to_from_dict(self):
        """No ``event_type`` -> ``from_dict`` (action "")."""
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

    def test_event_type_matching_an_enum_name_becomes_that_member(self):
        """An upper-cased native event type misses the (lowercase) enum values
        but matches an enum *name*, and is promoted to that member — so the
        ledger records one action vocabulary, not two casings of it."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(event_type="GOVERNANCE_BLOCKED")
        )

        assert entry.action is AuditAction.GOVERNANCE_BLOCKED

    @pytest.mark.parametrize("event_type", ["CB_STATE_CHANGE", "RATE_LIMITED"])
    def test_event_type_with_no_enum_member_is_normalised(self, event_type):
        """An event type with no enum member at all is kept as the recorded
        string, normalised to the enum's casing rather than dropped."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(event_type=event_type))

        assert entry.action == event_type.lower()
        assert not isinstance(entry.action, AuditAction)

    # --- native-only keys -> details (set membership) ------------------------

    def test_native_only_keys_folded_into_details(self):
        """``record_id``/``source``/``celery_context`` and the trace pair are
        folded into ``details`` (they have no first-class home)."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(
                record_id="audit-xyz",
                source="CircuitBreaker",
                celery_context={"task": "t1"},
                trace_id="trace-42",
                trace_id_full="00-trace42-span01-01",
            )
        )

        assert entry.details["record_id"] == "audit-xyz"
        assert entry.details["source"] == "CircuitBreaker"
        assert entry.details["celery_context"] == {"task": "t1"}
        assert entry.details["trace_id"] == "trace-42"
        assert entry.details["trace_id_full"] == "00-trace42-span01-01"

    def test_synced_marker_is_not_folded_into_details(self):
        """The retired ``synced`` marker never reaches a ledger row, even when
        an entry written before its removal still carries it."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(synced=False))

        assert "synced" not in entry.details

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

        assert entry.action is AuditAction.RETRY_EXHAUSTED

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


# =============================================================================
# Action derivation — the four resolution rules
# =============================================================================


class TestNativeActionDerivation:
    """``_derive_native_action()``: explicit, enum-by-value, enum-by-name,
    normalised verbatim — first match wins.

    A static event-type -> action table cannot see the payload, so it would
    file an operator's force-open as an automatic one; it would also need a
    hand-authored edit for every new event type. The derivation replaces it.
    """

    def test_explicit_action_wins_over_the_event_type(self):
        """Rule 1. The writer resolves the action when the payload — not the
        event type — decides it (a manual force-open and an automatic one
        share one event type), and that decision is final."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(event_type="CB_STATE_CHANGE", action="cb_force_open")
        )

        assert entry.action is AuditAction.CB_FORCE_OPEN

    def test_explicit_action_with_no_enum_member_is_kept_verbatim(self):
        """Rule 1, degraded: an explicit action outside the enum is still the
        payload's decision, so it is recorded rather than discarded back to
        the event type."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(event_type="CB_STATE_CHANGE", action="cb_quarantined")
        )

        assert entry.action == "cb_quarantined"
        assert not isinstance(entry.action, AuditAction)

    @pytest.mark.parametrize(
        "explicit",
        [None, "", 0, False],
        ids=["none", "empty-string", "zero", "false"],
    )
    def test_an_empty_explicit_action_falls_through_to_derivation(self, explicit):
        """Boundary: a falsy ``action`` means "not supplied" rather than "the
        action is blank" — recording an empty action would erase the event
        type the payload does carry."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(event_type="cb_force_open", action=explicit)
        )

        assert entry.action is AuditAction.CB_FORCE_OPEN

    def test_event_type_that_is_an_enum_value_becomes_that_member(self):
        """Rule 2."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(event_type="cb_auto_close", action=None)
        )

        assert entry.action is AuditAction.CB_AUTO_CLOSE

    def test_event_type_that_is_an_enum_name_becomes_that_member(self):
        """Rule 3: an upper-cased native event type misses the (lowercase)
        enum values but matches an enum *name*, so one vocabulary — not two
        casings of it — reaches the ledger."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(event_type="GOVERNANCE_BLOCKED", action=None)
        )

        assert entry.action is AuditAction.GOVERNANCE_BLOCKED

    def test_event_type_with_no_enum_member_is_normalised(self):
        """Rule 4: no member exists, so the recorded string is kept — cased
        like the enum so the ledger reads consistently."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(event_type="POOL_LEAK_CLOSED", action=None)
        )

        assert entry.action == "pool_leak_closed"
        assert not isinstance(entry.action, AuditAction)

    def test_absent_event_type_derives_an_empty_action(self):
        """Totality at the bottom of the ladder: nothing to derive from
        yields the empty string rather than a raise."""
        entry = AuditEntry.from_wal_dict({"event_type": ""})

        assert entry.action == ""

    @pytest.mark.parametrize(
        "event_type",
        [123, ["CB_STATE_CHANGE"], {"k": "v"}, None],
        ids=["int", "list", "dict", "none"],
    )
    def test_a_non_string_event_type_is_stringified_not_raised(self, event_type):
        """A corrupted WAL row must not become a poison entry stalling the
        sync cursor: ``.upper()`` on a non-string would raise inside the
        drain, so the value is stringified instead."""
        entry = AuditEntry.from_wal_dict(
            {"event_type": event_type, "record_id": "audit-x"}
        )

        assert isinstance(entry.action, str)
        assert isinstance(entry, AuditEntry)


# =============================================================================
# Native field map — the columns a ledger row needs
# =============================================================================


class TestNativeWalFieldMap:
    """The converter fills the fields a drained row would otherwise leave
    blank: target type, service, reason, context type, full traceparent."""

    # --- target_type, with the request path's fallback rule ------------------

    def test_explicit_target_type_is_used(self):
        """The writer passes a canonical value where one exists."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(target_type="circuit_breaker", source="CircuitBreaker")
        )

        assert entry.target_type == "circuit_breaker"

    def test_target_type_falls_back_to_source(self):
        """The rule the request-buffer path already applies, so a ledger row
        never records ``"unknown"`` for a target the payload names."""
        data = _native_wal_entry(source="PoolWatchdog")
        data.pop("target_type", None)

        entry = AuditEntry.from_wal_dict(data)

        assert entry.target_type == "PoolWatchdog"

    def test_target_type_absent_with_no_source_stays_none(self):
        """Neither present: the field is left unset rather than invented."""
        entry = AuditEntry.from_wal_dict({"event_type": "X"})

        assert entry.target_type is None

    def test_empty_target_type_falls_back_to_source(self):
        """Boundary: an empty string means "not named" rather than a named
        empty type."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(target_type="", source="CircuitBreaker")
        )

        assert entry.target_type == "CircuitBreaker"

    # --- the remaining newly-mapped columns ----------------------------------

    def test_service_name_is_mapped(self):
        """Without this the ledger cannot attribute a row to a service."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(service_name="payments"))

        assert entry.service_name == "payments"

    def test_reason_is_mapped(self):
        """The reason column is what an auditor reads to understand a row."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(reason="operator force"))

        assert entry.reason == "operator force"

    def test_context_type_is_mapped(self):
        """A drained row keeps the entry point it was emitted from."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(context_type="cli"))

        assert entry.context_type is ContextType.CLI

    @pytest.mark.parametrize(
        "raw",
        ["not-a-context", "", None, 7],
        ids=["unknown-string", "empty", "none", "non-string"],
    )
    def test_an_unmappable_context_type_degrades_to_unknown(self, raw):
        """Totality again: an unrecognised value must not raise inside the
        drain, and must not silently claim a context the event never had."""
        entry = AuditEntry.from_wal_dict(_native_wal_entry(context_type=raw))

        assert entry.context_type is ContextType.UNKNOWN

    def test_absent_context_type_is_unknown(self):
        """The default when the writer recorded none."""
        data = _native_wal_entry()
        data.pop("context_type", None)

        entry = AuditEntry.from_wal_dict(data)

        assert entry.context_type is ContextType.UNKNOWN

    def test_full_traceparent_reaches_the_row(self):
        """The short id alone cannot join a row to a distributed trace, so the
        full traceparent is carried alongside it."""
        entry = AuditEntry.from_wal_dict(
            _native_wal_entry(
                trace_id="trace-001",
                trace_id_full="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            )
        )

        assert entry.details["trace_id"] == "trace-001"
        assert entry.details["trace_id_full"] == (
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        )
