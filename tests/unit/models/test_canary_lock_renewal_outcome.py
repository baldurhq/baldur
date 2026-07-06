"""Contract tests for LockRenewalOutcome (623 D9).

The enum is the OSS-tier value type shared across the watchdog↔service config-
lock renewal boundary, so both the OSS watchdog and the PRO service import it
without a private-boundary crossing. These pin the wire values and membership.
"""

from __future__ import annotations

import json
from enum import Enum

from baldur.models.canary import LockRenewalOutcome


class TestLockRenewalOutcomeContract:
    """LockRenewalOutcome string values, (str, Enum) base, and membership."""

    def test_string_values(self):
        assert LockRenewalOutcome.RENEWED.value == "renewed"
        assert LockRenewalOutcome.REACQUIRED.value == "reacquired"
        assert LockRenewalOutcome.CONFLICT.value == "conflict"
        assert LockRenewalOutcome.FAILED.value == "failed"
        assert LockRenewalOutcome.SKIPPED.value == "skipped"

    def test_is_str_enum_for_json_serialization(self):
        assert issubclass(LockRenewalOutcome, str)
        assert issubclass(LockRenewalOutcome, Enum)

    def test_json_serializable_as_plain_string(self):
        assert json.dumps(LockRenewalOutcome.RENEWED) == '"renewed"'

    def test_membership_is_exactly_five_outcomes(self):
        assert {o.value for o in LockRenewalOutcome} == {
            "renewed",
            "reacquired",
            "conflict",
            "failed",
            "skipped",
        }

    def test_declared_in_module_all(self):
        from baldur.models import canary

        assert "LockRenewalOutcome" in canary.__all__
