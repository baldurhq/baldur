"""Unit tests for ``AuditEntry.from_dict()`` (#416 D20).

The H1 ``AuditEntry`` dataclass gained a ``from_dict()`` classmethod
that closes the hidden runtime bug in ``ResilientContinuousAuditRecorder``
(``services/audit/resilient_recorder.py``) — the recorder enriches
entries with ``integrity``, ``checksum`` and ``audit_id`` keys, and
``from_dict()`` must preserve those forensic fields under ``details``.

Covers:
- Round-trip safety (``to_dict() → from_dict()`` preserves all
  standard fields).
- Unknown keys overflow into ``details`` (forward-compat per the
  docstring).
- Action enum vs raw string fallback.
- Timestamp ISO 8601 round-trip including the ``Z`` suffix.
- ``ContextType`` reconstruction.
- ``integrity`` / ``checksum`` / ``audit_id`` preserved verbatim
  (the original D20 motivation).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    ContextType,
)

# =============================================================================
# Contract — exact field round-trip
# =============================================================================


class TestAuditEntryFromDictContract:
    """Hardcoded round-trip checks against design-doc field semantics."""

    def test_known_fields_round_trip(self):
        """Every standard field survives ``to_dict() → from_dict()``."""
        original = AuditEntry(
            action=AuditAction.CONFIG_CHANGE,
            actor_id="alice",
            actor_type="user",
            actor_roles=["admin", "auditor"],
            context_type=ContextType.REQUEST,
            target_type="circuit_breaker",
            target_id="payment",
            service_name="payment-service",
            domain="payment",
            reason="manual override",
            details={"key": "value"},
            success=False,
            error_message="boom",
        )

        rebuilt = AuditEntry.from_dict(original.to_dict())

        assert rebuilt.action == AuditAction.CONFIG_CHANGE
        assert rebuilt.actor_id == "alice"
        assert rebuilt.actor_type == "user"
        assert rebuilt.actor_roles == ["admin", "auditor"]
        assert rebuilt.context_type == ContextType.REQUEST
        assert rebuilt.target_type == "circuit_breaker"
        assert rebuilt.target_id == "payment"
        assert rebuilt.service_name == "payment-service"
        assert rebuilt.domain == "payment"
        assert rebuilt.reason == "manual override"
        assert rebuilt.details == {"key": "value"}
        assert rebuilt.success is False
        assert rebuilt.error_message == "boom"

    def test_action_string_falls_back_to_str(self):
        """Unknown action strings remain as raw ``str`` (not raised)."""
        rebuilt = AuditEntry.from_dict({"action": "totally_custom_action"})

        assert rebuilt.action == "totally_custom_action"
        assert not isinstance(rebuilt.action, AuditAction)

    def test_context_type_string_falls_back_to_unknown(self):
        """Unknown context_type strings degrade to ``ContextType.UNKNOWN``."""
        rebuilt = AuditEntry.from_dict(
            {"action": "config_change", "context_type": "no_such_context"}
        )

        assert rebuilt.context_type == ContextType.UNKNOWN

    def test_default_success_true_when_missing(self):
        """``success`` defaults to True when absent (parity with dataclass)."""
        rebuilt = AuditEntry.from_dict({"action": "config_change"})

        assert rebuilt.success is True

    def test_default_actor_type_system_when_missing(self):
        """Missing ``actor_type`` falls back to ``"system"`` per dataclass."""
        rebuilt = AuditEntry.from_dict({"action": "config_change"})

        assert rebuilt.actor_type == "system"


# =============================================================================
# Behavior — D20 forensic field overflow into details
# =============================================================================


class TestAuditEntryFromDictBehavior:
    """D20 — forward-compat overflow of unknown keys into ``details``."""

    def test_integrity_preserved_in_details(self):
        """``integrity`` (added by ``HashChainFileAuditLogAdapter``) lands in
        details — the D20 motivation."""
        data = {
            "action": "config_change",
            "actor_id": "alice",
            "details": {"existing": "field"},
            "integrity": {
                "sequence": 5,
                "previous_hash": "abc",
                "current_hash": "def",
            },
        }

        rebuilt = AuditEntry.from_dict(data)

        assert rebuilt.details["existing"] == "field"
        assert rebuilt.details["integrity"] == {
            "sequence": 5,
            "previous_hash": "abc",
            "current_hash": "def",
        }

    def test_checksum_and_audit_id_preserved_in_details(self):
        """``ResilientContinuousAuditRecorder`` adds ``checksum`` + ``audit_id``."""
        data = {
            "action": "config_change",
            "checksum": "sha256:abcdef",
            "audit_id": "a-001",
        }

        rebuilt = AuditEntry.from_dict(data)

        assert rebuilt.details["checksum"] == "sha256:abcdef"
        assert rebuilt.details["audit_id"] == "a-001"

    def test_unknown_fields_do_not_collide_with_known_details(self):
        """Existing ``details`` is preserved alongside overflow fields."""
        data = {
            "action": "config_change",
            "details": {"a": 1, "b": 2},
            "audit_id": "a-001",
        }

        rebuilt = AuditEntry.from_dict(data)

        assert rebuilt.details["a"] == 1
        assert rebuilt.details["b"] == 2
        assert rebuilt.details["audit_id"] == "a-001"

    def test_action_enum_value_round_trips(self):
        """An ``AuditAction`` enum stored as ``.value`` rebuilds as enum."""
        original = AuditEntry(action=AuditAction.CB_FORCE_OPEN)
        data = original.to_dict()

        rebuilt = AuditEntry.from_dict(data)

        assert rebuilt.action == AuditAction.CB_FORCE_OPEN
        assert isinstance(rebuilt.action, AuditAction)

    def test_timestamp_iso_round_trip(self):
        """Timestamp is preserved across ISO 8601 round-trip."""
        ts = datetime(2026, 4, 9, 12, 34, 56, tzinfo=UTC)
        data = AuditEntry(action=AuditAction.CONFIG_CHANGE, timestamp=ts).to_dict()

        rebuilt = AuditEntry.from_dict(data)

        assert rebuilt.timestamp == ts

    def test_timestamp_z_suffix_parses(self):
        """ISO 8601 with trailing ``Z`` is normalized to ``+00:00``."""
        data = {
            "action": "config_change",
            "timestamp": "2026-04-09T12:34:56Z",
        }

        rebuilt = AuditEntry.from_dict(data)

        assert rebuilt.timestamp.tzinfo is not None
        assert rebuilt.timestamp.year == 2026
        assert rebuilt.timestamp.hour == 12

    def test_to_json_round_trip_via_from_dict(self):
        """``to_json() → json.loads → from_dict()`` rebuilds the entry."""
        import json

        original = AuditEntry(
            action=AuditAction.RETRY_EXHAUSTED,
            target_type="operation",
            target_id="charge",
            details={"attempt": 5, "max_attempts": 5},
            success=False,
        )
        rebuilt = AuditEntry.from_dict(json.loads(original.to_json()))

        assert rebuilt.action == AuditAction.RETRY_EXHAUSTED
        assert rebuilt.target_id == "charge"
        assert rebuilt.details["attempt"] == 5
        assert rebuilt.success is False


# =============================================================================
# Edge cases
# =============================================================================


class TestAuditEntryFromDictEdgeCases:
    """Edge case handling: missing fields, weird types, defaults."""

    def test_empty_details_default(self):
        """Empty/missing ``details`` produces an empty dict, not ``None``."""
        rebuilt = AuditEntry.from_dict({"action": "config_change"})

        assert rebuilt.details == {}
        assert isinstance(rebuilt.details, dict)

    def test_actor_roles_list_copy(self):
        """``actor_roles`` is copied (not aliased) into a new list."""
        roles = ["admin"]
        rebuilt = AuditEntry.from_dict(
            {"action": "config_change", "actor_roles": roles}
        )

        # Mutating the original must not affect the rebuilt entry.
        roles.append("auditor")
        assert rebuilt.actor_roles == ["admin"]

    def test_no_action_key_falls_back_to_empty_string(self):
        """Missing action degrades to ``""`` (still a valid str-typed action)."""
        rebuilt = AuditEntry.from_dict({})

        assert rebuilt.action == ""

    @pytest.mark.parametrize("falsy", [None, "", 0, False])
    def test_success_falsy_inputs_become_bool(self, falsy):
        """``success`` is coerced through ``bool()`` for safety."""
        rebuilt = AuditEntry.from_dict({"action": "x", "success": falsy})

        assert rebuilt.success is False

    def test_timestamp_invalid_string_falls_back_to_now(self):
        """An invalid ISO string raises (per ``fromisoformat`` contract)."""
        # The current implementation does not catch ValueError for malformed
        # ISO timestamps — assert today's behavior so a future "fall back to
        # utc_now" change is intentional and visible.
        with pytest.raises(ValueError):
            AuditEntry.from_dict(
                {"action": "config_change", "timestamp": "not-a-timestamp"}
            )
