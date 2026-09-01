"""``resolve_stored_domain()`` — the one projection onto the stored domain form.

The DLQ store walks a ladder (validate, then retry through the label
canonicalization, then fall back) before deciding what name an entry is filed
under. Anything that later has to FIND those entries has to land on the same
answer, and re-deriving the ladder by hand is exactly what made captures
unreachable: plain validation raises on ``payment-api`` while the store quietly
files it as ``payment_api``, so a re-derived reader either crashes or searches
for a name nothing was ever stored under.

These cases pin the ladder's three landing zones — validated as-is,
canonicalization-recovered, and the unclassifiable bucket — plus the totality
that lets the store call it from inside an exception handler.
"""

from __future__ import annotations

import pytest

from baldur.utils.domain_validation import (
    FALLBACK_DOMAIN,
    MAX_DOMAIN_LENGTH,
    resolve_stored_domain,
)

# =============================================================================
# Behavior — projection ladder
# =============================================================================


class TestResolveStoredDomainBehavior:
    """Each input class lands on its documented projection."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Already canonical — validated on the first try, returned unchanged.
            pytest.param("payment", "payment", id="canonical"),
            pytest.param("payment_api", "payment_api", id="canonical_underscored"),
            pytest.param("payment.tier2", "payment.tier2", id="canonical_segmented"),
            # Case folding happens inside validation.
            pytest.param("PaymentAPI", "paymentapi", id="mixed_case"),
            # Hyphens fail validation and are recovered by canonicalization —
            # the divergence a re-derived reader gets wrong.
            pytest.param("payment-api", "payment_api", id="hyphenated"),
            pytest.param("Payment-API", "payment_api", id="mixed_case_hyphenated"),
            pytest.param("async-gw", "async_gw", id="hyphenated_short"),
            pytest.param("a b c", "a_b_c", id="spaced"),
            # A digit-leading name has no valid first segment either way.
            pytest.param("3ds-gateway", FALLBACK_DOMAIN, id="digit_leading"),
            # Empty / blank / non-string / over-length all reach the bucket.
            pytest.param("", FALLBACK_DOMAIN, id="empty"),
            pytest.param("   ", FALLBACK_DOMAIN, id="blank"),
            pytest.param(None, FALLBACK_DOMAIN, id="not_a_string"),
            pytest.param(123, FALLBACK_DOMAIN, id="not_a_string_number"),
            pytest.param(
                "a" * (MAX_DOMAIN_LENGTH + 1), FALLBACK_DOMAIN, id="over_length"
            ),
        ],
    )
    def test_projection_lands_on_the_stored_form(self, raw, expected):
        assert resolve_stored_domain(raw) == expected

    def test_max_length_boundary_passes(self):
        """A name exactly at the cap validates (over it is in the table above)."""
        at_cap = "a" * MAX_DOMAIN_LENGTH

        assert resolve_stored_domain(at_cap) == at_cap

    def test_a_resolved_name_is_idempotent(self):
        """The async outbox round-trip re-projects an already-stored domain;
        a second pass must not move it."""
        once = resolve_stored_domain("Payment-API")

        assert resolve_stored_domain(once) == once

    def test_the_bucket_verdict_is_terminal_not_a_name_to_reproject(self):
        """``FALLBACK_DOMAIN`` is the one output that does not round-trip: it
        is upper-case, so a second pass validates it into ``other_domain``.
        Pinned so a caller learns it here rather than by joining on a name
        nothing was stored under."""
        bucket = resolve_stored_domain("3ds-gateway")

        assert bucket == FALLBACK_DOMAIN
        assert resolve_stored_domain(bucket) != bucket

    def test_never_raises_on_hostile_input(self):
        """Total by construction — the store calls this from inside an
        exception handler, where a raise would drop the record entirely."""
        for hostile in (object(), b"bytes", 3.14, [], {}):
            assert isinstance(resolve_stored_domain(hostile), str)

    def test_fallback_bucket_is_not_a_service_identity(self):
        """Unrelated unresolvable names share one bucket, so a match on it
        proves nothing about which service an entry belongs to."""
        assert resolve_stored_domain("3ds-gateway") == resolve_stored_domain("")
        assert resolve_stored_domain("3ds-gateway") == FALLBACK_DOMAIN
