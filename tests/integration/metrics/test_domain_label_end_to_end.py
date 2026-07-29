"""One domain string, one label value — asserted ACROSS the modules that share it.

``protect(name, fn, retry=True, dlq=True)`` threads a single domain string
through four modules that share one piece of process-global state: the retry
stage registers into the metric registry, the DLQ store writes the domain into
the repository after its own canonicalization retry, the throttle-aware backoff
calculator reads the same registry for its own label, and the outbox histogram
does too.

"One logical domain never produces two label values" is an assertion *across*
those four. No unit test on any one of them can state it — each would be green
with a different projection on the other side of the seam, which is exactly the
split that registration would otherwise have created.

Mock-based — no Docker. The only doubles are the Prometheus metric definitions,
which exist to observe the label that reaches them; every projection under test
runs for real.

Reference:
    src/baldur/metrics/registry.py
    src/baldur/services/dlq_capture/service.py
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.metrics.recorders.dlq import DLQMetricRecorder
from baldur.metrics.registry import (
    _FALLBACK_DOMAIN,
    _registered_domains,
    get_registered_domains,
    reset_registered_domains,
    resolve_domain_label,
)
from baldur.models.dlq import DLQConfig
from baldur.protect_facade import protect
from baldur.services.backoff_calculator.calculator import (
    ThrottleAwareBackoffCalculator,
)
from baldur.services.dlq_capture import DLQCaptureService
from baldur.services.dlq_outbox.outbox import _on_processing_delay
from baldur.services.metrics import definitions as metric_definitions
from baldur.services.metrics.updaters import update_dlq_pending_gauges

_BACKOFF_METRIC_NAMES = (
    "retry_backoff_multiplier",
    "retry_backoff_original_seconds",
    "retry_backoff_adjusted_seconds",
    "retry_throttle_full_stop_skips_total",
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry, its memos and its cap cache are process-global."""
    original = _registered_domains.copy()
    reset_registered_domains()
    yield
    reset_registered_domains()
    _registered_domains.clear()
    _registered_domains.update(original)


@pytest.fixture
def repo():
    return InMemoryFailedOperationRepository()


@pytest.fixture
def dlq_service(repo):
    return DLQCaptureService(config=DLQConfig(enabled=True), repository=repo)


def _stored_domain(service, repo, domain):
    result = service.store_failure(
        domain=domain, failure_type="PG_TIMEOUT", mode="sync"
    )
    assert result.success is True
    return repo.get_by_id(result.dlq_id).domain


def _backoff_labels(domain, *, multiplier=float("inf")):
    """Drive the backoff recorder and return the label each family received."""
    doubles = {
        name: MagicMock(getattr(metric_definitions, name))
        for name in _BACKOFF_METRIC_NAMES
    }
    with patch.multiple("baldur.services.metrics.definitions", **doubles):
        ThrottleAwareBackoffCalculator(
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )._record_backoff_metrics(
            domain=domain,
            original_delay=1,
            adjusted_delay=2,
            multiplier=multiplier,
            reason="cluster_normal",
        )
    return {
        name: doubles[name].labels.call_args.kwargs["domain"]
        for name in _BACKOFF_METRIC_NAMES
    }


def _outbox_label(domain):
    """Drive the outbox delay histogram and return the label it received."""
    histogram = MagicMock(metric_definitions.dlq_outbox_processing_delay_seconds)
    with patch(
        "baldur.services.metrics.definitions.dlq_outbox_processing_delay_seconds",
        histogram,
    ):
        _on_processing_delay(0.05, domain)
    return histogram.labels.call_args.kwargs["domain"]


