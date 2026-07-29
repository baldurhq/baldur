"""
Domain Registry unit tests.

Tests domain registration limits, resolve_domain_label enforcement,
and settings integration for metric cardinality control.

Reference:
    src/baldur/metrics/registry.py
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.metrics.registry import (
    _FALLBACK_DOMAIN,
    _MAX_REGISTERED_DOMAINS,
    DEFAULT_DOMAINS,
    _registered_domains,
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
# Behavior Tests — Caller Migration (353 §3.7)
# =============================================================================
