"""
Tests for StateBackend get_all() pattern asymmetry fix (435).

Test Categories:
    A. FileStateBackend key encoding round-trip (D3)
    B. Cross-backend get_all contract (D5) — parametrized across File/Memory/Redis
    C. Redis get_all prefix stripping correctness (D6)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.state_backend import (
    FileStateBackend,
    MemoryStateBackend,
    RedisStateBackend,
)

# =============================================================================
# A. FileStateBackend key encoding round-trip (D3)
# =============================================================================


class TestFileStateBackendKeyEncodingBehavior:
    """FileStateBackend _encode/_decode round-trip preserves raw keys."""

    @pytest.mark.parametrize(
        "raw_key",
        [
            "simple_key",
            "baldur:runbook:execution:abc",
            "chaos:running:exp-123",
            "baldur:runbook:approval:req-456",
            "path/to/resource",
            "mixed:colon/slash_under.dot-dash",
            "key with spaces",
            "",
            "a",
            "unicode_키_テスト",
        ],
        ids=[
            "underscore_only",
            "colon_separated",
            "colon_with_dash",
            "colon_deep_nesting",
            "slash_separated",
            "mixed_special_chars",
            "spaces",
            "empty_string",
            "single_char",
            "unicode",
        ],
    )
    def test_encode_decode_round_trip_preserves_key(self, tmp_path, raw_key):
        """encode → decode round-trip returns the original key verbatim."""
        backend = FileStateBackend(directory=tmp_path / "state")

        encoded = backend._encode_key_for_filename(raw_key)
        decoded = backend._decode_key_from_filename(encoded)

        assert decoded == raw_key

    def test_encoded_filename_is_filesystem_safe(self, tmp_path):
        """Encoded key contains no filesystem-forbidden characters."""
        backend = FileStateBackend(directory=tmp_path / "state")
        dangerous_key = 'baldur:state:<test>"/\\|?*'

        encoded = backend._encode_key_for_filename(dangerous_key)

        forbidden = set('<>:"/\\|?*')
        assert not any(c in forbidden for c in encoded)

    def test_set_get_round_trip_with_colon_key(self, tmp_path):
        """set() then get() with colon-containing key returns correct value."""
        backend = FileStateBackend(directory=tmp_path / "state")
        key = "baldur:runbook:execution:abc-123"
        value = {"status": "running", "step": 3}

        backend.set(key, value)
        result = backend.get(key)

        assert result == value

    def test_delete_with_colon_key(self, tmp_path):
        """delete() correctly removes files for colon-containing keys."""
        backend = FileStateBackend(directory=tmp_path / "state")
        key = "chaos:running:exp-789"

        backend.set(key, {"active": True})
        assert backend.exists(key) is True

        deleted = backend.delete(key)

        assert deleted is True
        assert backend.exists(key) is False


# =============================================================================
# B. Cross-backend get_all contract (D5)
# =============================================================================


def _make_redis_backend_with_memory_store() -> tuple[RedisStateBackend, dict[str, str]]:
    """Create a RedisStateBackend backed by an in-memory dict for unit testing."""
    store: dict[str, str] = {}
    mock_client = MagicMock()

    def mock_set(key, value):
        store[key] = value

    def mock_setex(key, ttl, value):
        store[key] = value

    def mock_get(key):
        return store.get(key)

    def mock_scan_iter(match="*", count=100):
        import fnmatch

        for k in list(store.keys()):
            if fnmatch.fnmatchcase(k, match):
                yield k

    mock_client.set = mock_set
    mock_client.setex = mock_setex
    mock_client.get = mock_get
    mock_client.scan_iter = mock_scan_iter

    with patch.object(RedisStateBackend, "_initialize_client"):
        backend = RedisStateBackend(
            redis_url="redis://test:6379/0",
            key_prefix="baldur:state:",
        )
    backend._client = mock_client
    return backend, store


@pytest.fixture(params=["file", "memory", "redis"], ids=["File", "Memory", "Redis"])
def backend(request, tmp_path) -> Any:
    """Parametrized fixture providing all 3 backend implementations."""
    if request.param == "file":
        return FileStateBackend(directory=tmp_path / "state")
    elif request.param == "memory":
        return MemoryStateBackend()
    elif request.param == "redis":
        backend, _ = _make_redis_backend_with_memory_store()
        return backend


class TestStateBackendGetAllCrossBackendBehavior:
    """Cross-backend contract: get_all() behaves identically across all backends."""

    def test_get_all_wildcard_returns_all_entries(self, backend):
        """get_all('*') returns all stored entries."""
        backend.set("key1", {"v": 1})
        backend.set("key2", {"v": 2})
        backend.set("key3", {"v": 3})

        result = backend.get_all("*")

        assert len(result) == 3
        assert result["key1"] == {"v": 1}
        assert result["key2"] == {"v": 2}
        assert result["key3"] == {"v": 3}

    def test_get_all_prefix_glob_matches_only_prefixed_entries(self, backend):
        """get_all('prefix:*') matches only keys starting with 'prefix:'."""
        backend.set("chaos:running:exp1", {"id": "exp1"})
        backend.set("chaos:running:exp2", {"id": "exp2"})
        backend.set("baldur:runbook:exec1", {"id": "exec1"})

        result = backend.get_all("chaos:running:*")

        assert len(result) == 2
        assert "chaos:running:exp1" in result
        assert "chaos:running:exp2" in result
        assert "baldur:runbook:exec1" not in result

    def test_get_all_suffix_glob_matches_only_suffixed_entries(self, backend):
        """get_all('*:suffix') matches only keys ending with ':suffix'."""
        backend.set("ns1:tracker:active", {"ns": "ns1"})
        backend.set("ns2:tracker:active", {"ns": "ns2"})
        backend.set("ns3:tracker:inactive", {"ns": "ns3"})

        result = backend.get_all("*:active")

        assert len(result) == 2
        assert "ns1:tracker:active" in result
        assert "ns2:tracker:active" in result

    def test_get_all_returns_raw_keys_matching_set_input(self, backend):
        """Returned keys preserve the exact format used in set()."""
        raw_key = "baldur:runbook:execution:abc-123"
        backend.set(raw_key, {"status": "done"})

        result = backend.get_all("baldur:runbook:execution:*")

        assert raw_key in result
        assert result[raw_key] == {"status": "done"}

    def test_get_all_set_round_trip(self, backend):
        """set(k, v) → get_all(pattern matching k) returns {k: v}."""
        key = "baldur:daily_reports:2026-04-18"
        value = {"report": "data", "count": 42}

        backend.set(key, value)
        result = backend.get_all("baldur:daily_reports:*")

        assert result == {key: value}

    def test_get_all_no_match_returns_empty(self, backend):
        """get_all with non-matching pattern returns empty dict."""
        backend.set("chaos:running:exp1", {"id": "exp1"})

        result = backend.get_all("nonexistent:*")

        assert result == {}

    def test_get_all_empty_store_returns_empty(self, backend):
        """get_all on empty backend returns empty dict."""
        result = backend.get_all("*")

        assert result == {}

    def test_get_all_with_colon_prefix_pattern_from_chaos_scheduler(self, backend):
        """Regression: chaos_scheduler.py zombie hunter pattern works."""
        backend.set("chaos:running:exp-aaa", {"started": "2026-01-01"})
        backend.set("chaos:running:exp-bbb", {"started": "2026-01-02"})
        backend.set("chaos:config:global", {"timeout": 300})

        result = backend.get_all("chaos:running:*")

        assert len(result) == 2
        for key in result:
            experiment_id = key.removeprefix("chaos:running:")
            assert experiment_id in ("exp-aaa", "exp-bbb")

    def test_get_all_with_colon_prefix_pattern_from_runbook_tasks(self, backend):
        """Regression: runbook/tasks.py orphan scan pattern works."""
        backend.set("baldur:runbook:execution:run-001", {"step": 1})
        backend.set("baldur:runbook:execution:run-002", {"step": 5})

        result = backend.get_all("baldur:runbook:execution:*")

        assert len(result) == 2
        assert "baldur:runbook:execution:run-001" in result
        assert "baldur:runbook:execution:run-002" in result

    def test_get_all_with_colon_prefix_pattern_from_approval_gate(self, backend):
        """Regression: approval_gate.py waiting requests pattern works."""
        backend.set("baldur:runbook:approval:req-1", {"status": "waiting"})

        result = backend.get_all("baldur:runbook:approval:*")

        assert len(result) == 1
        assert "baldur:runbook:approval:req-1" in result

    def test_get_all_with_infix_glob_from_tracker(self, backend):
        """Regression: tracker.py active namespace scan pattern works."""
        state_key_pattern = ":emergency:state"
        backend.set("baldur:ns1:emergency:state", {"level": "HIGH"})
        backend.set("baldur:ns2:emergency:state", {"level": "LOW"})
        backend.set("baldur:ns3:emergency:config", {"timeout": 60})

        result = backend.get_all(f"*{state_key_pattern}")

        assert len(result) == 2
        for key in result:
            parts = key.split(":")
            ns = parts[1] if parts[0] == "baldur" else parts[0]
            assert ns in ("ns1", "ns2")


# =============================================================================
# C. Redis get_all prefix stripping correctness (D6)
# =============================================================================


class TestRedisGetAllPrefixStrippingBehavior:
    """Redis get_all uses removeprefix, not replace, for key stripping (D6)."""

    def test_removeprefix_preserves_key_body_containing_prefix_string(self):
        """Key body containing the prefix string is not corrupted by removeprefix."""
        # Given
        backend, store = _make_redis_backend_with_memory_store()
        # Key whose body contains the prefix string "baldur:state:"
        key_with_prefix_in_body = "meta:baldur:state:snapshot"
        full_key = f"baldur:state:{key_with_prefix_in_body}"
        store[full_key] = '{"data": "test"}'

        # When
        result = backend.get_all("*")

        # Then — removeprefix strips only the leading prefix
        assert key_with_prefix_in_body in result
        assert result[key_with_prefix_in_body] == {"data": "test"}

    def test_removeprefix_strips_only_leading_prefix(self):
        """Normal key has prefix stripped correctly."""
        backend, store = _make_redis_backend_with_memory_store()
        store["baldur:state:chaos:running:exp1"] = '{"active": true}'

        result = backend.get_all("chaos:running:*")

        assert "chaos:running:exp1" in result

    def test_replace_would_corrupt_key_with_embedded_prefix(self):
        """Demonstrates the D6 bug: str.replace corrupts keys with embedded prefix."""
        prefix = "baldur:state:"
        full_key = "baldur:state:meta:baldur:state:snapshot"

        replace_result = full_key.replace(prefix, "")
        removeprefix_result = full_key.removeprefix(prefix)

        assert replace_result == "meta:snapshot"  # corrupted!
        assert removeprefix_result == "meta:baldur:state:snapshot"  # correct
