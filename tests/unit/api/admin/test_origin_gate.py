"""Request-origin gate unit tests — 536 D6.

Verification targets (pure functions, crafted header dicts — no live socket):
- ``_request_origin_allowed(headers, settings)`` — the DNS-rebinding defense:
  Host/Origin hostname extraction, loopback allowlist, configured allowlist,
  enforcement-scope partitioning (localhost vs non-localhost ± allowlist),
  IPv6 bracket / port handling, absent-Host curl posture, malformed Origin.
- ``_lookup_header(headers, name)`` — case-insensitive lookup, absent → None.

Both functions are deliberately socket-free so the gate is testable with plain
dicts (D6 Testability Notes); ``http.client.HTTPMessage`` is exercised by the
end-to-end tests in ``test_console_integration.py``.
"""

from __future__ import annotations

import pytest

from baldur.api.admin.server import _lookup_header, _request_origin_allowed
from baldur.settings.admin import AdminServerSettings


def _localhost_settings(**overrides) -> AdminServerSettings:
    return AdminServerSettings(bind="127.0.0.1", port=9090, **overrides)


def _public_settings(**overrides) -> AdminServerSettings:
    # api_key mirrors the production requirement for a non-localhost bind; it is
    # not needed for settings construction but keeps the intent explicit.
    return AdminServerSettings(
        bind="0.0.0.0", port=9090, api_key="placeholder", **overrides
    )


# =============================================================================
# _lookup_header — case-insensitive header access
# =============================================================================


class TestLookupHeaderBehavior:
    """Case-insensitive lookup so the gate works for dicts and HTTPMessage."""

    @pytest.mark.parametrize(
        "stored_key",
        ["Host", "host", "HOST", "HoSt"],
    )
    def test_lookup_is_case_insensitive_for_any_key_casing(self, stored_key):
        headers = {stored_key: "127.0.0.1:9090"}
        assert _lookup_header(headers, "Host") == "127.0.0.1:9090"

    def test_lookup_query_name_casing_does_not_matter(self):
        headers = {"Origin": "http://127.0.0.1:9090"}
        assert _lookup_header(headers, "ORIGIN") == "http://127.0.0.1:9090"

    def test_lookup_absent_header_returns_none(self):
        assert _lookup_header({"Host": "x"}, "Origin") is None

    def test_lookup_empty_headers_returns_none(self):
        assert _lookup_header({}, "Host") is None


# =============================================================================
# _request_origin_allowed — localhost-bind enforcement
# =============================================================================


class TestRequestOriginAllowedLocalhostBehavior:
    """Localhost binds always enforce the gate (zero-config DNS-rebinding
    defense via the auto loopback allowlist)."""

    @pytest.mark.parametrize(
        "host_value",
        [
            "127.0.0.1:9090",  # loopback IPv4 with port
            "127.0.0.1",  # loopback IPv4 no port
            "localhost:9090",  # loopback name
            "[::1]:9090",  # IPv6 loopback — brackets + port stripped
            "::1",  # IPv6 loopback bare
        ],
    )
    def test_loopback_host_is_allowed(self, host_value):
        """Every canonical loopback Host form passes on a localhost bind."""
        assert (
            _request_origin_allowed({"Host": host_value}, _localhost_settings()) is True
        )

    def test_foreign_host_is_rejected(self):
        """A DNS-rebound page carries its own hostname as Host → rejected."""
        assert (
            _request_origin_allowed(
                {"Host": "evil.example.com:9090"}, _localhost_settings()
            )
            is False
        )

    def test_curl_posture_no_origin_loopback_host_is_allowed(self):
        """curl/CLI sends a loopback Host and no Origin → unchanged, allowed."""
        assert (
            _request_origin_allowed({"Host": "127.0.0.1:9090"}, _localhost_settings())
            is True
        )

    def test_absent_host_is_allowed_on_localhost(self):
        """HTTP/1.0 / raw clients omit Host; a missing Host cannot be a
        DNS-rebound browser request → allowed (matches curl posture)."""
        assert _request_origin_allowed({}, _localhost_settings()) is True

    def test_same_origin_fetch_is_allowed(self):
        """Console fetch from the served page carries a loopback Origin."""
        headers = {"Host": "127.0.0.1:9090", "Origin": "http://127.0.0.1:9090"}
        assert _request_origin_allowed(headers, _localhost_settings()) is True

    def test_foreign_origin_is_rejected_even_with_loopback_host(self):
        """Browsers send Origin on cross-origin mutations; a foreign Origin is
        rejected even when the Host header is loopback (the rebinding case)."""
        headers = {"Host": "127.0.0.1:9090", "Origin": "http://evil.example.com"}
        assert _request_origin_allowed(headers, _localhost_settings()) is False

    def test_malformed_origin_with_no_hostname_is_rejected(self):
        """A present-but-unparseable Origin yields hostname None → rejected."""
        headers = {"Host": "127.0.0.1:9090", "Origin": "not-a-valid-origin"}
        assert _request_origin_allowed(headers, _localhost_settings()) is False

    def test_ipv6_bracketed_host_strips_brackets_and_port(self):
        """``[::1]:9090`` must resolve to ``::1`` (in the loopback set) — a naive
        rsplit(':', 1) would mangle the bracketed IPv6 colons."""
        assert (
            _request_origin_allowed({"Host": "[::1]:9090"}, _localhost_settings())
            is True
        )


