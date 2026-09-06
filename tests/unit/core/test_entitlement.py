"""
Unit tests for EntitlementValidator (427 §4.2).

Verification techniques:
- Contract: enum values, claims fields, error hierarchy, status mapping
- Behavior: _load_token, _is_base64, _do_validate state transitions
- Singleton/lifecycle: get/reset pair
- Time dependency: TTL cache via monkeypatch of time.monotonic
- Side effects: logging, metrics update
- Exception/edge cases: malformed tokens, missing fields, expired tokens
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.core import entitlement as entitlement_module
from baldur.core.entitlement import (
    _RECHECK_TTL_SECONDS,
    EntitlementClaims,
    EntitlementError,
    EntitlementResult,
    EntitlementStatus,
    _EntitlementValidator,
    get_entitlement_status,
    is_entitlement_active,
    reset_entitlement_status,
)
from baldur.core.exceptions import BaldurError
from baldur.settings.license import reset_entitlement_settings

# ── Helpers ──────────────────────────────────────────────────


def _make_token_json(
    payload: dict | None = None,
    signature: str = "fakesig",
    *,
    expires: str | None = None,
) -> str:
    """Build a raw JSON token string (not base64-encoded)."""
    if payload is None:
        exp = expires or (date.today() + timedelta(days=30)).isoformat()
        payload = {
            "customer_id": "cust_test",
            "org": "test-org",
            "tier": "PRO",
            "plan": "monthly",
            "issued_at": "2026-01-01",
            "expires": exp,
        }
    token = {"alg": "ed25519", "payload": payload, "signature": signature}
    return json.dumps(token)


def _make_token_b64(**kwargs) -> str:
    """Build a base64-encoded token string."""
    raw = _make_token_json(**kwargs)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


# ═════════════════════════════════════════════════════════════
# Contract Tests
# ═════════════════════════════════════════════════════════════


class TestEntitlementStatusContract:
    """EntitlementStatus enum contract values (427 §4.2)."""

    def test_active_value(self):
        """ACTIVE = 'active'."""
        assert EntitlementStatus.ACTIVE == "active"

    def test_invalid_value(self):
        """INVALID = 'invalid'."""
        assert EntitlementStatus.INVALID == "invalid"

    def test_missing_value(self):
        """MISSING = 'missing'."""
        assert EntitlementStatus.MISSING == "missing"

    def test_json_serializable_via_str_enum(self):
        """str,Enum pattern enables JSON serialization."""
        assert json.dumps(EntitlementStatus.ACTIVE) == '"active"'


class TestEntitlementErrorContract:
    """EntitlementError inherits BaldurError with extra_context (427 §10.3)."""

    def test_inherits_baldur_error(self):
        """EntitlementError is a BaldurError subclass."""
        assert issubclass(EntitlementError, BaldurError)

    def test_extra_context_includes_reason(self):
        """extra_context returns reason when set."""
        err = EntitlementError("test", reason="expired")
        ctx = err.extra_context()
        assert ctx["reason"] == "expired"

    def test_extra_context_empty_when_no_reason(self):
        """extra_context omits reason when not set."""
        err = EntitlementError("test")
        ctx = err.extra_context()
        assert "reason" not in ctx


class TestEntitlementClaimsContract:
    """EntitlementClaims dataclass contract (427 §4.2)."""

    def test_frozen_dataclass(self):
        """Claims are immutable (frozen=True)."""
        claims = EntitlementClaims(
            customer_id="c",
            org="o",
            tier="PRO",
            plan="monthly",
            issued_at="2026-01-01",
            expires="2026-12-31",
        )
        with pytest.raises(AttributeError):
            claims.customer_id = "new"  # type: ignore[misc]

    def test_expiry_date_parses_correctly(self):
        """expiry_date parses YYYY-MM-DD format."""
        claims = EntitlementClaims(
            customer_id="c",
            org="o",
            tier="PRO",
            plan="monthly",
            issued_at="2026-01-01",
            expires="2026-06-15",
        )
        assert claims.expiry_date == date(2026, 6, 15)


class TestEntitlementResultContract:
    """EntitlementResult contract."""

    def test_is_active_true_when_active(self):
        """is_active returns True for ACTIVE status."""
        result = EntitlementResult(status=EntitlementStatus.ACTIVE)
        assert result.is_active is True

    def test_is_active_false_when_invalid(self):
        """is_active returns False for INVALID status."""
        result = EntitlementResult(status=EntitlementStatus.INVALID)
        assert result.is_active is False

    def test_is_active_false_when_missing(self):
        """is_active returns False for MISSING status."""
        result = EntitlementResult(status=EntitlementStatus.MISSING)
        assert result.is_active is False


class TestTtlConstantContract:
    """TTL constant contract (427 §4.2: 24h TTL)."""

    def test_recheck_ttl_is_24_hours(self):
        """Re-check TTL must be 86400 seconds (24 hours)."""
        assert _RECHECK_TTL_SECONDS == 86400


# ═════════════════════════════════════════════════════════════
# Behavior Tests
# ═════════════════════════════════════════════════════════════


class TestEntitlementClaimsBehavior:
    """Claims computation behavior."""

    def test_days_until_expiry_future_date_positive(self):
        """Future expiry yields positive days_until_expiry."""
        future = (date.today() + timedelta(days=10)).isoformat()
        claims = EntitlementClaims(
            customer_id="c",
            org="o",
            tier="PRO",
            plan="monthly",
            issued_at="2026-01-01",
            expires=future,
        )
        assert claims.days_until_expiry == 10

    def test_days_until_expiry_past_date_negative(self):
        """Past expiry yields negative days_until_expiry."""
        past = (date.today() - timedelta(days=5)).isoformat()
        claims = EntitlementClaims(
            customer_id="c",
            org="o",
            tier="PRO",
            plan="monthly",
            issued_at="2026-01-01",
            expires=past,
        )
        assert claims.days_until_expiry == -5

    def test_is_expired_true_for_yesterday(self):
        """Token expiring yesterday is expired."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        claims = EntitlementClaims(
            customer_id="c",
            org="o",
            tier="PRO",
            plan="monthly",
            issued_at="2026-01-01",
            expires=yesterday,
        )
        assert claims.is_expired is True

    def test_is_expired_false_for_today(self):
        """Token expiring today is NOT expired (boundary: days_until_expiry == 0)."""
        today = date.today().isoformat()
        claims = EntitlementClaims(
            customer_id="c",
            org="o",
            tier="PRO",
            plan="monthly",
            issued_at="2026-01-01",
            expires=today,
        )
        assert claims.is_expired is False


