"""
Domain Registry unit tests.

Tests domain registration limits, resolve_domain_label enforcement,
and settings integration for metric cardinality control.

Reference:
    src/baldur/metrics/registry.py
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.metrics import registry as registry_module
from baldur.metrics.registry import (
    _CAP_CACHE_TTL_SECONDS,
    _FALLBACK_DOMAIN,
    _MAX_REGISTERED_DOMAINS,
    DEFAULT_DOMAINS,
    NON_REGISTRABLE_DOMAIN_LABELS,
    UNKNOWN_LABEL_VALUE,
    _refused_seen,
    _registered_domains,
    canonicalize_domain_label,
    get_registered_domains,
    register_domain,
    reset_registered_domains,
    resolve_domain_label,
    sanitize_label_value,
)


@pytest.fixture(autouse=True)
def _reset_registered_domains():
    """Reset _registered_domains to default state before/after each test.

    Every registry-owned memo is cleared at both ends via
    ``reset_registered_domains()``: the unregistered-domain DEBUG notice, the
    refusal memo, the lossy-projection memo, the cap-epoch flag and the cap TTL
    cache all fire at most once per distinct key per process, so a domain some
    earlier test already touched would log nothing here and the assertion would
    fail on test ordering rather than on behavior.
    """
    original = _registered_domains.copy()
    reset_registered_domains()
    yield
    reset_registered_domains()
    _registered_domains.clear()
    _registered_domains.update(original)


# =============================================================================
# Contract Tests
# =============================================================================


class TestDomainRegistryContract:
    """Design contract verification for domain registry constants and defaults."""

    def test_max_registered_domains_default(self):
        """Default max registered domains is 50."""
        assert _MAX_REGISTERED_DOMAINS == 50

    def test_default_domains_count(self):
        """Exactly 5 default domains."""
        assert len(DEFAULT_DOMAINS) == 5

    def test_default_domains_values(self):
        """Default domains are the expected 5 domain-neutral fallbacks."""
        assert set(DEFAULT_DOMAINS) == {
            "external_service",
            "internal_process",
            "async_task",
            "notification",
            "data_sync",
        }

    def test_fallback_domain_constant(self):
        """Fallback domain for unregistered domains is 'OTHER_DOMAIN'."""
        assert _FALLBACK_DOMAIN == "OTHER_DOMAIN"

    def test_initial_registered_domains_match_defaults(self):
        """Initial _registered_domains contains defaults + _FALLBACK_DOMAIN."""
        assert _registered_domains == set(DEFAULT_DOMAINS) | {_FALLBACK_DOMAIN}

    def test_cap_cache_ttl_seconds_is_five(self):
        """The cardinality cap stays memoized for 5 seconds.

        Sized as the console-edit propagation delay for a cardinality ceiling —
        the only thing the layered settings read buys on this path.
        """
        assert _CAP_CACHE_TTL_SECONDS == 5.0

    def test_non_registrable_domain_labels_values(self):
        """Exactly three canonical forms may never occupy a registry slot."""
        assert NON_REGISTRABLE_DOMAIN_LABELS == frozenset(
            {"unknown", "other_domain", "default"}
        )

    def test_fallback_label_stays_in_the_reported_inventory(self):
        """``get_registered_domains()`` keeps the collapse bucket.

        The periodic per-domain gauge updaters enumerate this list, so dropping
        the fallback would freeze the collapse bucket's own gauge refresh.
        """
        assert _FALLBACK_DOMAIN in get_registered_domains()


# =============================================================================
# Behavior Tests — register_domain()
# =============================================================================


class TestRegisterDomainBehavior:
    """Behavior verification for register_domain()."""

    def test_register_within_limit_succeeds(self):
        """Registration within limit returns True."""
        result = register_domain("payment_service")
        assert result is True
        assert "payment_service" in _registered_domains

    def test_register_over_limit_returns_false(self):
        """Registration beyond limit returns False."""
        # Fill up to limit
        for i in range(_MAX_REGISTERED_DOMAINS - len(DEFAULT_DOMAINS)):
            register_domain(f"domain_{i}")

        assert len(_registered_domains) >= _MAX_REGISTERED_DOMAINS
        result = register_domain("one_too_many")
        assert result is False

    def test_register_over_custom_limit_returns_false(self):
        """Registration beyond custom max_domains limit returns False."""
        # Set a small limit
        max_limit = len(DEFAULT_DOMAINS) + 2
        register_domain("extra_1", max_domains=max_limit)
        register_domain("extra_2", max_domains=max_limit)

        result = register_domain("extra_3", max_domains=max_limit)
        assert result is False

    def test_duplicate_registration_always_succeeds(self):
        """Already registered domain always returns True."""
        assert register_domain("external_service") is True
        assert register_domain("external_service") is True

    def test_duplicate_registration_does_not_increase_count(self):
        """Re-registering an existing domain does not change count."""
        count_before = len(_registered_domains)
        register_domain("external_service")
        assert len(_registered_domains) == count_before

    def test_register_domain_stores_canonical_form(self):
        """The canonical label form is what occupies the slot, not the raw input.

        Both ends of the registry go through the same projection, so any two
        spellings with equal canonical forms share one slot and one label.
        """
        assert register_domain("  My-Special.Service ") is True
        assert "my_special_service" in _registered_domains
        assert resolve_domain_label("MY-special.SERVICE") == "my_special_service"

    def test_limit_reached_logs_warning_once_per_cap_epoch(self):
        """The cap refusal is reported once per time the registry FILLS.

        Registration runs on the request path now, so a flood of unique
        over-cap names must not emit one WARNING per call; the operator gets
        one line per cap epoch instead.
        """
        for i in range(_MAX_REGISTERED_DOMAINS - len(_registered_domains)):
            register_domain(f"domain_{i}")
        assert len(_registered_domains) == _MAX_REGISTERED_DOMAINS

        with patch("baldur.metrics.registry.logger") as mock_logger:
            assert register_domain("blocked_domain") is False
            assert register_domain("another_blocked_domain") is False

            mock_logger.warning.assert_called_once_with(
                "metrics.domain_registration_limit_reached",
                domain="blocked_domain",
                max_domains=_MAX_REGISTERED_DOMAINS,
                current_count=_MAX_REGISTERED_DOMAINS,
            )

    def test_successful_registration_logs_debug(self):
        """Debug logged for a successful registration, naming the stored form."""
        with patch("baldur.metrics.registry.logger") as mock_logger:
            register_domain("New_Test_Domain")
            mock_logger.debug.assert_called_with(
                "metrics.domain_registered",
                domain="new_test_domain",
            )


# =============================================================================
# Behavior Tests — resolve_domain_label()
# =============================================================================


class TestResolveDomainLabelBehavior:
    """Behavior verification for resolve_domain_label() enforcement."""

    def test_registered_domain_returns_sanitized(self):
        """Registered domain is returned as sanitized value."""
        result = resolve_domain_label("external_service")
        assert result == "external_service"

    def test_unregistered_domain_returns_fallback(self):
        """Unregistered domain returns OTHER_DOMAIN."""
        result = resolve_domain_label("never_registered_domain")
        assert result == _FALLBACK_DOMAIN

    def test_enforcement_after_limit_exceeded(self):
        """After register_domain rejected, resolve_domain_label returns OTHER_DOMAIN."""
        # Fill to limit
        for i in range(_MAX_REGISTERED_DOMAINS - len(DEFAULT_DOMAINS)):
            register_domain(f"domain_{i}")

        # Registration rejected
        rejected_domain = "rejected_new_domain"
        assert register_domain(rejected_domain) is False

        # Enforcement: resolve returns OTHER_DOMAIN
        assert resolve_domain_label(rejected_domain) == _FALLBACK_DOMAIN

    def test_resolve_sanitizes_input(self):
        """resolve_domain_label sanitizes before lookup."""
        # Register a domain with special chars
        register_domain("my-service")
        # After sanitization, it becomes "my_service"
        result = resolve_domain_label("my-service")
        assert result == sanitize_label_value("my-service")

    def test_unregistered_domain_logs_debug(self):
        """Debug logged when unregistered domain is resolved to OTHER_DOMAIN."""
        with patch("baldur.metrics.registry.logger") as mock_logger:
            resolve_domain_label("unknown_domain")
            mock_logger.debug.assert_called_with(
                "metrics.domain_label_unregistered",
                domain="unknown_domain",
                resolved_to=_FALLBACK_DOMAIN,
            )

    def test_repeat_unregistered_domain_logs_once(self):
        """The notice is per distinct domain, not per call.

        protect() passes the caller's name through as the metric domain and
        nothing registers domains, so an unregistered domain is the common
        case on every recording call — and the retry path now resolves once
        per attempt. Without the dedup this DEBUG dominates the hot path,
        because structlog's non-filtering BoundLogger pays the full processor
        chain at any configured level.
        """
        with patch("baldur.metrics.registry.logger") as mock_logger:
            resolve_domain_label("chatty_domain")
            resolve_domain_label("chatty_domain")
            resolve_domain_label("chatty_domain")

        assert mock_logger.debug.call_count == 1

    def test_dedup_is_per_domain_not_global(self):
        """A first sighting of a *different* domain still logs."""
        with patch("baldur.metrics.registry.logger") as mock_logger:
            resolve_domain_label("first_unknown")
            resolve_domain_label("second_unknown")

        logged = [c.kwargs["domain"] for c in mock_logger.debug.call_args_list]
        assert logged == ["first_unknown", "second_unknown"]

    def test_resolution_is_unchanged_by_the_dedup(self):
        """Suppressing the log must not change what the resolver returns."""
        assert resolve_domain_label("quiet_domain") == _FALLBACK_DOMAIN
        assert resolve_domain_label("quiet_domain") == _FALLBACK_DOMAIN

    def test_reset_registered_domains_clears_the_log_memo(self):
        """reset_registered_domains() is the single reset entry point.

        Every existing fixture in the suite depends on that being true, so it
        is asserted directly rather than inferred.
        """
        resolve_domain_label("resettable_domain")
        reset_registered_domains()

        with patch("baldur.metrics.registry.logger") as mock_logger:
            resolve_domain_label("resettable_domain")

        assert mock_logger.debug.call_count == 1

    def test_non_string_input_resolves_to_the_fallback_without_raising(self):
        """Totality: a non-``str`` must keep resolving, not start raising.

        ``sanitize_label_value`` short-circuits on falsy input today, so
        ``None`` already reaches a label; the canonical projection has to
        preserve that or the DLQ store's exception handler would drop records.
        """
        assert resolve_domain_label(None) == _FALLBACK_DOMAIN
        assert resolve_domain_label(123) == _FALLBACK_DOMAIN
        assert resolve_domain_label(object()) == _FALLBACK_DOMAIN

    def test_registered_domain_resolves_from_any_equal_canonical_spelling(self):
        """One registration serves every spelling with the same canonical form."""
        register_domain("Payment-API")

        assert resolve_domain_label("Payment-API") == "payment_api"
        assert resolve_domain_label("  payment-api  ") == "payment_api"
        assert resolve_domain_label("PAYMENT.API") == "payment_api"

    def test_fallback_label_resolves_to_itself(self):
        """Resolving the collapse bucket echoes it — no membership lookup."""
        assert resolve_domain_label(_FALLBACK_DOMAIN) == _FALLBACK_DOMAIN
        assert resolve_domain_label("other_domain") == _FALLBACK_DOMAIN

    def test_empty_domain_resolves_to_the_fallback(self):
        """An empty domain stays unclassified rather than becoming a series."""
        assert resolve_domain_label("") == _FALLBACK_DOMAIN
        assert resolve_domain_label("   ") == _FALLBACK_DOMAIN

    def test_literal_unknown_resolves_to_the_fallback(self):
        """The Celery unmatched-task fallback literal never becomes a label.

        ``unknown`` is simultaneously what a blank input sanitizes to and the
        Celery adapter's own unmatched-task fallback; registering it would
        merge unrelated traffic from both channels into one series.
        """
        assert register_domain("unknown") is False
        assert resolve_domain_label("unknown") == _FALLBACK_DOMAIN


# =============================================================================
# Behavior Tests — get_registered_domains()
# =============================================================================


class TestGetRegisteredDomainsBehavior:
    """Behavior verification for get_registered_domains()."""

    def test_returns_sorted_list(self):
        """Returned domains are sorted alphabetically."""
        domains = get_registered_domains()
        assert domains == sorted(domains)

    def test_includes_default_domains(self):
        """All default domains are included."""
        domains = get_registered_domains()
        for default in DEFAULT_DOMAINS:
            assert default in domains

    def test_includes_newly_registered_domains(self):
        """Newly registered domain appears in the list."""
        register_domain("zebra_service")
        domains = get_registered_domains()
        assert "zebra_service" in domains

    def test_returns_list_type(self):
        """Return type is list, not set."""
        domains = get_registered_domains()
        assert isinstance(domains, list)


# =============================================================================
# Behavior Tests — Idempotency
# =============================================================================


class TestDomainRegistryIdempotencyBehavior:
    """Behavior verification: idempotent operations."""

    def test_resolve_domain_label_idempotent(self):
        """Calling resolve_domain_label N times returns same result."""
        results = [resolve_domain_label("external_service") for _ in range(10)]
        assert all(r == "external_service" for r in results)

    def test_register_same_domain_idempotent(self):
        """Registering the same domain N times doesn't change state."""
        for _ in range(10):
            assert register_domain("external_service") is True
        assert len([d for d in _registered_domains if d == "external_service"]) == 1

    def test_resolve_fallback_domain_idempotent_no_spurious_logs(self):
        """Resolving _FALLBACK_DOMAIN itself does not trigger unregistered log."""
        with patch("baldur.metrics.registry.logger") as mock_logger:
            result = resolve_domain_label(_FALLBACK_DOMAIN)

        assert result == _FALLBACK_DOMAIN
        mock_logger.debug.assert_not_called()


