"""Unit tests for ``baldur.utils.domain_validation`` (impl doc 545).

Covers:
- ``validate_and_normalize_domain`` valid + invalid sets, parametrized over
  every ``DomainRejectReason`` enum member.
- ``DomainValidationError.extra_context()`` payload contract.
- ``DomainRejectReason`` JSON-serializable str-enum contract.
- ``FALLBACK_DOMAIN`` single-source-of-truth re-bind into
  ``baldur.metrics.registry._FALLBACK_DOMAIN``.
- ``MAX_DOMAIN_LENGTH`` boundary just-pass / just-fail.

Reference:
    545 (D1, D3, D7)
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from baldur.core.exceptions import DomainValidationError
from baldur.utils.domain_validation import (
    FALLBACK_DOMAIN,
    MAX_DOMAIN_LENGTH,
    DomainRejectReason,
    validate_and_normalize_domain,
)


class TestDomainValidationContract:
    """Hardcoded values from 545 D1/D3/D7 — the public contract."""

    def test_fallback_domain_value(self):
        assert FALLBACK_DOMAIN == "OTHER_DOMAIN"

    def test_max_domain_length_value(self):
        assert MAX_DOMAIN_LENGTH == 64

    @pytest.mark.parametrize(
        ("valid_domain", "expected_normalized"),
        [
            ("payment", "payment"),
            ("payment.charge", "payment.charge"),
            ("auth.verify_password", "auth.verify_password"),
            ("Payment.Charge", "payment.charge"),
            ("PAYMENT", "payment"),
            ("throttle_rejection", "throttle_rejection"),
            ("svc.retry", "svc.retry"),
            ("cache.dlq_explicit", "cache.dlq_explicit"),
            ("payment.cat56_3way", "payment.cat56_3way"),
            ("a", "a"),
            ("a" * MAX_DOMAIN_LENGTH, "a" * MAX_DOMAIN_LENGTH),
        ],
    )
    def test_dotted_segmented_accepted(self, valid_domain, expected_normalized):
        assert validate_and_normalize_domain(valid_domain) == expected_normalized

    @pytest.mark.parametrize(
        "valid_domain",
        [
            "region.1_primary",
            "payment.tier2",
            "auth.2fa",
            "cache.30s_ttl",
        ],
    )
    def test_sub_segment_digit_start(self, valid_domain):
        """D3 sub-segment relaxation: digit/underscore-start permitted."""
        assert validate_and_normalize_domain(valid_domain) == valid_domain.lower()

    @pytest.mark.parametrize(
        ("bad_input", "expected_reason"),
        [
            # NOT_STRING
            (None, DomainRejectReason.NOT_STRING),
            (123, DomainRejectReason.NOT_STRING),
            (b"bytes", DomainRejectReason.NOT_STRING),
            (["payment"], DomainRejectReason.NOT_STRING),
            # EMPTY
            ("", DomainRejectReason.EMPTY),
            ("   ", DomainRejectReason.EMPTY),
            ("\t\n", DomainRejectReason.EMPTY),
            # TOO_LONG — 65 chars
            ("a" * (MAX_DOMAIN_LENGTH + 1), DomainRejectReason.TOO_LONG),
            # INVALID_CHARSET
            ("invalid name!", DomainRejectReason.INVALID_CHARSET),
            ("payment-charge", DomainRejectReason.INVALID_CHARSET),
            ("1payment", DomainRejectReason.INVALID_CHARSET),
            ("_payment", DomainRejectReason.INVALID_CHARSET),
            (".payment", DomainRejectReason.INVALID_CHARSET),
            ("payment.", DomainRejectReason.INVALID_CHARSET),
        ],
    )
    def test_invalid_inputs_raise_with_typed_reason(self, bad_input, expected_reason):
        """Every reject path carries the matching ``DomainRejectReason``."""
        with pytest.raises(DomainValidationError) as exc_info:
            validate_and_normalize_domain(bad_input)
        assert exc_info.value.reason is expected_reason


class TestDomainValidationBoundary:
    """Boundary analysis for length / first-segment-anchor rules."""

    def test_length_just_at_cap_passes(self):
        domain = "a" * MAX_DOMAIN_LENGTH
        assert validate_and_normalize_domain(domain) == domain

    def test_length_one_over_cap_rejected(self):
        domain = "a" * (MAX_DOMAIN_LENGTH + 1)
        with pytest.raises(DomainValidationError) as exc_info:
            validate_and_normalize_domain(domain)
        assert exc_info.value.reason is DomainRejectReason.TOO_LONG

    def test_leading_anchor_rejects_uuid(self):
        """The canonical buggy-caller shape (``str(uuid4())``) must be rejected.

        UUIDs start with a hex digit (`5` etc.) — the leading ``[a-z]`` anchor
        rejects them structurally, NOT via the hyphen alone (so digit-only
        UUID hex forms are also blocked).
        """
        with pytest.raises(DomainValidationError) as exc_info:
            validate_and_normalize_domain(str(uuid4()))
        assert exc_info.value.reason is DomainRejectReason.INVALID_CHARSET

    def test_leading_anchor_rejects_uuid_hex(self):
        """Digit-start hex UUID-shaped strings — must reject via leading anchor.

        ``uuid.uuid4().hex`` is non-deterministic (62.5% digit start, 37.5%
        a-f start). The leading ``[a-z]`` anchor structurally rejects the
        digit-start case — the canonical buggy-caller shape against which
        545 D3 was sized. The doc's broader claim that the anchor blocks
        every ``uuid.uuid4().hex`` shape is imprecise: an a-f-start hex
        string passes the regex (charset is hex-subset of ``[a-z0-9_]``),
        relying instead on the 64-char length cap and downstream uses.
        """
        # Fixed digit-start hex string — deterministic test input.
        hex_domain = "5e07c8ab3f4e1d2c5e07c8ab3f4e1d2c"
        with pytest.raises(DomainValidationError) as exc_info:
            validate_and_normalize_domain(hex_domain)
        assert exc_info.value.reason is DomainRejectReason.INVALID_CHARSET


class TestDomainValidationErrorContract:
    """``DomainValidationError`` is the raised type for every reject path."""

    def test_inherits_from_baldur_error(self):
        from baldur.core.exceptions import BaldurError

        assert issubclass(DomainValidationError, BaldurError)

    def test_extra_context_carries_original_and_reason_value(self):
        with pytest.raises(DomainValidationError) as exc_info:
            validate_and_normalize_domain("invalid!")
        err = exc_info.value
        ctx = err.extra_context()
        assert ctx["original_domain"] == "invalid!"
        assert ctx["reason"] == DomainRejectReason.INVALID_CHARSET.value

    def test_extra_context_emits_string_value_not_repr(self):
        """Per 545 D7: ``(str, Enum).value`` is JSON-safe, not the repr."""
        with pytest.raises(DomainValidationError) as exc_info:
            validate_and_normalize_domain(None)
        ctx = exc_info.value.extra_context()
        json.dumps(ctx)
        assert ctx["reason"] == "not_string"

    def test_not_string_carries_repr_in_original(self):
        """``original_domain`` is ``repr(domain)`` when the input is not a str."""
        with pytest.raises(DomainValidationError) as exc_info:
            validate_and_normalize_domain(123)
        assert exc_info.value.original_domain == repr(123)


class TestDomainRejectReasonContract:
    """``DomainRejectReason`` is a (str, Enum) with JSON-serializable values."""

    def test_is_str_subclass(self):
        for reason in DomainRejectReason:
            assert isinstance(reason, str)

    def test_member_count_is_four(self):
        assert len(DomainRejectReason) == 4

    @pytest.mark.parametrize(
        ("member", "expected_value"),
        [
            (DomainRejectReason.TOO_LONG, "too_long"),
            (DomainRejectReason.EMPTY, "empty"),
            (DomainRejectReason.INVALID_CHARSET, "invalid_charset"),
            (DomainRejectReason.NOT_STRING, "not_string"),
        ],
    )
    def test_member_values(self, member, expected_value):
        assert member.value == expected_value
        assert member == expected_value

    def test_json_roundtrip(self):
        payload = {r.name: r.value for r in DomainRejectReason}
        roundtrip = json.loads(json.dumps(payload))
        assert roundtrip == payload


class TestFallbackDomainSingleSourceContract:
    """545 D1: ``metrics.registry._FALLBACK_DOMAIN`` re-binds the constant."""

    def test_metrics_registry_rebinds_fallback(self):
        from baldur.metrics import registry

        assert registry._FALLBACK_DOMAIN == FALLBACK_DOMAIN
        # Same string literal — rebind keeps a single source of truth.
        assert registry._FALLBACK_DOMAIN is FALLBACK_DOMAIN
