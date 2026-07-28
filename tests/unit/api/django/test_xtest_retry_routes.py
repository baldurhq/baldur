"""The retry X-Test route surface after the two legacy preview endpoints went.

Target: ``api/django/urls/xtest.py`` + ``api/django/views/xtest/retry.py``

The backoff-preview and retry-simulate endpoints rendered a legacy ``4/16/64``
curve that no execution path has produced for some time, and would have gone
from approximately wrong to actively wrong once the executor's ladder was
resolved from the operator-facing settings. They had no consumers, no tests
and no documentation, so they were removed rather than repointed; the startup
report's effective-backoff entry is the signal they only claimed to be.

Asserted against ``urlpatterns`` rather than through ``django.urls.reverse``:
the test URLconf does not ``include()`` the xtest routes, so ``reverse`` raises
``NoReverseMatch`` for every name here and cannot tell a removed route from an
unwired one -- which would make the removal assertions pass vacuously.
"""

from __future__ import annotations

import pytest

from baldur.api.django.urls.xtest import urlpatterns

#: The two endpoints removed with the backoff-resolution change.
REMOVED_ROUTE_NAMES = ("xtest-retry-backoff-preview", "xtest-retry-simulate")

#: The two that stay: both carry live scope in other work, so a whole-module
#: deletion would have silently voided it.
SURVIVING_ROUTE_NAMES = ("xtest-retry-rate-limit-status", "xtest-retry-config")


def _registered_names() -> set[str]:
    return {pattern.name for pattern in urlpatterns if pattern.name}


class TestXTestRetryRouteRemovalBehavior:
    """Which retry X-Test routes the module registers."""

    @pytest.mark.parametrize("name", REMOVED_ROUTE_NAMES)
    def test_a_removed_route_is_no_longer_registered(self, name):
        """The URL simply stops resolving -- early access needs no tombstone."""
        assert name not in _registered_names()

    @pytest.mark.parametrize("name", SURVIVING_ROUTE_NAMES)
    def test_a_surviving_route_is_still_registered(self, name):
        """The removal was scoped to two views, not to the module.

        Without this half, deleting the whole file would pass the assertions
        above while quietly taking two in-scope endpoints with it.
        """
        assert name in _registered_names()

    @pytest.mark.parametrize("symbol", ["BackoffPreviewView", "RetrySimulateView"])
    def test_a_removed_view_is_gone_from_the_view_package(self, symbol):
        """The barrel no longer advertises what the URLconf no longer wires."""
        from baldur.api.django.views import xtest as xtest_views

        assert not hasattr(xtest_views, symbol)

    def test_the_retry_view_module_no_longer_reaches_the_legacy_calculator(self):
        """The legacy curve had one importer left, and it was these two views.

        The calculator package itself stays -- its adaptive retry budget is
        still composed by the retry policy and the tenacity bridge -- but no
        API surface renders its ``base ** n`` schedule any more, so no operator
        can be shown a ladder the executor does not produce.
        """
        from baldur.api.django.views.xtest import retry as retry_views

        assert not hasattr(retry_views, "ThrottleAwareBackoffCalculator")
        assert not hasattr(retry_views, "BackoffConfig")

    def test_the_surviving_views_are_still_importable(self):
        """The two views other work owns are untouched by the deletion."""
        from baldur.api.django.views.xtest import retry as retry_views

        assert hasattr(retry_views, "RetryRateLimitStatusView")
        assert hasattr(retry_views, "XTestRetryConfigView")
