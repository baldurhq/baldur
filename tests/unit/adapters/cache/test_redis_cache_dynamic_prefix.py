"""Unit tests for ``RedisCacheAdapter`` dynamic prefix + tri-state ``key_prefix``.

Source: ``src/baldur/adapters/cache/redis_adapter.py``

Covers (463 D8 / D9 / G11):

- Tri-state ``key_prefix`` constructor argument:
    - ``None`` (default) → per-operation ``get_effective_key_prefix()``
    - ``""`` → composer pattern (no prefix added)
    - ``"static:"`` → static literal override
- ``_make_key()`` honors the tri-state selection.
- ``_effective_prefix()`` reads ``get_effective_key_prefix()`` per call so
  ``TestModeContext`` (request-scoped ``ContextVar``) flips synthetic
  traffic into the ``xtest:`` namespace without rebuilding the adapter.
- ``flush_all()`` SCAN literal uses the dynamic prefix.
- ``keys()`` / ``scan()`` strip arithmetic uses the same per-op prefix
  string used to build the SCAN pattern (no drift if TestModeContext
  flips mid-call — the call-site captures the prefix once).

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (mock SCAN args).
- §8.4 Side effects (prefix string composition).
- §8.11 Time-dependency analogue (ContextVar read-per-op invariant).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter
from baldur.core.test_mode_context import TestModeContext
from baldur.settings.namespace import (
    NamespaceSettings,
    get_effective_key_prefix,
)


@pytest.fixture
def mock_redis_client():
    """Mock Redis client with a stand-in connection_pool attribute."""
    client = MagicMock()
    client.connection_pool = MagicMock()
    return client


@pytest.fixture
def patched_namespace_settings(monkeypatch):
    """Replace ``get_namespace_settings()`` with a per-test stub.

    Lets each test parametrize the namespace state without leaking into
    the singleton settings cache.
    """

    def _install(*, enabled: bool = False, region: str | None = None):
        stub = NamespaceSettings(namespace_enabled=enabled, region=region)
        monkeypatch.setattr(
            "baldur.settings.namespace.get_namespace_settings",
            lambda: stub,
        )
        return stub

    return _install


# ---------------------------------------------------------------------------
# Tri-state key_prefix (D9, §8.5 dependency interaction)
# ---------------------------------------------------------------------------


class TestRedisCacheAdapterPrefixTriStateBehavior:
    """Tri-state ``key_prefix`` selector — None / empty / static literal."""

    def test_none_prefix_resolves_via_effective_key_prefix(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``key_prefix=None`` → per-op dynamic prefix from helper."""
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client, key_prefix=None)

        # Expected: helper returns "baldur:" by default (no namespace).
        expected = get_effective_key_prefix()
        assert adapter._make_key("foo") == f"{expected}foo"

    def test_empty_prefix_composer_adds_no_prefix(self, mock_redis_client):
        """``key_prefix=""`` → composer pattern; key passes through unchanged."""
        adapter = RedisCacheAdapter(client=mock_redis_client, key_prefix="")
        assert adapter._make_key("foo") == "foo"

    def test_static_prefix_override_is_literal(self, mock_redis_client):
        """``key_prefix="static:"`` → literal prefix; ignores helper / TestMode."""
        adapter = RedisCacheAdapter(client=mock_redis_client, key_prefix="static:")
        # Even inside TestModeContext, the literal wins (no dynamic dispatch).
        with TestModeContext.start(session_id="xtest-static"):
            assert adapter._make_key("foo") == "static:foo"

    @pytest.mark.parametrize(
        ("prefix_arg", "expected_prefix_for_foo"),
        [
            ("", "foo"),
            ("p:", "p:foo"),
            ("multi:layer:", "multi:layer:foo"),
        ],
        ids=["empty_composer", "single_layer", "multi_layer"],
    )
    def test_static_prefix_strings_pass_through_make_key(
        self, mock_redis_client, prefix_arg, expected_prefix_for_foo
    ):
        """Any static string argument is the prefix returned verbatim."""
        adapter = RedisCacheAdapter(client=mock_redis_client, key_prefix=prefix_arg)
        assert adapter._make_key("foo") == expected_prefix_for_foo


# ---------------------------------------------------------------------------
# Dynamic prefix — TestModeContext + Namespace honored (D8 / G11)
# ---------------------------------------------------------------------------


class TestRedisCacheAdapterDynamicPrefixBehavior:
    """``_make_key`` reads the helper per call; TestMode + Namespace compose.

    The helper combinations are the same as ``ResilientStorageBackend``
    so ``@idempotent`` and ``@dlq_protect`` see identical key namespaces.
    """

    def test_dynamic_prefix_with_namespace_disabled(
        self, mock_redis_client, patched_namespace_settings
    ):
        """Namespace OFF, no TestMode → bare ``baldur:`` prefix."""
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)
        assert adapter._make_key("k") == "baldur:k"

    def test_dynamic_prefix_with_namespace_region(
        self, mock_redis_client, patched_namespace_settings
    ):
        """Namespace ON + region=seoul → ``baldur:seoul:`` prefix."""
        patched_namespace_settings(enabled=True, region="seoul")
        adapter = RedisCacheAdapter(client=mock_redis_client)
        assert adapter._make_key("k") == "baldur:seoul:k"

    def test_dynamic_prefix_with_test_mode_active(
        self, mock_redis_client, patched_namespace_settings
    ):
        """TestModeContext active → ``xtest:`` prepended to standard prefix."""
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)
        with TestModeContext.start(session_id="xtest-1"):
            assert adapter._make_key("k") == "xtest:baldur:k"

    def test_dynamic_prefix_combines_test_mode_and_namespace(
        self, mock_redis_client, patched_namespace_settings
    ):
        """TestMode + Namespace together → ``xtest:baldur:seoul:`` prefix."""
        patched_namespace_settings(enabled=True, region="seoul")
        adapter = RedisCacheAdapter(client=mock_redis_client)
        with TestModeContext.start(session_id="xtest-multi"):
            assert adapter._make_key("k") == "xtest:baldur:seoul:k"

    def test_prefix_changes_observed_per_operation(
        self, mock_redis_client, patched_namespace_settings
    ):
        """Same adapter, different ContextVar state → different prefix per call.

        This is the categorical reason the constructor cannot cache the
        prefix string: ``TestModeContext`` is request-scoped and flips
        between operations on the same long-lived adapter.
        """
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)

        outside = adapter._make_key("k")
        with TestModeContext.start(session_id="x"):
            inside = adapter._make_key("k")
        outside_again = adapter._make_key("k")

        assert outside == "baldur:k"
        assert inside == "xtest:baldur:k"
        assert outside_again == "baldur:k"