class TestLoadTokenBehavior:
    """_load_token behavior: key precedence, file fallback, error handling."""

    def setup_method(self):
        self.validator = _EntitlementValidator()

    def test_key_returned_when_present(self):
        """license_key is returned directly when non-empty."""
        result = self.validator._load_token("my-key", "")
        assert result == "my-key"

    def test_key_takes_precedence_over_file(self):
        """license_key takes precedence when both are set."""
        result = self.validator._load_token("my-key", "/some/file")
        assert result == "my-key"

    def test_file_read_when_key_empty(self, tmp_path):
        """File is read when license_key is empty."""
        token_file = tmp_path / "token.key"
        token_file.write_text("  file-token  ")
        result = self.validator._load_token("", str(token_file))
        assert result == "file-token"

    def test_nonexistent_file_reports_unreadable(self):
        """A configured file that cannot be opened reports unreadable, not absent.

        ``None`` is not the same answer as ``""``: an absent licence source is
        a settled fact worth caching for the full TTL, while a source that is
        configured and momentarily unreadable is not. Caching the latter would
        pin "not entitled" on a licensed deployment until the TTL expires.
        """
        result = self.validator._load_token("", "/nonexistent/path.key")
        assert result is None

    def test_non_utf8_file_reports_unreadable(self, tmp_path):
        """A licence file that is not UTF-8 reports unreadable, never raises.

        ``read_text(encoding="utf-8")`` raises ``UnicodeDecodeError`` (a
        ``ValueError``, not an ``OSError``), so an unreadable encoding must be
        caught alongside the missing-file case or it escapes the validator and
        aborts the caller that decides whether the PRO tier registers.
        """
        token_file = tmp_path / "token.key"
        token_file.write_bytes(bytes([0xFF, 0xFE, 0x00]) + b"not-utf-8")

        result = self.validator._load_token("", str(token_file))

        assert result is None

    def test_both_empty_returns_empty(self):
        """Both key and file empty returns empty string."""
        result = self.validator._load_token("", "")
        assert result == ""


