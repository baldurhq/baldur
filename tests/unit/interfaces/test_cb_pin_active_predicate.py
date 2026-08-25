"""The expiry-aware manual-override predicate, in its three spellings.

The rule that decides whether an operator's override is still in force used
to be written out twice — once in the service's enforcement checks and once
in the memory adapter's storage primitives. Making the storage primitives ask
it inside their own lock hold turned that duplication into a correctness
hazard: a raw-flag reading blocks every automatic transition from reaching the
store for as long as a lapsed flag survives, because no primitive clears the
flag on an automatic transition and only one process per host runs the sweep
that does.

The rule now lives in one function taking the two fields, with a row-shaped
spelling next to the DTO and a service-side spelling that keeps accepting any
row carrying the fields. These tests pin the rule itself at its boundaries and
assert the three spellings cannot drift apart.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    manual_pin_is_active,
)
from baldur.services.circuit_breaker.manual_control import is_manual_pin_active
from baldur.utils.time import utc_now
from tests.factories.time_helpers import freeze_time

FROZEN_INSTANT = "2026-08-25 12:00:00"


# =============================================================================
# Contract — the rule as the design document states it
# =============================================================================


class TestPinActivePredicateContract:
    """flag truthy AND (expiry absent OR strictly after now)."""

    def test_flag_set_with_no_expiry_reads_active(self):
        # "Manually controlled, no lifetime" is permanently in force — the
        # reading the service layer relies on when it refuses to create one.
        assert manual_pin_is_active(True, None) is True

    def test_flag_set_with_future_expiry_reads_active(self):
        assert manual_pin_is_active(True, utc_now() + timedelta(minutes=5)) is True

    def test_flag_set_with_expiry_exactly_now_reads_inactive(self):
        # Boundary: the comparison is strictly greater-than, so an override
        # stops being honoured at the instant it promised, not after it.
        with freeze_time(FROZEN_INSTANT):
            assert manual_pin_is_active(True, utc_now()) is False

    def test_flag_set_one_microsecond_before_expiry_reads_active(self):
        # The other side of the same boundary.
        with freeze_time(FROZEN_INSTANT):
            expires_at = utc_now() + timedelta(microseconds=1)
            assert manual_pin_is_active(True, expires_at) is True

    def test_flag_set_with_past_expiry_reads_inactive(self):
        assert manual_pin_is_active(True, utc_now() - timedelta(seconds=1)) is False

    def test_flag_clear_with_future_expiry_reads_inactive(self):
        # The flag gates the whole rule: a stored expiry with no flag is not
        # an override.
        assert manual_pin_is_active(False, utc_now() + timedelta(minutes=5)) is False

    def test_flag_clear_with_no_expiry_reads_inactive(self):
        assert manual_pin_is_active(False, None) is False

    def test_returns_a_bool_for_a_truthy_non_bool_flag(self):
        # Adapters read the flag off a wire format (a SQL integer column, a
        # Redis string), so the predicate must normalize rather than leak the
        # stored value to a caller that does `is True`.
        assert manual_pin_is_active(1, None) is True


# =============================================================================
# Behavior — the three spellings agree over the whole matrix
# =============================================================================


def _matrix():
    """(flag, expiry_offset_seconds or None) covering every rule branch."""
    return [
        (True, None),
        (True, 300),
        (True, -300),
        (False, None),
        (False, 300),
        (False, -300),
    ]


class TestPinActiveSpellingAgreementBehavior:
    """``manual_pin_is_active`` / ``is_pin_active`` / ``is_manual_pin_active``.

    Three call sites ask the same question of the same fields. If they ever
    disagree, a storage primitive declines a write the service considers free
    to make (or the reverse), which is the drift the single definition exists
    to prevent.
    """

    @pytest.mark.parametrize(("flag", "offset"), _matrix())
    def test_row_predicate_matches_the_field_predicate(self, flag, offset):
        expires_at = None if offset is None else utc_now() + timedelta(seconds=offset)
        row = CircuitBreakerStateData(
            service_name="svc",
            manually_controlled=flag,
            manual_override_expires_at=expires_at,
        )

        assert row.is_pin_active() == manual_pin_is_active(flag, expires_at)

    @pytest.mark.parametrize(("flag", "offset"), _matrix())
    def test_service_enforcement_predicate_matches_the_field_predicate(
        self, flag, offset
    ):
        expires_at = None if offset is None else utc_now() + timedelta(seconds=offset)
        row = CircuitBreakerStateData(
            service_name="svc",
            manually_controlled=flag,
            manual_override_expires_at=expires_at,
        )

        assert is_manual_pin_active(row) == manual_pin_is_active(flag, expires_at)

    def test_service_enforcement_predicate_accepts_any_row_with_the_fields(self):
        # The service check is promised to a *shape*, not to
        # CircuitBreakerStateData — a Django model instance or a layer's own
        # DTO must keep working, which is why it reads the fields rather than
        # calling the dataclass's own method.
        class _ForeignRow:
            manually_controlled = True
            manual_override_expires_at = None

        assert is_manual_pin_active(_ForeignRow()) is True