# ---------------------------------------------------------------------------
# SCAN-touching ops — flush_all / keys() / scan() use the same dynamic prefix
# ---------------------------------------------------------------------------


class TestRedisCacheAdapterScanPathsBehavior:
    """``flush_all`` / ``keys()`` / ``scan()`` build patterns from dynamic prefix.

    Strip arithmetic in ``keys()`` / ``scan()`` resolves the prefix ONCE so the
    SCAN pattern and the strip slice see identical lengths even if
    ``TestModeContext`` flipped between Python statements.
    """

    def test_flush_all_scan_pattern_uses_dynamic_prefix(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``flush_all`` SCAN pattern is ``f"{effective_prefix}*"``."""
        patched_namespace_settings(enabled=True, region="seoul")
        adapter = RedisCacheAdapter(client=mock_redis_client)

        # First scan call returns no keys + cursor=0 → loop terminates.
        mock_redis_client.scan.return_value = (0, [])

        adapter.flush_all()

        mock_redis_client.scan.assert_called_once_with(
            0, match="baldur:seoul:*", count=100
        )

    def test_flush_all_scan_pattern_includes_xtest_when_test_mode_active(
        self, mock_redis_client, patched_namespace_settings
    ):
        """Inside TestModeContext, the SCAN pattern carries the ``xtest:`` prefix."""
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)
        mock_redis_client.scan.return_value = (0, [])

        with TestModeContext.start(session_id="x"):
            adapter.flush_all()

        mock_redis_client.scan.assert_called_once_with(
            0, match="xtest:baldur:*", count=100
        )

    def test_keys_strips_dynamic_prefix_consistently(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``keys()`` strips exactly the prefix length used to build the pattern."""
        patched_namespace_settings(enabled=True, region="seoul")
        adapter = RedisCacheAdapter(client=mock_redis_client)
        # Redis returns full-prefixed keys; strip arithmetic must remove
        # the dynamic prefix, returning only "foo" / "bar".
        mock_redis_client.keys.return_value = [
            b"baldur:seoul:foo",
            b"baldur:seoul:bar",
        ]

        result = adapter.keys("*")

        mock_redis_client.keys.assert_called_once_with("baldur:seoul:*")
        assert result == ["foo", "bar"]

    def test_keys_handles_str_inputs_without_decode(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``keys()`` strip arithmetic supports already-decoded ``str`` values."""
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)
        mock_redis_client.keys.return_value = ["baldur:foo", "baldur:bar"]

        result = adapter.keys("*")

        assert result == ["foo", "bar"]

    def test_scan_strips_dynamic_prefix_consistently(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``scan()`` strip arithmetic uses the same per-op prefix."""
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)
        mock_redis_client.scan.return_value = (
            17,
            [b"baldur:foo", b"baldur:bar"],
        )

        cursor, keys = adapter.scan("*", count=50)

        mock_redis_client.scan.assert_called_once_with(0, match="baldur:*", count=50)
        assert cursor == 17
        assert keys == ["foo", "bar"]

    def test_scan_pattern_reflects_test_mode_prefix(
        self, mock_redis_client, patched_namespace_settings
    ):
        """``scan()`` SCAN pattern picks up the ``xtest:`` prefix in TestMode."""
        patched_namespace_settings(enabled=False)
        adapter = RedisCacheAdapter(client=mock_redis_client)
        mock_redis_client.scan.return_value = (0, [b"xtest:baldur:foo"])

        with TestModeContext.start(session_id="x"):
            cursor, keys = adapter.scan("*")

        mock_redis_client.scan.assert_called_once_with(
            0, match="xtest:baldur:*", count=100
        )
        assert keys == ["foo"]


# ---------------------------------------------------------------------------
# Empty-prefix composer pattern — flush_all/keys/scan match all keys
# ---------------------------------------------------------------------------


class TestRedisCacheAdapterEmptyPrefixComposerBehavior:
    """When ``key_prefix=""``, prefix is empty; SCAN patterns match raw input."""

    def test_flush_all_empty_prefix_matches_all_keys(self, mock_redis_client):
        """Composer pattern: ``flush_all`` issues ``"*"`` SCAN with no prefix."""
        adapter = RedisCacheAdapter(client=mock_redis_client, key_prefix="")
        mock_redis_client.scan.return_value = (0, [])

        adapter.flush_all()

        mock_redis_client.scan.assert_called_once_with(0, match="*", count=100)

    def test_keys_empty_prefix_returns_full_key_strings(self, mock_redis_client):
        """Strip length is 0 → keys() returns the SCAN result verbatim."""
        adapter = RedisCacheAdapter(client=mock_redis_client, key_prefix="")
        mock_redis_client.keys.return_value = [b"foo", b"bar"]

        result = adapter.keys("*")

        assert result == ["foo", "bar"]