# =============================================================================
# Behavior Tests — Settings Integration (353)
# =============================================================================


class TestSettingsIntegrationBehavior:
    """Behavior verification: register_domain() settings connection (353 §3.1)."""

    def test_get_max_domains_from_settings_returns_settings_value(self):
        """_get_max_domains_from_settings reads MetricsSettings via layered provider."""
        from baldur.metrics.registry import _get_max_domains_from_settings

        with patch(
            "baldur.settings.layered_provider.get_layered_settings",
            autospec=True,
        ) as mock_get:
            mock_settings = mock_get.return_value
            mock_settings.max_registered_domains = 200

            result = _get_max_domains_from_settings()

        assert result == 200

    def test_get_max_domains_from_settings_fallback_on_failure(self):
        """Settings load failure falls back to _MAX_REGISTERED_DOMAINS (50)."""
        from baldur.metrics.registry import _get_max_domains_from_settings

        with patch(
            "baldur.settings.layered_provider.get_layered_settings",
            autospec=True,
            side_effect=RuntimeError("settings unavailable"),
        ):
            result = _get_max_domains_from_settings()

        assert result == _MAX_REGISTERED_DOMAINS

    def test_get_max_domains_from_settings_fallback_logs_warning(self):
        """Settings load failure logs metrics.settings_load_failed WARNING."""
        from baldur.metrics.registry import _get_max_domains_from_settings

        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings",
                autospec=True,
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "baldur.metrics.registry.logger",
            ) as mock_logger,  # structlog BoundLogger uses dynamic dispatch
        ):
            _get_max_domains_from_settings()

        mock_logger.warning.assert_called_once_with(
            "metrics.settings_load_failed",
            fallback=_MAX_REGISTERED_DOMAINS,
            error="boom",
        )

    def test_register_domain_none_max_reads_settings(self):
        """register_domain(max_domains=None) reads limit from settings."""
        with patch(
            "baldur.metrics.registry._get_max_domains_from_settings",
            autospec=True,
            return_value=len(_registered_domains),
        ):
            # Already at limit → registration should fail
            result = register_domain("should_be_rejected")

        assert result is False

    def test_register_domain_explicit_max_ignores_settings(self):
        """register_domain(max_domains=100) ignores settings entirely."""
        with patch(
            "baldur.metrics.registry._get_max_domains_from_settings",
            autospec=True,
        ) as mock_get_settings:
            result = register_domain("explicit_domain", max_domains=100)

        # Settings helper should not be called when max_domains is explicit
        mock_get_settings.assert_not_called()
        assert result is True


