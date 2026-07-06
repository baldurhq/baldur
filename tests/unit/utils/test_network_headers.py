"""Unit tests for ``extract_client_ip_from_headers`` — 537 D-C6.

Verification target: the framework-free admin transport's client-IP resolver
(``utils/network.extract_client_ip_from_headers``), which mirrors the Django
``extract_client_ip`` over a plain header mapping. Both share the private
``_resolve_forwarded_ip`` precedence helper so the ``X-Forwarded-For`` ->
``X-Real-IP`` order cannot drift between transports (G6).

Verification techniques (UNIT_TEST_GUIDELINES §8):
- §8.1 / §6.7 boundary + parametrize: XFF single / multi-IP / X-Real-IP /
  neither / mixed-case header keys.
- §8.2 edge cases: empty mapping, empty header value, default sentinel.
- No-drift equivalence: the header-map function and the Django META function
  produce the same client IP for equivalent inputs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from baldur.utils.network import (
    extract_client_ip,
    extract_client_ip_from_headers,
)


class TestExtractClientIpFromHeadersBehavior:
    """Header-mapping client-IP resolution behavior (537 D-C6)."""

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"X-Forwarded-For": "203.0.113.5"}, "203.0.113.5"),
            ({"X-Forwarded-For": "203.0.113.5, 10.0.0.1, 10.0.0.2"}, "203.0.113.5"),
            ({"X-Real-IP": "198.51.100.7"}, "198.51.100.7"),
            ({"x-forwarded-for": "203.0.113.9"}, "203.0.113.9"),
            ({"X-REAL-IP": "198.51.100.9"}, "198.51.100.9"),
            ({"X-Forwarded-For": "  203.0.113.5  ,  10.0.0.1"}, "203.0.113.5"),
        ],
        ids=[
            "xff_single",
            "xff_multi_takes_first",
            "x_real_ip_only",
            "lowercase_xff_key",
            "uppercase_real_ip_key",
            "xff_whitespace_stripped",
        ],
    )
    def test_extract_client_ip_from_headers_resolves_forwarded_ip(
        self, headers, expected
    ):
        """Forwarded headers resolve case-insensitively, first XFF entry wins."""
        assert extract_client_ip_from_headers(headers) == expected

    def test_extract_client_ip_from_headers_prefers_xff_over_real_ip(self):
        """X-Forwarded-For takes precedence over X-Real-IP (shared precedence)."""
        headers = {"X-Forwarded-For": "203.0.113.5", "X-Real-IP": "198.51.100.7"}
        assert extract_client_ip_from_headers(headers) == "203.0.113.5"

    def test_extract_client_ip_from_headers_no_forwarded_returns_default(self):
        """No forwarded header present -> caller-supplied default (TCP peer)."""
        assert (
            extract_client_ip_from_headers({"Host": "localhost"}, default="127.0.0.1")
            == "127.0.0.1"
        )

    def test_extract_client_ip_from_headers_empty_mapping_returns_default(self):
        """Empty header mapping falls back to the default sentinel."""
        assert extract_client_ip_from_headers({}, default="peer-addr") == "peer-addr"

    def test_extract_client_ip_from_headers_default_is_none_when_unspecified(self):
        """No default and no forwarded header -> None (D-C6 signature default)."""
        assert extract_client_ip_from_headers({}) is None

    def test_extract_client_ip_from_headers_empty_xff_value_falls_through(self):
        """An empty XFF value is falsy -> falls through to default, not "" ."""
        assert (
            extract_client_ip_from_headers({"X-Forwarded-For": ""}, default="peer")
            == "peer"
        )

    @pytest.mark.parametrize(
        "xff_value",
        ["203.0.113.5", "203.0.113.5, 10.0.0.1", ""],
        ids=["single", "multi", "empty"],
    )
    def test_no_drift_between_meta_and_headers_resolution(self, xff_value):
        """The header-map and Django-META resolvers agree on equivalent input.

        Both delegate to ``_resolve_forwarded_ip`` (537 D-C6, G6): a regression
        that changed one path's XFF parsing without the other would surface as
        a mismatch here. Equivalent defaults are supplied so only the forwarded
        precedence is compared.
        """
        # Given equivalent forwarded-IP inputs in both transport shapes
        django_request = SimpleNamespace(META={"HTTP_X_FORWARDED_FOR": xff_value})
        headers = {"X-Forwarded-For": xff_value}

        # When / Then both transports resolve to the same client IP
        assert extract_client_ip(
            django_request, default="fallback"
        ) == extract_client_ip_from_headers(headers, default="fallback")