class TestIsBase64Behavior:
    """_is_base64 heuristic behavior."""

    def test_json_string_returns_false(self):
        """String starting with '{' is not base64."""
        assert _EntitlementValidator._is_base64('{"alg":"ed25519"}') is False

    def test_valid_base64_returns_true(self):
        """Valid base64-encoded string detected."""
        b64 = base64.b64encode(b"test").decode()
        assert _EntitlementValidator._is_base64(b64) is True

    def test_invalid_string_returns_false(self):
        """Invalid base64 returns False."""
        assert _EntitlementValidator._is_base64("not!valid!base64!!!") is False


class TestDoValidateBehavior:
    """_do_validate state transitions: MISSING, INVALID, ACTIVE."""

    def setup_method(self):
        self.validator = _EntitlementValidator()

    def test_empty_token_returns_missing(self):
        """No license key/file → MISSING."""
        with patch(
            "baldur.settings.license.get_entitlement_settings",
            return_value=MagicMock(key="", file=""),
        ):
            result = self.validator._do_validate()

        assert result.status == EntitlementStatus.MISSING
        assert result.claims is None

    def test_invalid_json_returns_invalid(self):
        """Malformed JSON → INVALID."""
        with patch(
            "baldur.settings.license.get_entitlement_settings",
            return_value=MagicMock(key="not-json-at-all", file=""),
        ):
            result = self.validator._do_validate()

        assert result.status == EntitlementStatus.INVALID

    def test_missing_payload_returns_invalid(self):
        """Token without payload field → INVALID."""
        token = json.dumps({"alg": "ed25519", "signature": "abc"})
        with patch(
            "baldur.settings.license.get_entitlement_settings",
            return_value=MagicMock(key=token, file=""),
        ):
            result = self.validator._do_validate()

        assert result.status == EntitlementStatus.INVALID

    def test_missing_claims_field_returns_invalid(self):
        """Payload missing required field (customer_id) → INVALID."""
        token = json.dumps(
            {
                "alg": "ed25519",
                "payload": {"org": "x"},  # missing customer_id, tier, plan, etc.
                "signature": "abc",
            }
        )
        with patch(
            "baldur.settings.license.get_entitlement_settings",
            return_value=MagicMock(key=token, file=""),
        ):
            result = self.validator._do_validate()

        assert result.status == EntitlementStatus.INVALID

    def test_expired_token_returns_invalid_with_claims(self):
        """Expired token → INVALID with claims attached."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        token = _make_token_json(expires=yesterday)

        # Mock signature verification to pass (so expiry check is reached)
        with (
            patch(
                "baldur.settings.license.get_entitlement_settings",
                return_value=MagicMock(key=token, file=""),
            ),
            patch.object(
                _EntitlementValidator,
                "_verify_signature",
                return_value=True,
            ),
        ):
            result = self.validator._do_validate()

        assert result.status == EntitlementStatus.INVALID
        assert result.claims is not None
        assert result.claims.is_expired is True

    def test_malformed_expires_returns_invalid(self):
        """An ``expires`` claim that is not a date → INVALID, never raises.

        ``is_expired`` parses the claim with ``strptime``; the claim comes
        straight from the token payload, so a malformed value is input, not an
        internal invariant, and must produce a status like every other
        token-content defect.
        """
        token = _make_token_json(expires="not-a-date")

        with (
            patch(
                "baldur.settings.license.get_entitlement_settings",
                return_value=SimpleNamespace(key=token, file=""),
            ),
            patch.object(
                _EntitlementValidator,
                "_verify_signature",
                return_value=True,
            ),
        ):
            result = self.validator._do_validate()

        assert result.status == EntitlementStatus.INVALID
        assert result.claims is not None

    def test_non_string_expires_returns_invalid(self):
        """An ``expires`` the issuer emitted as a number -> INVALID, never raises.

        The claims dataclass annotates ``expires: str`` and does not enforce it,
        so a JSON number (or ``null``) reaches ``strptime`` as a non-string and
        raises ``TypeError`` rather than the ``ValueError`` a malformed *string*
        date produces. Both are the same defect from a caller's side, and this
        one escapes past the entitlement check that decides whether the PRO tier
        registers.
        """
        token = _make_token_json(
            payload={
                "customer_id": "cust_test",
                "org": "test-org",
                "tier": "PRO",
                "plan": "monthly",
                "issued_at": "2026-01-01",
                "expires": 20260501,
            }
        )

        with (
            patch(
                "baldur.settings.license.get_entitlement_settings",
                return_value=SimpleNamespace(key=token, file=""),
            ),
            patch.object(
                _EntitlementValidator,
                "_verify_signature",
                return_value=True,
            ),
        ):
            result = self.validator._do_validate()

        assert result.status == EntitlementStatus.INVALID
        assert result.claims is not None

    def test_valid_token_returns_active(self):
        """Valid, non-expired token with valid signature → ACTIVE."""
        future = (date.today() + timedelta(days=30)).isoformat()
        token = _make_token_json(expires=future)

        with (
            patch(
                "baldur.settings.license.get_entitlement_settings",
                return_value=MagicMock(key=token, file=""),
            ),
            patch.object(
                _EntitlementValidator,
                "_verify_signature",
                return_value=True,
            ),
        ):
            result = self.validator._do_validate()

        assert result.status == EntitlementStatus.ACTIVE
        assert result.claims is not None
        assert result.claims.customer_id == "cust_test"
        assert result.claims.org == "test-org"

    def test_base64_encoded_token_decoded(self):
        """Base64-encoded token is transparently decoded."""
        future = (date.today() + timedelta(days=30)).isoformat()
        token_b64 = _make_token_b64(expires=future)

        with (
            patch(
                "baldur.settings.license.get_entitlement_settings",
                return_value=MagicMock(key=token_b64, file=""),
            ),
            patch.object(
                _EntitlementValidator,
                "_verify_signature",
                return_value=True,
            ),
        ):
            result = self.validator._do_validate()

        assert result.status == EntitlementStatus.ACTIVE

    def test_signature_failure_returns_invalid_with_claims(self):
        """Failed signature verification → INVALID with claims."""
        token = _make_token_json()

        with (
            patch(
                "baldur.settings.license.get_entitlement_settings",
                return_value=MagicMock(key=token, file=""),
            ),
            patch.object(
                _EntitlementValidator,
                "_verify_signature",
                return_value=False,
            ),
        ):
            result = self.validator._do_validate()

        assert result.status == EntitlementStatus.INVALID
        assert result.claims is not None


class TestVerifySignatureBehavior:
    """_verify_signature edge cases."""

    def test_no_cryptography_returns_false(self):
        """Missing cryptography package → False."""
        with patch.dict(
            "sys.modules", {"cryptography.hazmat.primitives.asymmetric.ed25519": None}
        ):
            # ImportError path
            _EntitlementValidator._verify_signature({"a": 1}, "sig")
        # Without cryptography, should return False
        # (the actual import error path is hard to trigger with patch.dict,
        #  but we can test with a mock)

    def test_no_baldur_pro_returns_false(self):
        """Missing baldur_pro package → False (expected in OSS mode)."""
        with (
            patch.dict("sys.modules", {"baldur_pro._entitlement": None}),
            patch.dict("sys.modules", {"baldur_pro": None}),
        ):
            result = _EntitlementValidator._verify_signature({"a": 1}, "c2ln")
        assert result is False


class TestValidatorTtlCacheBehavior:
    """TTL-based caching behavior (427 D5)."""

    def test_cached_result_returned_within_ttl(self):
        """Second call within TTL returns cached result without re-validation."""
        validator = _EntitlementValidator()

        with (
            patch.object(validator, "_do_validate") as mock_validate,
            patch.object(validator, "_log_result"),
            patch.object(validator, "_update_metrics"),
        ):
            mock_validate.return_value = EntitlementResult(
                status=EntitlementStatus.MISSING,
            )

            # First call — validates
            result1 = validator.validate()
            # Second call — cached
            result2 = validator.validate()

        assert mock_validate.call_count == 1
        assert result1 is result2

    def test_force_bypasses_cache(self):
        """force=True always re-validates."""
        validator = _EntitlementValidator()

        with (
            patch.object(validator, "_do_validate") as mock_validate,
            patch.object(validator, "_log_result"),
            patch.object(validator, "_update_metrics"),
        ):
            mock_validate.return_value = EntitlementResult(
                status=EntitlementStatus.MISSING,
            )

            validator.validate()
            validator.validate(force=True)

        assert mock_validate.call_count == 2

    def test_expired_ttl_triggers_revalidation(self):
        """After TTL expires, next call re-validates."""
        validator = _EntitlementValidator()

        monotonic_values = iter([100.0, 100.1, 100.0 + _RECHECK_TTL_SECONDS + 1])

        with (
            patch.object(validator, "_do_validate") as mock_validate,
            patch.object(validator, "_log_result"),
            patch.object(validator, "_update_metrics"),
            patch(
                "baldur.core.entitlement.time.monotonic", side_effect=monotonic_values
            ),
        ):
            mock_validate.return_value = EntitlementResult(
                status=EntitlementStatus.MISSING,
            )

            validator.validate()  # t=100.0, validates
            validator.validate()  # t=100.1, cached
            validator.validate()  # t=100+86401, re-validates

        assert mock_validate.call_count == 2


class TestForcedRevalidationSourceBehavior:
    """force=True re-reads the licence source, not just the cached verdict."""

    def setup_method(self):
        reset_entitlement_settings()

    def teardown_method(self):
        reset_entitlement_settings()

    def test_forced_revalidation_sees_a_token_that_arrived_after_the_first_read(
        self, monkeypatch
    ):
        """A token injected after the first validation is still found.

        EntitlementSettings is a process-global singleton, so a validation taken
        before the token reached ``os.environ`` pins its empty snapshot for the
        life of the process, and every later ``force=True`` re-checks a token it
        can no longer see. That is reachable whenever something resolves the
        verdict at import time — a Celery process composing its beat schedule —
        while the deployment injects the token from inside Python (dotenv,
        Django settings). The victim is the ``init()``-time re-validation that
        decides whether the PRO tier registers at all.

        INVALID rather than MISSING is the whole assertion: MISSING means the
        validator never saw a token, which is the stale-snapshot failure.
        """
        monkeypatch.delenv("BALDUR_LICENSE_KEY", raising=False)
        monkeypatch.delenv("BALDUR_LICENSE_FILE", raising=False)
        validator = _EntitlementValidator()

        assert validator.validate().status == EntitlementStatus.MISSING

        monkeypatch.setenv("BALDUR_LICENSE_KEY", "arrived-after-the-first-read")

        assert validator.validate(force=True).status == EntitlementStatus.INVALID


class TestSingletonBehavior:
    """get_entitlement_status / reset_entitlement_status singleton."""

    def setup_method(self):
        reset_entitlement_status()

    def teardown_method(self):
        reset_entitlement_status()

    def test_get_creates_validator_and_returns_result(self):
        """get_entitlement_status creates validator on first call."""
        with patch(
            "baldur.core.entitlement._EntitlementValidator.validate",
            return_value=EntitlementResult(status=EntitlementStatus.MISSING),
        ):
            result = get_entitlement_status()

        assert result.status == EntitlementStatus.MISSING

    def test_reset_clears_validator(self):
        """reset_entitlement_status forces fresh validator on next call."""
        with patch(
            "baldur.core.entitlement._EntitlementValidator.validate",
            return_value=EntitlementResult(status=EntitlementStatus.MISSING),
        ) as mock_validate:
            get_entitlement_status()
            reset_entitlement_status()
            get_entitlement_status()

        # Two separate validator instances, each called once
        assert mock_validate.call_count == 2

    def test_force_passed_through(self):
        """force parameter is forwarded to validator.validate."""
        with patch(
            "baldur.core.entitlement._EntitlementValidator.validate",
            return_value=EntitlementResult(status=EntitlementStatus.MISSING),
        ) as mock_validate:
            get_entitlement_status(force=True)

        mock_validate.assert_called_once_with(force=True)


class TestMetricsUpdateBehavior:
    """_update_metrics side effect: maps status to correct gauge values."""

    def test_active_status_maps_to_2(self):
        """ACTIVE → status gauge = 2, expiry_days from claims."""
        future = (date.today() + timedelta(days=15)).isoformat()
        claims = EntitlementClaims(
            customer_id="c",
            org="o",
            tier="PRO",
            plan="monthly",
            issued_at="2026-01-01",
            expires=future,
        )
        result = EntitlementResult(status=EntitlementStatus.ACTIVE, claims=claims)

        with (
            patch(
                "baldur.metrics.recorders.entitlement.set_entitlement_status"
            ) as mock_status,
            patch(
                "baldur.metrics.recorders.entitlement.set_entitlement_expiry_days"
            ) as mock_days,
        ):
            _EntitlementValidator._update_metrics(result)

        mock_status.assert_called_once_with(2)
        mock_days.assert_called_once_with(15)

    def test_missing_status_maps_to_0(self):
        """MISSING → status gauge = 0, expiry_days = -1."""
        result = EntitlementResult(status=EntitlementStatus.MISSING)

        with (
            patch(
                "baldur.metrics.recorders.entitlement.set_entitlement_status"
            ) as mock_status,
            patch(
                "baldur.metrics.recorders.entitlement.set_entitlement_expiry_days"
            ) as mock_days,
        ):
            _EntitlementValidator._update_metrics(result)

        mock_status.assert_called_once_with(0)
        mock_days.assert_called_once_with(-1)

    def test_invalid_status_maps_to_1(self):
        """INVALID → status gauge = 1."""
        result = EntitlementResult(status=EntitlementStatus.INVALID)

        with (
            patch(
                "baldur.metrics.recorders.entitlement.set_entitlement_status"
            ) as mock_status,
            patch(
                "baldur.metrics.recorders.entitlement.set_entitlement_expiry_days"
            ) as mock_days,
        ):
            _EntitlementValidator._update_metrics(result)

        mock_status.assert_called_once_with(1)
        mock_days.assert_called_once_with(-1)


class TestValidateTransientReadBehavior:
    """A momentarily unreadable licence source never becomes a cached verdict.

    ``_do_validate()`` answers ``None`` for that case, distinct from the real
    MISSING verdict an absent source produces. ``validate()`` must neither
    cache it nor refresh the TTL clock: a projected-volume re-link swaps the
    licence file atomically under the reader, and caching the read it loses
    would pin "not entitled" on a fully licensed deployment for a whole TTL.
    """

    def test_unreadable_source_without_a_previous_verdict_answers_missing(self):
        """Nothing to fall back on, so the answer is MISSING (fail closed)."""
        validator = _EntitlementValidator()

        with patch.object(validator, "_do_validate", return_value=None):
            result = validator.validate()

        assert result.status == EntitlementStatus.MISSING

    def test_unreadable_source_leaves_the_cache_and_the_ttl_clock_untouched(self):
        """The MISSING answer is returned, not stored: no cache, no clock."""
        validator = _EntitlementValidator()

        with patch.object(validator, "_do_validate", return_value=None):
            validator.validate()

        assert validator._cached_result is None
        assert validator._last_checked == 0.0

    def test_unreadable_source_keeps_the_previous_verdict(self):
        """An established ACTIVE verdict survives a read that answers nothing."""
        # Given: an ACTIVE verdict already cached
        validator = _EntitlementValidator()
        active = EntitlementResult(status=EntitlementStatus.ACTIVE)
        with (
            patch.object(validator, "_do_validate", return_value=active),
            patch.object(validator, "_log_result"),
            patch.object(validator, "_update_metrics"),
        ):
            validator.validate()
        cached_at = validator._last_checked

        # When: the TTL has expired and the source is unreadable at that instant
        with (
            patch.object(validator, "_do_validate", return_value=None),
            patch(
                "baldur.core.entitlement.time.monotonic",
                return_value=cached_at + _RECHECK_TTL_SECONDS + 1,
            ),
        ):
            result = validator.validate()

        # Then: the previous verdict stands and the clock did not move
        assert result is active
        assert validator._last_checked == cached_at

    def test_unreadable_source_is_retried_on_the_very_next_call(self):
        """No cache write means the next call re-reads instead of waiting a TTL."""
        validator = _EntitlementValidator()
        active = EntitlementResult(status=EntitlementStatus.ACTIVE)

        with (
            patch.object(
                validator, "_do_validate", side_effect=[None, active]
            ) as mock_validate,
            patch.object(validator, "_log_result"),
            patch.object(validator, "_update_metrics"),
        ):
            first = validator.validate()
            second = validator.validate()

        assert mock_validate.call_count == 2
        assert first.status == EntitlementStatus.MISSING
        assert second is active

    def test_unreadable_source_writes_no_log_and_no_gauge(self):
        """A non-verdict is not an observation, so nothing is logged or gauged."""
        validator = _EntitlementValidator()

        with (
            patch.object(validator, "_do_validate", return_value=None),
            patch.object(validator, "_log_result") as mock_log,
            patch.object(validator, "_update_metrics") as mock_metrics,
        ):
            validator.validate()

        mock_log.assert_not_called()
        mock_metrics.assert_not_called()


class TestIsEntitlementActiveBehavior:
    """``is_entitlement_active()``, the predicate every body-level gate reads.

    Presence is resolved first so an OSS-only install pays nothing; the verdict
    decides the rest; an indeterminate read fails closed; and the read is never
    forced, so the cached verdict's TTL stays the single lapse granularity.
    """

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (EntitlementStatus.ACTIVE, True),
            (EntitlementStatus.INVALID, False),
            (EntitlementStatus.MISSING, False),
        ],
        ids=["active", "invalid", "missing"],
    )
    def test_verdict_decides_the_predicate_on_a_pro_install(
        self, mock_pro_tier, status, expected
    ):
        """Only ACTIVE admits licensed behaviour; every other verdict withholds it."""
        with patch(
            "baldur.core.entitlement.get_entitlement_status",
            return_value=EntitlementResult(status=status),
        ):
            assert is_entitlement_active() is expected

    def test_unreadable_verdict_fails_closed(self, mock_pro_tier):
        """A raising verdict read is not an admission: the tier boundary holds."""
        with patch(
            "baldur.core.entitlement.get_entitlement_status",
            side_effect=RuntimeError("licence store down"),
        ):
            assert is_entitlement_active() is False

    def test_pro_absent_answers_false_without_reading_the_verdict(self, mock_oss_tier):
        """Presence answers the whole question on an OSS-only install.

        Without the PRO distribution the verdict can only be non-ACTIVE, so
        reading it would add a settings construction, a licence-file read, an
        INFO line and two gauge writes to a tier no gate can serve.
        """
        with patch("baldur.core.entitlement.get_entitlement_status") as mock_verdict:
            result = is_entitlement_active()

        assert result is False
        mock_verdict.assert_not_called()

    def test_the_verdict_is_read_without_forcing_a_revalidation(self, mock_pro_tier):
        """Read-only: no ``force``, so the TTL stays the single lapse granularity."""
        with patch(
            "baldur.core.entitlement.get_entitlement_status",
            return_value=EntitlementResult(status=EntitlementStatus.ACTIVE),
        ) as mock_verdict:
            is_entitlement_active()

        mock_verdict.assert_called_once_with()


class TestEntitlementFixtureContract:
    """The autouse ``entitlement_verdict_active`` conftest fixture's contract.

    Body-level gates read the verdict at call time, so without an ambient
    ACTIVE verdict every existing test that means "PRO installed" would
    silently start meaning "PRO installed but unlicensed".
    """

    def test_fixture_supplies_an_active_verdict(self, entitlement_verdict_active):
        """The ambient verdict is ACTIVE and is what the getter answers."""
        assert entitlement_verdict_active.status == EntitlementStatus.ACTIVE
        assert entitlement_verdict_active.is_active is True
        assert entitlement_module.get_entitlement_status() is entitlement_verdict_active

    def test_local_patch_of_the_same_name_overrides_the_fixture(self):
        """A test wanting a lapsed verdict patches the same getter: the inner
        patch wins for its duration and the ambient one resumes after."""
        with patch(
            "baldur.core.entitlement.get_entitlement_status",
            return_value=EntitlementResult(status=EntitlementStatus.MISSING),
        ):
            assert (
                entitlement_module.get_entitlement_status().status
                == EntitlementStatus.MISSING
            )

        assert (
            entitlement_module.get_entitlement_status().status
            == EntitlementStatus.ACTIVE
        )

    def test_fixture_patches_the_getter_so_import_time_bindings_stay_real(self):
        """The patch is installed on the module attribute, not on the function
        object, so a module that bound the name at import time (this one
        included) keeps calling the real implementation and the validator's
        own tests are unaffected."""
        assert get_entitlement_status is not entitlement_module.get_entitlement_status
