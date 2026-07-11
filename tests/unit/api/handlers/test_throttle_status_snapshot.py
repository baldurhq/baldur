"""throttle_status() provider-ownership contract tests.

The handler must build its response as its own dict and never write
into the provider's returned mapping (the Protocol's caller-owned
snapshot contract). Wire shape: provider keys + a top-level timestamp.

Verification techniques:
- Side effects: the provider's retained dict is unmutated (§8.4)
- Contract: response body = provider keys + "timestamp"
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.interfaces.web_framework import HttpMethod, RequestContext


def _make_ctx(method="GET", path="/throttle/", query=None):
    """Create a RequestContext for handler testing."""
    return RequestContext(
        method=HttpMethod(method),
        path=path,
        query_params=query or {},
        path_params={},
    )


def _provider_stats() -> dict:
    """Realistic AdaptiveThrottle.get_stats() payload (nested maps)."""
    return {
        "current_limit": 100,
        "min_limit": 10,
        "max_limit": 1000,
        "total_requests": 5000,
        "gradient": 0.0,
        "emergency": {"active": False, "level": 0},
        "governance": {"kill_switch_active": False},
    }


class TestThrottleStatusSnapshotBehavior:
    """The handler stamps its own dict, not the provider's."""

    def test_provider_returned_dict_is_not_mutated(self):
        """A provider that retains its returned dict sees no injected key."""
        from baldur.api.handlers.throttle import throttle_status
        from baldur.factory.registry import ProviderRegistry

        retained = _provider_stats()
        stub = MagicMock()
        stub.get_stats.return_value = retained

        with patch.object(
            ProviderRegistry.adaptive_throttle, "safe_get", return_value=stub
        ):
            resp = throttle_status(_make_ctx())

        assert resp.status_code == 200
        assert "timestamp" not in retained
        assert resp.body is not retained

    def test_response_is_provider_keys_plus_timestamp(self):
        """Wire shape is unchanged: every provider key verbatim + timestamp."""
        from baldur.api.handlers.throttle import throttle_status
        from baldur.factory.registry import ProviderRegistry

        retained = _provider_stats()
        stub = MagicMock()
        stub.get_stats.return_value = retained

        with patch.object(
            ProviderRegistry.adaptive_throttle, "safe_get", return_value=stub
        ):
            resp = throttle_status(_make_ctx())

        assert set(resp.body) == set(retained) | {"timestamp"}
        for key, value in retained.items():
            assert resp.body[key] == value
