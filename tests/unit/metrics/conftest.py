"""Shared fixtures for metrics unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _check_prometheus():
    """Skip all tests if prometheus_client is not installed."""
    from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE

    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")
