"""
MetricsAwareCacheAdapter unit tests (311 — Phase 4a).

Verifies the decorator pattern correctly delegates to the wrapped
cache adapter and records drift metrics on get/set operations.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.cache.metrics_decorator import MetricsAwareCacheAdapter
from baldur.interfaces.cache_provider import CacheProviderInterface


@pytest.fixture
def mock_delegate():
    """Create a mock CacheProviderInterface."""
    delegate = MagicMock(spec=CacheProviderInterface)
    delegate.provider_name = "MockCache"
    return delegate


@pytest.fixture
def adapter(mock_delegate):
    """Create a MetricsAwareCacheAdapter wrapping the mock delegate."""
    return MetricsAwareCacheAdapter(mock_delegate)


class TestMetricsAwareCacheAdapterDelegationBehavior:
    """All CacheProviderInterface methods delegate to the wrapped adapter."""

    def test_get_delegates_to_inner(self, adapter, mock_delegate):
        """get() delegates to the wrapped adapter and returns its result."""
        mock_delegate.get.return_value = "cached_value"
        result = adapter.get("my_key")
        mock_delegate.get.assert_called_once_with("my_key")
        assert result == "cached_value"

    def test_set_delegates_to_inner(self, adapter, mock_delegate):
        """set() delegates to the wrapped adapter and returns its result."""
        mock_delegate.set.return_value = True
        ttl = timedelta(seconds=60)
        result = adapter.set("key", "value", ttl)
        mock_delegate.set.assert_called_once_with("key", "value", ttl)
        assert result is True

    def test_delete_delegates_to_inner(self, adapter, mock_delegate):
        """delete() delegates to the wrapped adapter."""
        mock_delegate.delete.return_value = True
        result = adapter.delete("key")
        mock_delegate.delete.assert_called_once_with("key")
        assert result is True

    def test_exists_delegates_to_inner(self, adapter, mock_delegate):
        """exists() delegates to the wrapped adapter."""
        mock_delegate.exists.return_value = True
        assert adapter.exists("key") is True

    def test_incr_delegates_to_inner(self, adapter, mock_delegate):
        """incr() delegates to the wrapped adapter."""
        mock_delegate.incr.return_value = 5
        assert adapter.incr("counter", 2) == 5
        mock_delegate.incr.assert_called_once_with("counter", 2)

    def test_decr_delegates_to_inner(self, adapter, mock_delegate):
        """decr() delegates to the wrapped adapter."""
        mock_delegate.decr.return_value = 3
        assert adapter.decr("counter", 1) == 3
        mock_delegate.decr.assert_called_once_with("counter", 1)

    def test_expire_delegates_to_inner(self, adapter, mock_delegate):
        """expire() delegates to the wrapped adapter."""
        mock_delegate.expire.return_value = True
        ttl = timedelta(seconds=300)
        assert adapter.expire("key", ttl) is True

    def test_ttl_delegates_to_inner(self, adapter, mock_delegate):
        """ttl() delegates to the wrapped adapter."""
        mock_delegate.ttl.return_value = 120
        assert adapter.ttl("key") == 120
        mock_delegate.ttl.assert_called_once_with("key")

    def test_setnx_delegates_to_inner(self, adapter, mock_delegate):
        """setnx() delegates to the wrapped adapter."""
        mock_delegate.setnx.return_value = True
        assert adapter.setnx("key", "val", timedelta(seconds=10)) is True

    def test_cas_dict_field_delegates_to_inner(self, adapter, mock_delegate):
        """cas_dict_field() forwards every argument verbatim to the inner adapter."""
        mock_delegate.cas_dict_field.return_value = True
        ttl = timedelta(seconds=30)
        new_value = {"status": "completed", "result": {"ok": True}}

        result = adapter.cas_dict_field("k", "status", "executing", new_value, ttl)

        mock_delegate.cas_dict_field.assert_called_once_with(
            "k", "status", "executing", new_value, ttl
        )
        assert result is True

    def test_cas_dict_field_returns_inner_false(self, adapter, mock_delegate):
        """cas_dict_field() propagates a False return from the inner adapter."""
        mock_delegate.cas_dict_field.return_value = False
        assert (
            adapter.cas_dict_field("k", "status", "executing", {"status": "completed"})
            is False
        )

    def test_cas_takeover_delegates_to_inner(self, adapter, mock_delegate):
        """cas_takeover() forwards every argument verbatim to the inner adapter.

        673 D1: the gate holds the WRAPPED adapter, so without this delegation
        the call would resolve to the base non-atomic ``get→check→set`` default
        (silently re-opening the takeover race) even though the atomicity
        validator unwrapped to the concrete adapter. The ``stale_before`` /
        ``ttl`` kwargs MUST reach the delegate unchanged.
        """
        mock_delegate.cas_takeover.return_value = True
        new_record = {"status": "executing", "started_at": 123.0}
        ttl = timedelta(seconds=30)

        result = adapter.cas_takeover("k", new_record, stale_before=100.0, ttl=ttl)

        mock_delegate.cas_takeover.assert_called_once_with(
            "k", new_record, stale_before=100.0, ttl=ttl
        )
        assert result is True

    def test_cas_takeover_returns_inner_false(self, adapter, mock_delegate):
        """cas_takeover() propagates a False (takeover lost) return unchanged."""
        mock_delegate.cas_takeover.return_value = False
        assert (
            adapter.cas_takeover("k", {"status": "executing"}, stale_before=1.0)
            is False
        )

    def test_get_lock_delegates_to_inner(self, adapter, mock_delegate):
        """get_lock() delegates to the wrapped adapter."""
        mock_lock = MagicMock()
        mock_delegate.get_lock.return_value = mock_lock
        result = adapter.get_lock("my_lock")
        mock_delegate.get_lock.assert_called_once()
        assert result is mock_lock

    def test_mget_delegates_to_inner(self, adapter, mock_delegate):
        """mget() delegates to the wrapped adapter."""
        mock_delegate.mget.return_value = {"a": 1, "b": 2}
        result = adapter.mget(["a", "b"])
        assert result == {"a": 1, "b": 2}

    def test_mset_delegates_to_inner(self, adapter, mock_delegate):
        """mset() delegates to the wrapped adapter."""
        mock_delegate.mset.return_value = True
        assert adapter.mset({"a": 1, "b": 2}) is True

    def test_mdelete_delegates_to_inner(self, adapter, mock_delegate):
        """mdelete() delegates to the wrapped adapter."""
        mock_delegate.mdelete.return_value = 2
        assert adapter.mdelete(["a", "b"]) == 2
        mock_delegate.mdelete.assert_called_once_with(["a", "b"])

    def test_hget_delegates_to_inner(self, adapter, mock_delegate):
        """hget() delegates to the wrapped adapter."""
        mock_delegate.hget.return_value = "field_val"
        assert adapter.hget("hash", "field") == "field_val"
        mock_delegate.hget.assert_called_once_with("hash", "field")

    def test_hset_delegates_to_inner(self, adapter, mock_delegate):
        """hset() delegates to the wrapped adapter."""
        mock_delegate.hset.return_value = True
        assert adapter.hset("hash", "field", "val") is True

    def test_hgetall_delegates_to_inner(self, adapter, mock_delegate):
        """hgetall() delegates to the wrapped adapter."""
        mock_delegate.hgetall.return_value = {"f1": "v1"}
        assert adapter.hgetall("hash") == {"f1": "v1"}

    def test_push_limit_delegates_to_inner(self, adapter, mock_delegate):
        """push_limit() delegates to the wrapped adapter."""
        mock_delegate.push_limit.return_value = 42
        ttl = timedelta(seconds=120)
        result = adapter.push_limit("list_key", {"data": 1}, max_len=100, ttl=ttl)
        mock_delegate.push_limit.assert_called_once_with(
            "list_key", {"data": 1}, 100, ttl
        )
        assert result == 42

    def test_list_range_delegates_to_inner(self, adapter, mock_delegate):
        """list_range() delegates to the wrapped adapter."""
        mock_delegate.list_range.return_value = [{"a": 1}, {"b": 2}]
        result = adapter.list_range("list_key", 0, -1)
        mock_delegate.list_range.assert_called_once_with("list_key", 0, -1)
        assert result == [{"a": 1}, {"b": 2}]

    def test_health_check_delegates_to_inner(self, adapter, mock_delegate):
        """health_check() delegates to the wrapped adapter."""
        mock_delegate.health_check.return_value = True
        assert adapter.health_check() is True

    def test_flush_all_delegates_to_inner(self, adapter, mock_delegate):
        """flush_all() delegates to the wrapped adapter."""
        mock_delegate.flush_all.return_value = True
        assert adapter.flush_all() is True

    def test_keys_delegates_to_inner(self, adapter, mock_delegate):
        """keys() delegates to the wrapped adapter."""
        mock_delegate.keys.return_value = ["k1", "k2"]
        assert adapter.keys("k*") == ["k1", "k2"]

    def test_scan_delegates_to_inner(self, adapter, mock_delegate):
        """scan() delegates to the wrapped adapter."""
        mock_delegate.scan.return_value = (0, ["k1"])
        assert adapter.scan("k*", 50) == (0, ["k1"])

    def test_provider_name_delegates_to_inner(self, adapter, mock_delegate):
        """provider_name property delegates to the wrapped adapter."""
        mock_delegate.provider_name = "TestProvider"
        assert adapter.provider_name == "TestProvider"


class TestMetricsAwareCacheAdapterMetricsBehavior:
    """Drift metrics are recorded on get/set operations."""

    @patch(
        "baldur.adapters.cache.metrics_decorator.HAS_DRIFT_METRICS",
        True,
    )
    @patch(
        "baldur.adapters.cache.metrics_decorator.record_cache_get",
        autospec=True,
    )
    def test_get_hit_records_hit_metric(self, mock_record, mock_delegate, adapter):
        """Cache hit records 'hit' metric with delegate class name."""
        mock_delegate.get.return_value = "found"
        adapter.get("key")
        mock_record.assert_called_once_with("MagicMock", "hit")

    @patch(
        "baldur.adapters.cache.metrics_decorator.HAS_DRIFT_METRICS",
        True,
    )
    @patch(
        "baldur.adapters.cache.metrics_decorator.record_cache_get",
        autospec=True,
    )
    def test_get_miss_records_miss_metric(self, mock_record, mock_delegate, adapter):
        """Cache miss records 'miss' metric."""
        mock_delegate.get.return_value = None
        adapter.get("key")
        mock_record.assert_called_once_with("MagicMock", "miss")

    @patch(
        "baldur.adapters.cache.metrics_decorator.HAS_DRIFT_METRICS",
        True,
    )
    @patch(
        "baldur.adapters.cache.metrics_decorator.record_cache_set",
        autospec=True,
    )
    def test_set_records_set_metric(self, mock_record, mock_delegate, adapter):
        """set() records cache set metric."""
        mock_delegate.set.return_value = True
        adapter.set("key", "value")
        mock_record.assert_called_once_with("MagicMock")

    @patch(
        "baldur.adapters.cache.metrics_decorator.HAS_DRIFT_METRICS",
        False,
    )
    @patch(
        "baldur.adapters.cache.metrics_decorator.record_cache_get",
        autospec=True,
    )
    def test_get_skips_metrics_when_unavailable(
        self, mock_record, mock_delegate, adapter
    ):
        """No metrics recorded when drift_metrics is not available."""
        mock_delegate.get.return_value = "val"
        adapter.get("key")
        mock_record.assert_not_called()


class TestMetricsAwareCacheAdapterContract:
    """Contract verification for MetricsAwareCacheAdapter."""

    def test_is_instance_of_cache_provider_interface(self, adapter):
        """MetricsAwareCacheAdapter implements CacheProviderInterface."""
        assert isinstance(adapter, CacheProviderInterface)

    def test_backend_name_uses_delegate_class_name(self, mock_delegate):
        """_backend_name is the class name of the delegate."""
        wrapped = MetricsAwareCacheAdapter(mock_delegate)
        assert wrapped._backend_name == type(mock_delegate).__name__