# =============================================================================
# Contract Tests — _FALLBACK_DOMAIN pre-registration (353)
# =============================================================================


class TestFallbackDomainPreRegistrationContract:
    """Contract: _FALLBACK_DOMAIN must be in _registered_domains at init (353 §2.2)."""

    def test_fallback_domain_in_initial_registered_domains(self):
        """_FALLBACK_DOMAIN ('OTHER_DOMAIN') is pre-registered."""
        assert _FALLBACK_DOMAIN in _registered_domains

    def test_fallback_domain_count_in_initial_set(self):
        """Initial set has 5 defaults + 1 fallback = 6 entries."""
        assert len(_registered_domains) == len(DEFAULT_DOMAINS) + 1


# =============================================================================
# Behavior Tests — canonicalize_domain_label()
# =============================================================================


class TestCanonicalizeDomainLabelBehavior:
    """Behavior verification for the one projection between the two vocabularies.

    The tree carries a validated domain form and a Prometheus label form. This
    function is the single stated projection between them, and both ends of the
    registry go through it — which is what makes any two spellings of one
    logical domain share one label value.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("payment", "payment"),
            ("Payment", "payment"),
            ("PAYMENT", "payment"),
            ("  payment  ", "payment"),
            ("payment-api", "payment_api"),
            ("My-Service", "my_service"),
            ("payment.tier2", "payment_tier2"),
            ("my-service.v2", "my_service_v2"),
            ("", UNKNOWN_LABEL_VALUE),
            ("   ", UNKNOWN_LABEL_VALUE),
            ("unknown", UNKNOWN_LABEL_VALUE),
            ("default", "default"),
            ("OTHER_DOMAIN", "other_domain"),
            ("3rd_party", "3rd_party"),
            ("payment-결제", "payment___"),
            ("payment-환불", "payment___"),
            ("결제", "__"),
        ],
    )
    def test_canonical_form_of_each_input_class(self, raw, expected):
        """Each input class projects onto its stated canonical label form."""
        assert canonicalize_domain_label(raw) == expected

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Payment", "payment"),
            (" payment", "payment"),
            ("payment-api", "payment_api"),
            ("payment.tier2", "payment_tier2"),
            ("PAYMENT-api", "payment.API"),
            ("payment-결제", "payment-환불"),
        ],
    )
    def test_equivalent_spellings_share_one_canonical_form(self, left, right):
        """Two spellings of one logical domain cannot produce two label values."""
        assert canonicalize_domain_label(left) == canonicalize_domain_label(right)

    def test_long_input_is_not_truncated_at_the_validation_cap(self):
        """Canonicalization truncates at the LABEL cap, not the validated one.

        Shortening to 64 here would silently manufacture a label the validated
        channel never agreed to; the admission gate is what rejects an
        over-length canonical form.
        """
        assert canonicalize_domain_label("x" * 100) == "x" * 100

    @pytest.mark.parametrize("raw", [None, 123, 4.5, object(), [], {}, b"payment"])
    def test_non_string_input_yields_the_unknown_label_without_raising(self, raw):
        """Totality on any input, not merely on ``str``.

        Load-bearing rather than defensive: the DLQ store calls this from
        inside an exception handler, where an ``AttributeError`` from
        ``.strip()`` would drop a record that is filed today.
        """
        assert canonicalize_domain_label(raw) == UNKNOWN_LABEL_VALUE

    def test_canonicalization_is_idempotent(self):
        """Applying the projection twice changes nothing.

        The DLQ store re-validates a canonical form on the async outbox
        round-trip, so a non-idempotent projection would drift per hop.
        """
        for raw in ("Payment-API", "payment.tier2", "  MY-Service ", "결제"):
            once = canonicalize_domain_label(raw)
            assert canonicalize_domain_label(once) == once


# =============================================================================
# Behavior Tests — register_domain() admission gate
# =============================================================================


class TestRegisterDomainAdmissionBehavior:
    """Behavior verification for what may occupy a registry slot."""

    def test_canonical_form_at_the_length_cap_is_admitted(self):
        """A 64-character canonical form is inside the validated length cap."""
        name = "a" * 64

        assert register_domain(name) is True
        assert name in _registered_domains

    def test_canonical_form_one_over_the_length_cap_is_refused(self):
        """65 characters is the first refusal — measured on the CANONICAL form.

        Validating the canonical rather than the raw input is what makes a
        raw-vs-stripped length disagreement between the metric and DLQ
        channels inexpressible.
        """
        name = "a" * 65

        assert register_domain(name) is False
        assert name not in _registered_domains

    def test_whitespace_padded_name_is_measured_after_stripping(self):
        """A 66-raw / 64-stripped name admits — length is a canonical property."""
        assert register_domain("  " + "a" * 64 + "  ") is True
        assert "a" * 64 in _registered_domains

    def test_alpha_start_canonical_form_is_admitted(self):
        """The validated pattern's alpha-start rule is the anti-UUID guard."""
        assert register_domain("payment") is True

    def test_digit_start_canonical_form_is_refused(self):
        """A digit-leading name keeps collapsing, consistently on every family."""
        assert register_domain("3rd_party") is False
        assert "3rd_party" not in _registered_domains

    @pytest.mark.parametrize(
        "raw",
        ["unknown", "UNKNOWN", "default", "Default", "other_domain", "OTHER_DOMAIN"],
    )
    def test_non_registrable_canonical_forms_are_refused(self, raw):
        """The three reserved canonical forms never occupy a slot.

        ``unknown`` is the unclassified bucket, ``other_domain`` is the collapse
        bucket itself, and ``default`` is the retry config field default — an
        absence of declaration, not a declaration.
        """
        count_before = len(_registered_domains)

        assert register_domain(raw) is False
        assert len(_registered_domains) == count_before

    def test_empty_domain_is_refused_as_the_unclassified_bucket(self):
        """``""`` canonicalizes to the unclassified bucket, which is skip-listed."""
        assert register_domain("") is False
        assert UNKNOWN_LABEL_VALUE not in _registered_domains

    def test_dotted_name_registers_as_its_underscored_projection(self):
        """A segmented validated form admits under the label form it projects to."""
        assert register_domain("payment.tier2") is True
        assert "payment_tier2" in _registered_domains
        assert "payment.tier2" not in _registered_domains

    def test_wholly_non_ascii_name_is_refused(self):
        """Non-ASCII is a MERGE case, not a cardinality case.

        Every non-ASCII character is substituted before admission, so a wholly
        non-ASCII name canonicalizes to underscores and fails the alpha-start
        rule — it collapses consistently rather than splitting the label space.
        """
        assert register_domain("결제") is False

    def test_registration_is_idempotent(self):
        """N identical registrations leave one slot and keep returning True."""
        assert register_domain("Payment-API") is True
        count_after_first = len(_registered_domains)

        for _ in range(10):
            assert register_domain("payment_api") is True
            assert register_domain("  PAYMENT-api ") is True

        assert len(_registered_domains) == count_after_first

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("a" * 65, "too_long"),
            ("3rd_party", "invalid_charset"),
            ("default", "non_registrable"),
            ("", "non_registrable"),
        ],
    )
    def test_admission_refusal_is_reported_with_its_reason(self, raw, reason):
        """An operator gets the refused canonical form and why it was refused."""
        with patch("baldur.metrics.registry.logger") as mock_logger:
            register_domain(raw)

        mock_logger.warning.assert_called_once_with(
            "metrics.domain_registration_refused",
            domain=canonicalize_domain_label(raw),
            reason=reason,
        )

    def test_repeating_admission_refusal_is_reported_once(self):
        """A refused name on the request path must not warn per call."""
        with patch("baldur.metrics.registry.logger") as mock_logger:
            for _ in range(5):
                assert register_domain("3rd_party") is False

        assert mock_logger.warning.call_count == 1

    def test_refusal_memo_is_per_domain_not_global(self):
        """A first sighting of a different refused name still warns."""
        with patch("baldur.metrics.registry.logger") as mock_logger:
            register_domain("3rd_party")
            register_domain("4th_party")

        refused = [c.kwargs["domain"] for c in mock_logger.warning.call_args_list]
        assert refused == ["3rd_party", "4th_party"]

    def test_a_flood_of_distinct_refused_names_saturates_instead_of_recycling(self):
        """The refusal memo is bounded AND the WARNING count is bounded.

        Registration now runs on the request path, so a per-tenant name that
        the admission gate rejects — a digit-leading id, say — arrives with a
        fresh spelling every call. An LRU would evict and re-admit forever,
        emitting one WARNING per call: the exact flood the cap-epoch flag
        exists to prevent on the other refusal path.
        """
        limit = registry_module._MAX_REFUSED_LOGGED_DOMAINS

        with patch("baldur.metrics.registry.logger") as mock_logger:
            for index in range(limit + 200):
                assert register_domain(f"{index}_tenant") is False

        assert mock_logger.warning.call_count == limit
        assert len(registry_module._refused_seen) == limit


