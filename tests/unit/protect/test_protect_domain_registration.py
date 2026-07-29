"""``protect()`` domain registration — the first-run entry point.

``protect(name)``'s name is documented as reaching a Prometheus label, but it
does so only through the retry stage: the retry policies record
``RetryPolicyConfig.domain``, and the registry collapses anything unregistered
into the fallback label. The retry stage's config-returning exit is therefore
the declaration site, and the other exits deliberately register nothing.

Reference:
    src/baldur/protect_facade.py — ``_resolve_retry_stage``
"""

from __future__ import annotations

import pytest

from baldur.metrics.registry import (
    _FALLBACK_DOMAIN,
    _registered_domains,
    get_registered_domains,
    reset_registered_domains,
    resolve_domain_label,
)
from baldur.protect_facade import _resolve_retry_stage, protect
from baldur.services.retry_handler.models import RetryPolicyConfig


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry, its memos and its cap cache are process-global."""
    original = _registered_domains.copy()
    reset_registered_domains()
    yield
    reset_registered_domains()
    _registered_domains.clear()
    _registered_domains.update(original)


class _DummyBridgePolicy:
    """A caller-supplied pre-built ``ResiliencePolicy`` (duck-typed Protocol)."""

    @property
    def name(self) -> str:
        return "dummy_bridge"

    def execute(self, func, *args, context=None, **kwargs):
        raise AssertionError("bridge policy must not run in these tests")


class TestProtectRegistrationBehavior:
    """Behavior verification: which protect() exits claim a metric-label slot."""

    def test_retry_composing_call_registers_its_domain(self):
        """The headline outcome: an application's own domain survives as a label.

        Asserted through ``protect(name, fn, retry=True)`` rather than a direct
        recorder call — this is the entry point a first-time user meets before
        any adapter.
        """
        # Given / When
        assert protect("payment", lambda: 42, retry=True) == 42

        # Then
        assert "payment" in get_registered_domains()
        assert resolve_domain_label("payment") == "payment"

    def test_registered_label_is_the_canonical_form_of_the_name(self):
        """A hyphenated protect name lands on one label value, not two."""
        assert protect("Payment-API", lambda: 1, retry=True) == 1

        assert "payment_api" in get_registered_domains()
        assert resolve_domain_label("Payment-API") == "payment_api"

    def test_default_retry_off_call_registers_nothing(self):
        """No retry stage means no domain label exists to claim a slot for.

        Registering here would burn a cap slot and materialize zero-valued
        gauge series for a domain no series carries.
        """
        before = get_registered_domains()

        assert protect("no_retry_domain", lambda: 1) == 1

        assert get_registered_domains() == before

    def test_retry_false_registers_nothing(self):
        """The explicit off switch takes the same no-domain exit."""
        before = get_registered_domains()

        assert protect("explicitly_off_domain", lambda: 1, retry=False) == 1

        assert get_registered_domains() == before
        assert resolve_domain_label("explicitly_off_domain") == _FALLBACK_DOMAIN

    def test_caller_supplied_resilience_policy_registers_nothing(self):
        """A pre-built policy carries no in-band domain to declare."""
        before = get_registered_domains()

        retry_cfg, retry_policy, settings_derived = _resolve_retry_stage(
            _DummyBridgePolicy(), dlq_requested=False, domain="bridge_domain"
        )

        assert (retry_cfg, settings_derived) == (None, False)
        assert retry_policy is not None
        assert get_registered_domains() == before

    def test_explicit_config_with_unset_domain_registers_nothing(self):
        """``RetryPolicyConfig()``'s field default is an ABSENCE of declaration.

        ``"default"`` is in the registry's non-registrable set, so these calls
        keep collapsing to the fallback label exactly as before registration
        existed.
        """
        before = get_registered_domains()

        assert protect("unset_domain_name", lambda: 1, retry=RetryPolicyConfig()) == 1

        assert get_registered_domains() == before
        assert "default" not in get_registered_domains()
        assert resolve_domain_label("default") == _FALLBACK_DOMAIN

    def test_explicit_config_registers_its_domain_not_the_protect_name(self):
        """The recorded domain is ``cfg.domain``; they coincide only by default.

        A hook that registered ``name`` would register a string no series
        carries whenever the caller passes an explicit config.
        """
        cfg = RetryPolicyConfig(max_attempts=1, domain="billing")

        assert protect("checkout", lambda: 1, retry=cfg) == 1

        assert "billing" in get_registered_domains()
        assert "checkout" not in get_registered_domains()

    def test_repeated_calls_register_one_slot(self):
        """The membership fast path keeps the request path idempotent."""
        for _ in range(5):
            protect("payment", lambda: 1, retry=True)

        assert get_registered_domains().count("payment") == 1
