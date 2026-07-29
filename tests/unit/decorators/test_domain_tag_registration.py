"""``@domain_tag`` as a metric-label declaration site.

The decorator literal IS the application declaring its domain vocabulary, so it
claims a metric-label slot at decoration time. The runtime channels that carry
the same value — ``DomainContext``, ``set_domain_context``, the middleware mixin
— deliberately do not: they receive strings whose provenance (code literal vs
client-supplied header) is indistinguishable in-process, and auto-admitting them
would let an external caller squat the cardinality cap.

Reference:
    src/baldur/decorators/domain_tag.py
"""

from __future__ import annotations

import pytest

from baldur.decorators.domain_tag import (
    DomainContext,
    clear_domain_context,
    domain_tag,
    set_domain_context,
)
from baldur.metrics.registry import (
    _registered_domains,
    get_registered_domains,
    reset_registered_domains,
    resolve_domain_label,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry, its memos and its cap cache are process-global."""
    original = _registered_domains.copy()
    reset_registered_domains()
    clear_domain_context()
    yield
    clear_domain_context()
    reset_registered_domains()
    _registered_domains.clear()
    _registered_domains.update(original)


class TestDomainTagRegistrationBehavior:
    """Behavior verification: decoration-time registration and its exclusions."""

    def test_decoration_registers_the_domain_before_any_call(self):
        """The slot is claimed when the module is imported, not when it runs."""

        # Given / When: decoration only — the function is never called
        @domain_tag("payment")
        def charge():  # pragma: no cover - never invoked
            return "charged"

        # Then
        assert "payment" in get_registered_domains()
        assert resolve_domain_label("payment") == "payment"

    def test_decorated_function_still_returns_its_value(self):
        """Registration is a side effect, not a change to the wrapper contract."""

        @domain_tag("order")
        def place():
            return "placed"

        assert place() == "placed"
        assert "order" in get_registered_domains()

    def test_mixed_case_literal_registers_its_canonical_form(self):
        """One logical domain, one label value — regardless of spelling."""

        @domain_tag("Checkout")
        def checkout():  # pragma: no cover - never invoked
            return None

        assert "checkout" in get_registered_domains()

    def test_dotted_literal_registers_its_underscored_projection(self):
        """A segmented validated form claims the label form it projects to."""

        @domain_tag("payment.tier2")
        def tiered():  # pragma: no cover - never invoked
            return None

        assert "payment_tier2" in get_registered_domains()
        assert resolve_domain_label("payment.tier2") == "payment_tier2"

    def test_domain_context_does_not_register(self):
        """Negative assertion: a runtime string must not claim a cap slot.

        ``DomainContext`` receives values that may have come from an
        ``X-Domain`` client header, which is indistinguishable in-process from
        a code literal.
        """
        before = get_registered_domains()

        with DomainContext("client_supplied"):
            pass

        assert get_registered_domains() == before

    def test_set_domain_context_does_not_register(self):
        """Negative assertion — same channel, same provenance problem."""
        before = get_registered_domains()

        set_domain_context("client_supplied")

        assert get_registered_domains() == before

    def test_repeated_decoration_of_one_literal_claims_one_slot(self):
        """Two modules tagging the same domain share the slot."""

        @domain_tag("shared")
        def first():  # pragma: no cover - never invoked
            return None

        @domain_tag("shared")
        def second():  # pragma: no cover - never invoked
            return None

        assert get_registered_domains().count("shared") == 1