# =============================================================================
# Behavior Tests — register_domain() totality
# =============================================================================


class TestRegisterDomainTotalityBehavior:
    """Behavior verification: register_domain() never raises.

    Declaration sites call it bare, without a local try/except. One of them
    (``baldur_task``) runs inside an ``except`` block ahead of a re-raise, where
    a propagating registry error would mask the business exception and kill the
    DLQ store that follows it.
    """

    @pytest.mark.parametrize("raw", [None, 123, 4.5, object(), [], {"d": 1}])
    def test_non_string_input_returns_false_without_raising(self, raw):
        """A non-``str`` is refused, not propagated."""
        assert register_domain(raw) is False

    def test_internal_exception_returns_false_without_raising(self):
        """An injected internal failure degrades to a refusal plus a WARNING."""
        with (
            patch.object(
                registry_module,
                "_admission_refusal_reason",
                autospec=True,
                side_effect=RuntimeError("boom"),
            ),
            patch("baldur.metrics.registry.logger") as mock_logger,
        ):
            result = register_domain("payment")

        assert result is False
        mock_logger.warning.assert_called_once_with(
            "metrics.domain_registration_failed",
            error="boom",
        )

    def test_registration_failure_does_not_mask_a_caller_exception(self):
        """A site calling it from an ``except`` block still re-raises its own error."""
        with patch.object(
            registry_module,
            "_admission_refusal_reason",
            autospec=True,
            side_effect=RuntimeError("registry exploded"),
        ):
            with pytest.raises(ValueError, match="business failure"):
                try:
                    raise ValueError("business failure")
                except ValueError:
                    register_domain("payment")
                    raise


