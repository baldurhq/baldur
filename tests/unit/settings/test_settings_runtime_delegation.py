"""Delegation tests for the 13 settings wrappers (#450 Phase 1, D3).

Sources:
- ``src/baldur/settings/admin.py``        — ``AdminServerSettings``
- ``src/baldur/settings/api_view.py``     — ``ApiViewSettings``
- ``src/baldur/settings/arq_task.py``     — ``ArqTaskSettings``
- ``src/baldur/settings/batch.py``        — ``BatchSettings``
- ``src/baldur/settings/channel_routing.py`` — ``ChannelRoutingSettings``
- ``src/baldur/settings/channel_target.py``  — ``ChannelTargetSettings``
- ``src/baldur/settings/event_buffer.py`` — ``EventBufferSettings``
- ``src/baldur/settings/postgres.py``     — ``PostgresSettings``
- ``src/baldur/settings/protect.py``      — ``ProtectSettings``
- ``src/baldur/settings/s3.py``           — ``S3Settings``
- ``src/baldur/settings/scale.py``        — ``ScaleSettings``
- ``src/baldur/settings/slack_channel.py``— ``SlackChannelSettings``
- ``src/baldur/settings/sql.py``          — ``SQLSettings``

Per the 450 D3 contract:

- ``get_xxx_settings()`` MUST be a 1-line delegation to
  ``get_runtime().get_settings(XxxSettings)``.
- ``reset_xxx_settings()`` MUST be a 1-line delegation to
  ``get_runtime().reset_settings(XxxSettings)``.
- No module-level ``_xxx_settings: T | None`` cache may remain — the cache
  lives on the runtime, so two consecutive ``get_xxx_settings()`` calls
  must return the same object identity AND a ``reset_runtime()`` must wipe
  that cache.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5  Dependency interaction — wrappers invoke the runtime, not a
  module-level slot.
- §8.10 Singleton/lifecycle — get → reset → get yields a fresh instance.
- §8.3  Idempotency — repeated get returns the same instance.
"""

from __future__ import annotations

from typing import Any

import pytest

from baldur import runtime as runtime_module
from baldur.runtime import BaldurRuntime, set_runtime
from baldur.settings.admin import (
    AdminServerSettings,
    get_admin_server_settings,
    reset_admin_server_settings,
)
from baldur.settings.api_view import (
    ApiViewSettings,
    get_api_view_settings,
    reset_api_view_settings,
)
from baldur.settings.arq_task import (
    ArqTaskSettings,
    get_arq_task_settings,
    reset_arq_task_settings,
)
from baldur.settings.batch import (
    BatchSettings,
    get_batch_settings,
    reset_batch_settings,
)
from baldur.settings.channel_routing import (
    ChannelRoutingSettings,
    get_channel_routing_settings,
    reset_channel_routing_settings,
)
from baldur.settings.channel_target import (
    ChannelTargetSettings,
    get_channel_target_settings,
    reset_channel_target_settings,
)
from baldur.settings.event_buffer import (
    EventBufferSettings,
    get_event_buffer_settings,
    reset_event_buffer_settings,
)
from baldur.settings.postgres import (
    PostgresSettings,
    get_postgres_settings,
    reset_postgres_settings,
)
from baldur.settings.protect import (
    ProtectSettings,
    get_protect_settings,
    reset_protect_settings,
)
from baldur.settings.s3 import (
    S3Settings,
    get_s3_settings,
    reset_s3_settings,
)
from baldur.settings.scale import (
    ScaleSettings,
    get_scale_settings,
    reset_scale_settings,
)
from baldur.settings.slack_channel import (
    SlackChannelSettings,
    get_slack_channel_settings,
    reset_slack_channel_settings,
)
from baldur.settings.sql import (
    SQLSettings,
    get_sql_settings,
    reset_sql_settings,
)

# (label, getter, resetter, settings_cls)
SETTINGS_WRAPPERS: list[tuple[str, Any, Any, type]] = [
    (
        "admin",
        get_admin_server_settings,
        reset_admin_server_settings,
        AdminServerSettings,
    ),
    ("api_view", get_api_view_settings, reset_api_view_settings, ApiViewSettings),
    ("arq_task", get_arq_task_settings, reset_arq_task_settings, ArqTaskSettings),
    ("batch", get_batch_settings, reset_batch_settings, BatchSettings),
    (
        "channel_routing",
        get_channel_routing_settings,
        reset_channel_routing_settings,
        ChannelRoutingSettings,
    ),
    (
        "channel_target",
        get_channel_target_settings,
        reset_channel_target_settings,
        ChannelTargetSettings,
    ),
    (
        "event_buffer",
        get_event_buffer_settings,
        reset_event_buffer_settings,
        EventBufferSettings,
    ),
    ("postgres", get_postgres_settings, reset_postgres_settings, PostgresSettings),
    ("protect", get_protect_settings, reset_protect_settings, ProtectSettings),
    ("s3", get_s3_settings, reset_s3_settings, S3Settings),
    ("scale", get_scale_settings, reset_scale_settings, ScaleSettings),
    (
        "slack_channel",
        get_slack_channel_settings,
        reset_slack_channel_settings,
        SlackChannelSettings,
    ),
    ("sql", get_sql_settings, reset_sql_settings, SQLSettings),
]