# =============================================================================
# _request_origin_allowed — configured allowlist
# =============================================================================


class TestRequestOriginAllowedAllowlistBehavior:
    """allowed_origins extends the auto-derived loopback/bind allowlist."""

    def test_configured_origin_host_is_allowed(self):
        settings = _localhost_settings(allowed_origins=["admin.example.com"])
        headers = {"Host": "admin.example.com:9090"}
        assert _request_origin_allowed(headers, settings) is True

    def test_configured_origin_header_is_allowed(self):
        settings = _localhost_settings(allowed_origins=["admin.example.com"])
        headers = {
            "Host": "admin.example.com",
            "Origin": "https://admin.example.com",
        }
        assert _request_origin_allowed(headers, settings) is True

    def test_unconfigured_foreign_host_still_rejected_with_allowlist_set(self):
        """A configured allowlist does not open other hostnames."""
        settings = _localhost_settings(allowed_origins=["admin.example.com"])
        headers = {"Host": "other.example.com"}
        assert _request_origin_allowed(headers, settings) is False


# =============================================================================
# _request_origin_allowed — enforcement-scope partitioning
# =============================================================================


class TestRequestOriginAllowedEnforcementScopeBehavior:
    """The gate is enforced on localhost binds and on non-localhost binds only
    when allowed_origins is explicitly set (D6 enforcement scope)."""

    def test_non_localhost_bind_without_allowlist_skips_enforcement(self):
        """0.0.0.0 with no allowlist: Host auto-derivation is impossible and the
        bind is already API-key-gated → gate is skipped (would otherwise break
        K8s httpGet probes / reverse-proxy Host headers)."""
        headers = {"Host": "anything.example.com", "Origin": "http://evil.example.com"}
        assert _request_origin_allowed(headers, _public_settings()) is True

    def test_non_localhost_bind_with_allowlist_enforces_and_rejects_foreign(self):
        """Once allowed_origins is set, the gate is enforced on the public bind."""
        settings = _public_settings(allowed_origins=["admin.example.com"])
        headers = {"Host": "evil.example.com"}
        assert _request_origin_allowed(headers, settings) is False

    def test_non_localhost_bind_with_allowlist_allows_configured_host(self):
        settings = _public_settings(allowed_origins=["admin.example.com"])
        headers = {"Host": "admin.example.com:9090"}
        assert _request_origin_allowed(headers, settings) is True

    def test_localhost_bind_enforces_without_any_allowlist(self):
        """The localhost branch enforces purely from the auto loopback set."""
        headers = {"Host": "evil.example.com"}
        assert _request_origin_allowed(headers, _localhost_settings()) is False
