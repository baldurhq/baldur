"""``store_failure`` as the DLQ channel's projection choke point.

The DLQ family's metric label is derived from the *stored* domain, while the
retry family's comes from the metric registry. Those two vocabularies disagree
on every spelling the validated channel rejects but the label channel rewrites
(``payment-api``, whitespace padding, mixed case), so registration alone would
have SPLIT one logical domain into two label values. The rejection branch
retries validation on the canonical form, which is what keeps them on one.

The same function is a declaration site: ``domain`` is an explicit parameter of
the public DLQ entry point, so a direct caller gets its own label on the first
call rather than only after some unrelated ``@domain_tag`` module is imported.
This module deliberately imports no decorator.

Reference:
    src/baldur/services/dlq_capture/service.py — ``store_failure``
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.metrics.protocols import MetricsBackend
from baldur.metrics.registry import (
    _registered_domains,
    get_registered_domains,
    reset_registered_domains,
    resolve_domain_label,
)
from baldur.models.dlq import DLQConfig
from baldur.services.dlq_capture import DLQCaptureService
from baldur.utils.domain_validation import FALLBACK_DOMAIN

# A letter-leading / digit-leading UUID spelling, pinned as literals: a random
# ``str(uuid4())`` is a coin-flip between the two admission outcomes below.
_LETTER_LEADING_UUID = "a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
_DIGIT_LEADING_UUID = "01b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


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
    """Real in-process double — deterministic, no I/O, readable by id."""
    return InMemoryFailedOperationRepository()


@pytest.fixture
def service(repo):
    return DLQCaptureService(config=DLQConfig(enabled=True), repository=repo)


def _store(service, repo, domain):
    """Store one entry and return the domain the repository actually holds."""
    result = service.store_failure(
        domain=domain, failure_type="PG_TIMEOUT", mode="sync"
    )
    assert result.success is True
    return repo.get_by_id(result.dlq_id).domain


class TestStoreFailureCanonicalizationRetryBehavior:
    """Behavior verification: one call, one label, for every fixable spelling."""

    @pytest.mark.parametrize(
        ("raw", "expected_stored"),
        [
            # First-try passes — the retry branch is never entered.
            ("payment", "payment"),
            ("Payment", "payment"),
            ("payment.tier2", "payment.tier2"),
            # Validation rejects, canonicalization fixes.
            ("payment-api", "payment_api"),
            ("  payment  ", "payment"),
            ("My-Service", "my_service"),
            (" " * 5 + "a" * 60, "a" * 60),
            # Canonicalization cannot fix it — today's fallback path stands.
            ("x" * 100, FALLBACK_DOMAIN),
            ("", FALLBACK_DOMAIN),
            ("   ", FALLBACK_DOMAIN),
            ("3rd_party", FALLBACK_DOMAIN),
        ],
    )
    def test_stored_domain_of_each_spelling(self, service, repo, raw, expected_stored):
        """The stored key is the form the metric registry admits, or the fallback."""
        assert _store(service, repo, raw) == expected_stored

    @pytest.mark.parametrize(
        ("raw", "expected_label"),
        [
            ("payment-api", "payment_api"),
            ("  payment  ", "payment"),
            (" " * 5 + "a" * 60, "a" * 60),
        ],
    )
    def test_stored_domain_and_metric_label_agree(
        self, service, repo, raw, expected_label
    ):
        """The split this projection exists to prevent: two families, one value."""
        stored = _store(service, repo, raw)

        assert stored == expected_label
        assert resolve_domain_label(raw) == expected_label

    def test_over_length_name_lands_on_the_fallback_on_every_family(
        self, service, repo
    ):
        """Negative assertion: a >64-char canonical form is never a label."""
        raw = "x" * 100

        assert _store(service, repo, raw) == FALLBACK_DOMAIN
        assert resolve_domain_label(raw) == FALLBACK_DOMAIN
        assert "x" * 100 not in get_registered_domains()

    def test_empty_domain_keeps_todays_rejection_path(self, service, repo):
        """The skip-list is what stops ``""`` merging into a real domain."""
        with patch(
            "baldur.metrics.event_handlers.DLQMetricEventHandler.on_domain_rejected",
            autospec=True,
        ) as mock_rejected:
            assert _store(service, repo, "") == FALLBACK_DOMAIN

        mock_rejected.assert_called_once()

    def test_fixable_spelling_does_not_report_a_rejection(self, service, repo):
        """``on_domain_rejected`` narrows to "canonicalization cannot fix it"."""
        with patch(
            "baldur.metrics.event_handlers.DLQMetricEventHandler.on_domain_rejected",
            autospec=True,
        ) as mock_rejected:
            assert _store(service, repo, "payment-api") == "payment_api"

        mock_rejected.assert_not_called()

    def test_canonical_form_re_validates_as_a_first_try_pass(self, service, repo):
        """Idempotency under the async outbox round-trip.

        The worker thread re-enters ``store_failure`` with the already-projected
        value, which must not be re-projected or reported as a rejection.
        """
        once = _store(service, repo, "payment-api")

        with patch(
            "baldur.metrics.event_handlers.DLQMetricEventHandler.on_domain_rejected",
            autospec=True,
        ) as mock_rejected:
            twice = _store(service, repo, once)

        assert twice == once
        mock_rejected.assert_not_called()


class TestStoreFailureRegistrationBehavior:
    """Behavior verification: the label lands on the FIRST direct DLQ call."""

    def test_direct_call_registers_its_domain_immediately(self, service, repo):
        """No import-timing dependency: one input, one label value, always.

        Without this site the same string in the same process labels the
        fallback before, and ``payment`` after, some unrelated module carrying
        ``@domain_tag("payment")`` happens to be imported.
        """
        assert "payment" not in get_registered_domains()

        assert _store(service, repo, "payment") == "payment"

        assert "payment" in get_registered_domains()
        assert resolve_domain_label("payment") == "payment"

    def test_item_created_metric_carries_the_declared_domain(self, service):
        """The declaration reaches the metric within the same call."""
        mock_metrics = MagicMock(spec=MetricsBackend)

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            autospec=True,
            return_value=mock_metrics,
        ):
            service.store_failure(
                domain="payment", failure_type="PG_TIMEOUT", mode="sync"
            )

        mock_metrics.record_dlq_item_created.assert_called_once_with(
            "payment", "PG_TIMEOUT"
        )

    def test_registered_form_matches_the_stored_form(self, service, repo):
        """Registration runs AFTER the projection, so the join key agrees."""
        stored = _store(service, repo, "Payment-API")

        assert stored == "payment_api"
        assert stored in get_registered_domains()

    def test_unfixable_domain_registers_nothing(self, service, repo):
        """Negative assertion: the fallback bucket claims no new slot."""
        before = get_registered_domains()

        assert _store(service, repo, "x" * 100) == FALLBACK_DOMAIN

        assert get_registered_domains() == before


class TestStoreFailureAdmissionWideningBehavior:
    """Behavior verification: the *new* DLQ-store admission contract.

    DLQ store admission was always shape-only — underscore/hex spellings such
    as ``job_<hex>`` already passed. The canonicalization retry widens it to
    hyphenated spellings, which is intended and priced: the alpha-start /
    length / charset guard is unchanged, and metric cardinality stays capped by
    the registry.
    """

    def test_letter_leading_uuid_stores_under_its_canonical_form(self, service, repo):
        """It now occupies a registry slot instead of collapsing."""
        expected = _LETTER_LEADING_UUID.replace("-", "_")

        assert _store(service, repo, _LETTER_LEADING_UUID) == expected
        assert expected in get_registered_domains()

    def test_digit_leading_uuid_still_collapses(self, service, repo):
        """The alpha-start anti-UUID guard is not weakened by the widening."""
        assert _store(service, repo, _DIGIT_LEADING_UUID) == FALLBACK_DOMAIN
        assert _DIGIT_LEADING_UUID.replace("-", "_") not in get_registered_domains()
