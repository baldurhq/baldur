"""Domain-labeled writers that bypass the recorder layer still use the funnel.

Five domain-labeled writes reached their metric definitions directly and passed
a raw string: the four ``retry_backoff_*`` / throttle families and the outbox
processing-delay histogram. Two consequences, both covered here — one logical
domain landed on two label values (canonical wherever the funnel runs, raw
here), and ``max_registered_domains`` did not apply, so
``protect(f"order_{id}")`` minted an unbounded set of label values on four
histograms.

The structural half of this rule — that no NEW writer reintroduces the bypass —
is the G79 AST fitness function; this module covers the runtime half.

Reference:
    src/baldur/services/backoff_calculator/calculator.py
    src/baldur/services/dlq_outbox/outbox.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.metrics.registry import (
    _FALLBACK_DOMAIN,
    _registered_domains,
    register_domain,
    reset_registered_domains,
)
from baldur.services.backoff_calculator.calculator import (
    ThrottleAwareBackoffCalculator,
)
from baldur.services.dlq_outbox.outbox import _on_processing_delay
from baldur.services.metrics import definitions as metric_definitions

_BACKOFF_METRIC_NAMES = (
    "retry_backoff_multiplier",
    "retry_backoff_original_seconds",
    "retry_backoff_adjusted_seconds",
    "retry_throttle_full_stop_skips_total",
)

# Every spelling on the left canonicalizes to the label on the right, so a
# single registration has to serve all of them on every family.
_CANONICAL_PAIRS = [
    ("payment", "payment"),
    ("Payment", "payment"),
    ("  payment  ", "payment"),
    ("Payment-API", "payment_api"),
    ("payment.tier2", "payment_tier2"),
]


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
def backoff_metrics():
    """Patch the four throttle/backoff metric definitions with doubles."""
    doubles = {
        name: MagicMock(getattr(metric_definitions, name))
        for name in _BACKOFF_METRIC_NAMES
    }
    with patch.multiple(
        "baldur.services.metrics.definitions",
        **doubles,
    ):
        yield doubles


def _record_backoff(domain: str, *, multiplier: float = 2.0) -> None:
    """Drive the private recorder directly — the label is its whole output."""
    calculator = ThrottleAwareBackoffCalculator(
        enable_push_cache=False,
        error_budget_check_enabled=False,
    )
    calculator._record_backoff_metrics(
        domain=domain,
        original_delay=1,
        adjusted_delay=2,
        multiplier=multiplier,
        reason="cluster_normal",
    )


class TestUnguardedWriterRoutingBehavior:
    """Behavior verification: the bypassed families now resolve their label."""

    @pytest.mark.parametrize(("raw", "expected"), _CANONICAL_PAIRS)
    def test_backoff_families_carry_the_canonical_label(
        self, backoff_metrics, raw, expected
    ):
        """One registration serves every spelling on all four families."""
        register_domain(raw)

        _record_backoff(raw)

        assert backoff_metrics["retry_backoff_multiplier"].labels.call_args.kwargs == {
            "domain": expected,
            "reason": "cluster_normal",
        }
        for name in (
            "retry_backoff_original_seconds",
            "retry_backoff_adjusted_seconds",
        ):
            assert backoff_metrics[name].labels.call_args.kwargs == {"domain": expected}

    @pytest.mark.parametrize(("raw", "expected"), _CANONICAL_PAIRS)
    def test_full_stop_counter_carries_the_canonical_label(
        self, backoff_metrics, raw, expected
    ):
        """The infinite-multiplier skip counter is on the same funnel."""
        register_domain(raw)

        _record_backoff(raw, multiplier=float("inf"))

        assert backoff_metrics[
            "retry_throttle_full_stop_skips_total"
        ].labels.call_args.kwargs == {"domain": expected}

    @pytest.mark.parametrize(("raw", "expected"), _CANONICAL_PAIRS)
    def test_outbox_delay_histogram_carries_the_canonical_label(self, raw, expected):
        """The stored DLQ domain reaches the histogram as its registered label."""
        register_domain(raw)
        mock_histogram = MagicMock(
            metric_definitions.dlq_outbox_processing_delay_seconds
        )

        with patch(
            "baldur.services.metrics.definitions.dlq_outbox_processing_delay_seconds",
            mock_histogram,
        ):
            _on_processing_delay(0.05, raw)

        mock_histogram.labels.assert_called_once_with(domain=expected)

    def test_unregistered_domain_collapses_on_the_backoff_families(
        self, backoff_metrics
    ):
        """The cardinality cap now applies to these four histograms.

        ``protect(f"order_{id}")`` previously minted one label value per id
        here — precisely the blow-up ``max_registered_domains`` exists to
        prevent.
        """
        _record_backoff("order_98f2c1", multiplier=float("inf"))

        for name in _BACKOFF_METRIC_NAMES:
            assert (
                backoff_metrics[name].labels.call_args.kwargs["domain"]
                == _FALLBACK_DOMAIN
            )

    def test_unregistered_domain_collapses_on_the_outbox_histogram(self):
        """Same collapse on the outbox family."""
        mock_histogram = MagicMock(
            metric_definitions.dlq_outbox_processing_delay_seconds
        )

        with patch(
            "baldur.services.metrics.definitions.dlq_outbox_processing_delay_seconds",
            mock_histogram,
        ):
            _on_processing_delay(0.05, "order_98f2c1")

        mock_histogram.labels.assert_called_once_with(domain=_FALLBACK_DOMAIN)
