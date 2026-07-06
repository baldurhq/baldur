"""URL configuration for testapp dummy views."""

from django.urls import path

from tests.testapp.views import (
    ErrorView,
    RateLimitTestView,
    SlowView,
    SuccessView,
    TieredEndpointView,
)

urlpatterns = [
    path("test/success/", SuccessView.as_view(), name="test-success"),
    path("test/error/", ErrorView.as_view(), name="test-error"),
    path("test/slow/", SlowView.as_view(), name="test-slow"),
    path("test/rate-limit/", RateLimitTestView.as_view(), name="test-rate-limit"),
    path("test/tiered/", TieredEndpointView.as_view(), name="test-tiered"),
]
