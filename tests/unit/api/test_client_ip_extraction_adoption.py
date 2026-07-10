"""Regression tests for canonical client-IP adoption across the former straggler sites.

Every site now delegates to :func:`baldur.utils.network.extract_client_ip`, so each
honors the ``X-Forwarded-For`` -> ``X-Real-IP`` -> ``REMOTE_ADDR`` precedence and
preserves its own missing-IP sentinel. Before the migration, five of the six ignored
``X-Real-IP`` entirely: behind an X-Real-IP-only proxy (nginx convention) every client
collapsed onto ``REMOTE_ADDR`` (the proxy IP), so enforcement keyed on one bucket while
audit/permission subsystems recorded the real client. The sixth (the DRF throttle
adapter) already read ``X-Real-IP`` but never stripped it.

The helpers are independent of ``self``, so they are exercised as unbound calls — no
middleware construction (and its Django wiring side effects) is needed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from baldur.api.django.admission_control import AdmissionControlMiddleware
from baldur.api.django.middleware.access_logging import SensitiveEndpointAccessLogger
from baldur.api.django.rate_limit.middleware import HybridRateLimitMiddleware
from baldur.api.django.throttle_adapter import AdaptiveDRFThrottle
from baldur.api.django.tiering.middleware import TieringMiddleware
from baldur.services.cell_topology.tagger import CellTagger

# (site id, resolver taking a request, missing-IP sentinel preserved from the original site)
SITES = [
    (
        "admission_control",
        lambda req: AdmissionControlMiddleware._get_client_ip(None, req),
        None,
    ),
    (
        "access_logging",
        lambda req: SensitiveEndpointAccessLogger._get_client_ip(None, req),
        "",
    ),
    ("tiering", lambda req: TieringMiddleware._get_client_ip(None, req), None),
    (
        "rate_limit",
        lambda req: HybridRateLimitMiddleware._get_client_ip(None, req),
        "unknown",
    ),
    ("cell_tagger", lambda req: CellTagger._get_client_ip(req), "unknown"),
    (
        "throttle_adapter",
        lambda req: AdaptiveDRFThrottle.get_ident(None, req),
        "unknown",
    ),
]
SITE_IDS = [site[0] for site in SITES]


@pytest.mark.parametrize(("site_id", "resolve", "sentinel"), SITES, ids=SITE_IDS)
class TestClientIpAdoptionBehavior:
    def test_x_forwarded_for_first_hop_wins(self, rf, site_id, resolve, sentinel):
        request = rf.get(
            "/x", HTTP_X_FORWARDED_FOR="203.0.113.10, 10.0.0.1, 192.168.0.1"
        )
        assert resolve(request) == "203.0.113.10"

    def test_x_real_ip_resolved_when_no_forwarded_for(
        self, rf, site_id, resolve, sentinel
    ):
        # Regression: behind an X-Real-IP-only proxy every site must resolve the real
        # client IP, not REMOTE_ADDR (RequestFactory injects REMOTE_ADDR=127.0.0.1).
        request = rf.get("/x", HTTP_X_REAL_IP="198.51.100.7")
        assert resolve(request) == "198.51.100.7"

    def test_x_real_ip_whitespace_stripped(self, rf, site_id, resolve, sentinel):
        request = rf.get("/x", HTTP_X_REAL_IP="  198.51.100.7  ")
        assert resolve(request) == "198.51.100.7"

    def test_forwarded_for_takes_precedence_over_real_ip(
        self, rf, site_id, resolve, sentinel
    ):
        request = rf.get(
            "/x",
            HTTP_X_FORWARDED_FOR="203.0.113.10",
            HTTP_X_REAL_IP="198.51.100.7",
        )
        assert resolve(request) == "203.0.113.10"

    def test_remote_addr_fallback(self, rf, site_id, resolve, sentinel):
        request = rf.get("/x", REMOTE_ADDR="127.0.0.5")
        assert resolve(request) == "127.0.0.5"

    def test_missing_everything_returns_site_sentinel(self, site_id, resolve, sentinel):
        request = SimpleNamespace(META={})
        assert resolve(request) == sentinel