class TestDomainLabelAgreementAcrossFamilies:
    """SC: one logical domain, one label value, on every domain-labeled family."""

    def test_protect_registration_carries_a_hyphenated_name_to_every_family(
        self, dlq_service, repo
    ):
        """The single registrant is ``protect``; the other three must follow it.

        Before this landed, ``payment-api`` would have labeled the retry family
        ``payment_api`` (registered) and the DLQ family the fallback
        (validation-rejected) — a split *created* by registration.
        """
        # Given: the only registration in this process comes from protect()
        assert protect("Payment-API", lambda: 1, retry=True) == 1
        assert "payment_api" in get_registered_domains()

        # When / Then: every family resolves the same raw spelling identically
        assert resolve_domain_label("Payment-API") == "payment_api"
        assert _stored_domain(dlq_service, repo, "Payment-API") == "payment_api"
        assert set(_backoff_labels("Payment-API").values()) == {"payment_api"}
        assert _outbox_label("Payment-API") == "payment_api"

    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            ("Payment", "payment", "payment"),
            (" payment", "payment", "payment"),
            ("payment.tier2", "payment_tier2", "payment_tier2"),
        ],
    )
    def test_two_spellings_of_one_domain_never_split_the_label_space(
        self, dlq_service, repo, left, right, expected
    ):
        """Equal canonical forms share one registry slot and one label value."""
        assert protect(left, lambda: 1, retry=True) == 1

        assert resolve_domain_label(left) == expected
        assert resolve_domain_label(right) == expected
        assert _outbox_label(right) == expected
        assert set(_backoff_labels(left).values()) == {expected}
        assert get_registered_domains().count(expected) == 1

    def test_over_cap_name_collapses_on_every_family_at_once(self, dlq_service, repo):
        """The cardinality cap now reaches the formerly unguarded writers.

        ``protect(f"order_{id}")`` used to mint one label value per id on four
        histograms — the blow-up ``max_registered_domains`` exists to prevent.
        """
        with patch(
            "baldur.metrics.registry._get_max_domains_from_settings",
            autospec=True,
            return_value=len(_registered_domains),
        ):
            assert protect("order_98f2c1", lambda: 1, retry=True) == 1

            assert "order_98f2c1" not in get_registered_domains()
            assert resolve_domain_label("order_98f2c1") == _FALLBACK_DOMAIN
            assert set(_backoff_labels("order_98f2c1").values()) == {_FALLBACK_DOMAIN}
            assert _outbox_label("order_98f2c1") == _FALLBACK_DOMAIN


class TestDottedDomainStorageLabelDivergence:
    """The one boundary a rejection-branch choke point structurally cannot reach.

    A dotted domain VALIDATES, so ``store_failure`` never enters its
    canonicalization retry and stores the dots verbatim — while the registry
    admits the underscored projection. Every domain-labeled *series* still
    agrees, but every consumer joining a stored key against the registered set
    misses.

    This is pinned as KNOWN, not fixed: if a later change makes the two agree,
    this test fails and the parked finding closes.
    """

    def test_dotted_domain_stores_dots_and_labels_underscores(self, dlq_service, repo):
        """Storage keeps the validated form; the label is the canonical one."""
        assert _stored_domain(dlq_service, repo, "payment.tier2") == "payment.tier2"
        assert resolve_domain_label("payment.tier2") == "payment_tier2"
        assert "payment_tier2" in get_registered_domains()

    def test_registry_joined_pending_gauge_reads_zero_for_a_dotted_domain(
        self, dlq_service, repo
    ):
        """The consequence, stated: a real backlog publishes as a silent zero.

        The gauge loop enumerates the REGISTERED set and looks each name up in
        a mapping keyed by the STORED domain, so the dotted entry is invisible
        to it and its underscored twin reads 0.
        """
        # Given: a real pending entry under a dotted domain
        _stored_domain(dlq_service, repo, "payment.tier2")
        mock_metrics = SimpleNamespace(dlq=MagicMock(spec=DLQMetricRecorder))

        # When
        with patch(
            "baldur.metrics.prometheus.get_metrics",
            autospec=True,
            return_value=mock_metrics,
        ):
            pending_by_domain = update_dlq_pending_gauges(repository=repo)

        # Then: the repository knows about the dotted key...
        assert pending_by_domain.get("payment.tier2") == 1
        assert "payment_tier2" not in pending_by_domain

        # ...but the gauge keyed on the registered label reads zero.
        published = {
            call.args[0]: call.args[1]
            for call in mock_metrics.dlq.set_pending_count.call_args_list
        }
        assert published["payment_tier2"] == 0

    def test_lossy_projection_is_announced_at_registration(self, dlq_service, repo):
        """The divergence is stated at registration, not left to be discovered.

        Without this line the operator's only symptom is the silent zero above.
        """
        with patch("baldur.metrics.registry.logger") as mock_logger:
            _stored_domain(dlq_service, repo, "payment.tier2")

        mock_logger.warning.assert_called_once_with(
            "metrics.domain_label_projection_lossy",
            domain="payment.tier2",
            label="payment_tier2",
        )