WRAPPER_IDS = [w[0] for w in SETTINGS_WRAPPERS]


@pytest.fixture(autouse=True)
def _swap_isolated_runtime():
    """Isolate this file's runtime so injected overrides do not leak globally."""
    original_var = runtime_module._runtime_var.get()
    isolated = BaldurRuntime()
    token = set_runtime(isolated)
    try:
        yield isolated
    finally:
        runtime_module._runtime_var.reset(token)
        # Belt-and-suspenders: ensure the post-test slot matches the pre-test slot.
        runtime_module._runtime_var.set(original_var)


# ---------------------------------------------------------------------------
# Contract — wrappers exist as expected and resolve to the right cls
# ---------------------------------------------------------------------------


class TestSettingsWrapperContract:
    """Each pair returns an instance of the documented Settings class."""

    @pytest.mark.parametrize(
        ("getter", "resetter", "cls"),
        [(g, r, c) for _, g, r, c in SETTINGS_WRAPPERS],
        ids=WRAPPER_IDS,
    )
    def test_get_returns_instance_of_settings_class(self, getter, resetter, cls):
        """``get_xxx_settings()`` returns an instance of ``XxxSettings``."""
        instance = getter()
        try:
            assert isinstance(instance, cls)
        finally:
            resetter()


# ---------------------------------------------------------------------------
# Behavior — delegation to the active runtime (no module-level cache)
# ---------------------------------------------------------------------------


class TestSettingsWrapperDelegationBehavior:
    """Wrappers must read/write through the active ``BaldurRuntime``."""

    @pytest.mark.parametrize(
        ("getter", "resetter", "cls"),
        [(g, r, c) for _, g, r, c in SETTINGS_WRAPPERS],
        ids=WRAPPER_IDS,
    )
    def test_get_returns_runtime_cached_instance(
        self, _swap_isolated_runtime, getter, resetter, cls
    ):
        """``get_xxx_settings`` returns the runtime's cached instance.

        Verifies the wrapper resolves through ``runtime.get_settings(cls)``:
        the runtime's per-cls cache and the wrapper's return value share
        identity.
        """
        from_wrapper = getter()
        from_runtime = _swap_isolated_runtime.get_settings(cls)
        try:
            assert from_wrapper is from_runtime
        finally:
            resetter()

    @pytest.mark.parametrize(
        ("getter", "resetter", "cls"),
        [(g, r, c) for _, g, r, c in SETTINGS_WRAPPERS],
        ids=WRAPPER_IDS,
    )
    def test_repeated_get_returns_same_instance(self, getter, resetter, cls):
        """Idempotency: two consecutive ``get_xxx_settings`` calls share identity."""
        try:
            first = getter()
            second = getter()
            assert first is second
        finally:
            resetter()

    @pytest.mark.parametrize(
        ("getter", "resetter", "cls"),
        [(g, r, c) for _, g, r, c in SETTINGS_WRAPPERS],
        ids=WRAPPER_IDS,
    )
    def test_reset_drops_runtime_cache(
        self, _swap_isolated_runtime, getter, resetter, cls
    ):
        """``reset_xxx_settings`` clears only the runtime entry for that cls."""
        first = getter()
        # Sanity: cached in the runtime
        assert _swap_isolated_runtime.get_settings(cls) is first

        resetter()

        # The runtime cache for this cls must be gone — next get builds anew.
        second = getter()
        try:
            assert second is not first
        finally:
            resetter()

    @pytest.mark.parametrize(
        ("getter", "resetter", "cls"),
        [(g, r, c) for _, g, r, c in SETTINGS_WRAPPERS],
        ids=WRAPPER_IDS,
    )
    def test_set_settings_override_visible_through_wrapper(
        self, _swap_isolated_runtime, getter, resetter, cls
    ):
        """Injecting via ``runtime.set_settings`` is observable via the wrapper.

        Confirms the wrapper does not bypass the runtime by reading from a
        leftover module-level slot.
        """
        # Build the sentinel through the cls itself so the field defaults
        # supply any required values.
        override = cls()
        _swap_isolated_runtime.set_settings(cls, override)

        try:
            assert getter() is override
        finally:
            resetter()


# ---------------------------------------------------------------------------
# Behavior — runtime swap drops every wrapper's cache (D3 contract)
# ---------------------------------------------------------------------------


class TestSettingsRuntimeSwapBehavior:
    """Swapping in a fresh runtime must invalidate every wrapper's cache."""

    @pytest.mark.parametrize(
        ("getter", "resetter", "_cls"),
        [(g, r, c) for _, g, r, c in SETTINGS_WRAPPERS],
        ids=WRAPPER_IDS,
    )
    def test_runtime_swap_yields_fresh_instance(self, getter, resetter, _cls):
        """A new runtime → wrappers return a different instance from the prior runtime."""
        first = getter()

        # Swap in a brand-new runtime — equivalent to per-test isolation.
        new_runtime = BaldurRuntime()
        token = set_runtime(new_runtime)
        try:
            second = getter()
            assert second is not first
        finally:
            runtime_module._runtime_var.reset(token)
            resetter()