# =============================================================================
# Behavior Tests — register_domain() cap enforcement under the registry lock
# =============================================================================


def _fill_to(target_size: int, *, prefix: str = "filler") -> None:
    """Register throwaway domains until the registry holds ``target_size``."""
    index = 0
    while len(_registered_domains) < target_size:
        assert register_domain(f"{prefix}_{index}", max_domains=target_size) is True
        index += 1


class TestRegisterDomainCapBehavior:
    """Behavior verification: the cap is exact, and reported once per epoch."""

    def test_concurrent_registrants_at_the_cap_boundary_do_not_overshoot(self):
        """N threads racing for the last slot admit exactly one domain.

        Check-then-act was benign while nothing called ``register_domain``; D3
        puts it on the request path, where 8 WSGI threads at cap-1 previously
        admitted 8. The miss path is now taken under the registry lock.
        """
        # Given: a deterministic cap with exactly one free slot
        cap = len(_registered_domains) + 6
        _fill_to(cap - 1)
        assert len(_registered_domains) == cap - 1

        worker_count = 8
        barrier = threading.Barrier(worker_count)
        results: list[bool] = []
        results_lock = threading.Lock()

        def _register(index: int) -> None:
            barrier.wait(timeout=10.0)
            outcome = register_domain(f"racer_{index}")
            with results_lock:
                results.append(outcome)

        # When
        with patch(
            "baldur.metrics.registry._get_max_domains_from_settings",
            autospec=True,
            return_value=cap,
        ):
            threads = [
                threading.Thread(target=_register, args=(i,), daemon=True)
                for i in range(worker_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)

        # Then
        assert all(not t.is_alive() for t in threads)
        assert results.count(True) == 1
        assert len(_registered_domains) == cap

    def test_second_registrant_of_the_same_name_wins_on_the_in_lock_recheck(self):
        """Double-checked locking: the re-check runs BEFORE the cap read.

        The fast path is lock-free, so two threads registering the same new
        name both reach the miss path. Without the in-lock re-check the second
        one measures a set the first just filled and — at the exact cap
        boundary — refuses a domain that is already registered, opening a cap
        epoch that names it.

        Sequencing is deterministic rather than timed: the main thread holds
        the registry lock while the worker passes the lock-free fast path, then
        fills the registry to the cap and adds the worker's own name before
        releasing.
        """
        # Given: the worker signals once it is past the lock-free prefix
        entered_miss_path = threading.Event()
        real_reason = registry_module._admission_refusal_reason

        def _gated_reason(canonical: str):
            outcome = real_reason(canonical)
            entered_miss_path.set()
            return outcome

        cap_reader = MagicMock(
            registry_module._get_max_domains_from_settings,
            return_value=len(_registered_domains),
        )
        result: list[bool] = []

        def _worker() -> None:
            result.append(register_domain("contended_domain"))

        # When
        with (
            patch.object(registry_module, "_admission_refusal_reason", _gated_reason),
            patch.object(registry_module, "_get_max_domains_from_settings", cap_reader),
            patch("baldur.metrics.registry.logger") as mock_logger,
        ):
            registry_module._registry_lock.acquire()
            worker = threading.Thread(target=_worker, daemon=True)
            worker.start()
            try:
                assert entered_miss_path.wait(timeout=10.0)
                # The "other" registrant wins the race and fills the registry.
                _registered_domains.add("contended_domain")
                while len(_registered_domains) < cap_reader.return_value:
                    _registered_domains.add(f"winner_{len(_registered_domains)}")
            finally:
                registry_module._registry_lock.release()
            worker.join(timeout=10.0)

        # Then
        assert not worker.is_alive()
        assert result == [True]
        cap_reader.assert_not_called()
        assert mock_logger.warning.call_count == 0

    def test_cap_epoch_reopens_after_the_cap_is_raised(self):
        """A successful add clears the epoch, so the NEXT fill warns again.

        One line per time the registry becomes full — not one per rejected
        name, and not one per process lifetime.
        """
        cap = len(_registered_domains) + 2
        _fill_to(cap)

        with patch("baldur.metrics.registry.logger") as mock_logger:
            assert register_domain("over_cap_one", max_domains=cap) is False
            assert register_domain("over_cap_two", max_domains=cap) is False
            assert mock_logger.warning.call_count == 1

            # Cap raised: the refused domain now fits, which closes the epoch.
            assert register_domain("over_cap_one", max_domains=cap + 1) is True
            assert register_domain("over_cap_three", max_domains=cap + 1) is False

        assert mock_logger.warning.call_count == 2

    def test_reset_clears_the_cap_epoch_flag(self):
        """``reset_registered_domains()`` is the single reset entry point."""
        cap = len(_registered_domains) + 1
        _fill_to(cap)
        assert register_domain("blocked_before_reset", max_domains=cap) is False

        reset_registered_domains()
        _fill_to(cap)

        with patch("baldur.metrics.registry.logger") as mock_logger:
            assert register_domain("blocked_after_reset", max_domains=cap) is False

        assert mock_logger.warning.call_count == 1


# =============================================================================
# Behavior Tests — register_domain() cap TTL cache
# =============================================================================


class TestRegisterDomainCapCacheBehavior:
    """Behavior verification: an at-cap flood pays no settings read per call.

    A high-cardinality protect name (``protect(f"order_{id}")``) misses the
    registry, the admission gate AND the refusal memo on every call — cap
    refusals deliberately do not enter the LRU memo, because a unique-name
    flood would thrash it. Without the cap cache each such call would pay a
    full layered settings read on a path that previously cost nothing.
    """

    def test_flood_of_unique_at_cap_names_reads_settings_at_most_twice(self):
        """200 unique refused names, at most 2 settings constructions."""
        cap = len(_registered_domains)

        with patch(
            "baldur.metrics.registry._get_max_domains_from_settings",
            autospec=True,
            return_value=cap,
        ) as mock_cap_read:
            for index in range(200):
                assert register_domain(f"order_{index}") is False

        assert mock_cap_read.call_count <= 2

    def test_flood_of_unique_at_cap_names_warns_at_most_once(self):
        """The cap epoch, not the refusal memo, is what bounds the WARNING."""
        cap = len(_registered_domains)

        with (
            patch(
                "baldur.metrics.registry._get_max_domains_from_settings",
                autospec=True,
                return_value=cap,
            ),
            patch("baldur.metrics.registry.logger") as mock_logger,
        ):
            for index in range(200):
                register_domain(f"order_{index}")

        assert mock_logger.warning.call_count <= 1

    def test_expired_cap_cache_reads_settings_again(self):
        """The memo is a TTL cache, not a permanent freeze.

        Expiry is forced by moving the recorded deadline into the past, which
        is what a monotonic-clock advance past the TTL amounts to — no clock
        patching, and no wall-clock wait.
        """
        cap = len(_registered_domains) + 5

        with patch(
            "baldur.metrics.registry._get_max_domains_from_settings",
            autospec=True,
            return_value=cap,
        ) as mock_cap_read:
            assert register_domain("cached_one") is True
            assert register_domain("cached_two") is True
            assert mock_cap_read.call_count == 1

            registry_module._cap_cache_expires_at = 0.0
            assert register_domain("cached_three") is True

        assert mock_cap_read.call_count == 2

    def test_reset_invalidates_the_cap_cache(self):
        """A reset must not leave a stale cap behind for the next test."""
        with patch(
            "baldur.metrics.registry._get_max_domains_from_settings",
            autospec=True,
            return_value=len(_registered_domains) + 5,
        ) as mock_cap_read:
            register_domain("pre_reset_domain")
            assert mock_cap_read.call_count == 1

            reset_registered_domains()
            register_domain("post_reset_domain")

        assert mock_cap_read.call_count == 2


# =============================================================================
# Behavior Tests — cap read fail-open + recovery
# =============================================================================


def _unrelated_field_validation_error() -> Exception:
    """Build a real ``ValidationError`` from an unrelated MetricsSettings field.

    The failure this guards against is one bad ``BALDUR_METRICS_*`` value
    taking the whole layered read down — so the injected fault is the real
    exception type that produces, not a stand-in ``RuntimeError``.
    """
    from baldur.settings.metrics import MetricsSettings

    try:
        MetricsSettings(snapshot_max_age=1)
    except Exception as exc:  # pydantic.ValidationError
        return exc
    raise AssertionError("snapshot_max_age=1 must violate its ge=300 bound")


class TestMaxDomainsFailOpenBehavior:
    """Behavior verification: a settings failure must not refuse all registration.

    Fail-closed would let one unrelated invalid field re-collapse every
    application domain to the fallback label — silently reproducing the exact
    symptom registration exists to fix. Fail-open's worst case is a bounded cap
    of 50 plus a WARNING.
    """

    def test_unrelated_field_error_falls_back_to_the_module_constant(self):
        """Registration continues at cap 50, with the load failure announced."""
        with (
            patch(
                "baldur.settings.layered_provider.get_layered_settings",
                autospec=True,
                side_effect=_unrelated_field_validation_error(),
            ),
            patch("baldur.metrics.registry.logger") as mock_logger,
        ):
            assert register_domain("payment") is True

        assert "payment" in _registered_domains
        warned = [c.args[0] for c in mock_logger.warning.call_args_list]
        assert "metrics.settings_load_failed" in warned
        assert (
            mock_logger.warning.call_args_list[0].kwargs["fallback"]
            == _MAX_REGISTERED_DOMAINS
        )

    def test_settings_failure_memoizes_no_refusal(self):
        """Negative assertion: refusals must not become sticky after recovery."""
        with patch(
            "baldur.settings.layered_provider.get_layered_settings",
            autospec=True,
            side_effect=_unrelated_field_validation_error(),
        ):
            assert register_domain("payment") is True

        assert _refused_seen == {}

    def test_fallback_cap_is_cached_then_the_configured_cap_returns(self):
        """fail → cached 50 → TTL advance → configured cap, with no reset.

        The fallback is cached on the same terms as a configured value: a
        persistently invalid field must not reinstate the per-call layered read
        the cache exists to remove. The priced consequence is that recovery
        from a TRANSIENT failure lags by up to one TTL.
        """
        # Given: the layered read is broken
        with patch(
            "baldur.settings.layered_provider.get_layered_settings",
            autospec=True,
        ) as mock_layered:
            mock_layered.side_effect = _unrelated_field_validation_error()

            # When: two registrations during the failure window
            assert register_domain("failopen_one") is True
            assert register_domain("failopen_two") is True

            # Then: the fallback was resolved once, not once per call
            assert mock_layered.call_count == 1

            # When: settings recover with a cap the registry has already passed
            mock_layered.side_effect = None
            mock_layered.return_value.max_registered_domains = len(_registered_domains)

            # ...the cached fallback still rules until the TTL expires
            assert register_domain("still_cached") is True
            assert mock_layered.call_count == 1

            registry_module._cap_cache_expires_at = 0.0

            # Then: the configured cap is in force again, without a reset call
            assert register_domain("after_recovery") is False
            assert mock_layered.call_count == 2


# =============================================================================
# Behavior Tests — lossy-projection announcement
# =============================================================================


class TestProjectionLossyWarningBehavior:
    """Behavior verification: a stored-vs-label divergence is announced.

    Every domain-labeled series agrees after canonicalization, but a consumer
    joining a STORED domain key against the registered set misses whenever the
    validated form survives a character the label form rewrites. Without this
    line the symptom is a silent zero on the pending gauge, the console panel
    and the drift report.
    """

    def test_dotted_domain_registration_announces_the_divergence(self):
        """The WARNING carries both forms, so the operator can join them."""
        with patch("baldur.metrics.registry.logger") as mock_logger:
            assert register_domain("payment.tier2") is True

        mock_logger.warning.assert_called_once_with(
            "metrics.domain_label_projection_lossy",
            domain="payment.tier2",
            label="payment_tier2",
        )

    def test_second_registration_of_the_same_name_does_not_re_announce(self):
        """Once per canonical name — the memo, not the membership fast path.

        The fast path already short-circuits a repeat, so the memo is exercised
        by forcing the miss path a second time.
        """
        assert register_domain("payment.tier2") is True

        _registered_domains.discard("payment_tier2")
        with patch("baldur.metrics.registry.logger") as mock_logger:
            assert register_domain("payment.tier2") is True

        assert mock_logger.warning.call_count == 0

    def test_lossy_spelling_of_an_already_registered_name_still_announces(self):
        """The divergence belongs to the spelling, not to who registered first.

        ``data.sync`` canonicalizes onto ``data_sync``, one of the six domains
        the registry ships pre-registered — so it never reaches the admission
        branch. It is stored with its dot all the same, so the registry-joined
        gauge reads zero for it, and an admission-only announcement would be
        silent on the framework's own shipped vocabulary.
        """
        assert "data_sync" in _registered_domains

        with patch("baldur.metrics.registry.logger") as mock_logger:
            assert register_domain("data.sync") is True

        mock_logger.warning.assert_called_once_with(
            "metrics.domain_label_projection_lossy",
            domain="data.sync",
            label="data_sync",
        )

    def test_agreeing_form_on_the_fast_path_is_evaluated_once(self):
        """A repeat of an agreeing name must not re-run the validator.

        The memo records every canonical form it has *evaluated*, not only the
        ones it warned about — otherwise the membership fast path would pay a
        validation on every steady-state call.
        """
        register_domain("payment")

        with patch(
            "baldur.metrics.registry.validate_and_normalize_domain", autospec=True
        ) as mock_validate:
            for _ in range(5):
                assert register_domain("payment") is True

        mock_validate.assert_not_called()

    @pytest.mark.parametrize("raw", ["payment", "Payment-API", "  my-service  "])
    def test_agreeing_forms_are_not_announced(self, raw):
        """Negative assertion: a domain whose two forms agree stays silent.

        ``Payment-API`` is rejected outright by the validated channel, so the
        DLQ store falls back to this same canonical form — the two agree and
        there is nothing to announce.
        """
        with patch("baldur.metrics.registry.logger") as mock_logger:
            assert register_domain(raw) is True

        assert mock_logger.warning.call_count == 0

    def test_reset_clears_the_lossy_projection_memo(self):
        """A reset re-arms the announcement for the next process-lifetime."""
        register_domain("payment.tier2")
        reset_registered_domains()

        with patch("baldur.metrics.registry.logger") as mock_logger:
            register_domain("payment.tier2")

        assert mock_logger.warning.call_count == 1


# =============================================================================
# Behavior Tests — Caller Migration (353 §3.7)
# =============================================================================
