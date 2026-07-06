"""Module-import-time contract tests for ``ProviderRegistry`` defaults (#464).

Source: ``src/baldur/factory/registry.py`` (``set_default(...)`` block,
lines ~1000-1030).

Two registries get specifically reclassified by 464 and need their
post-import default-name pinned via Contract tests:

- **``security_repo``** (D7): default was ``"redis"`` (no adapter ever
  existed → ``AdapterNotFoundError`` on first call). 464 D7 reclassifies
  it as a Group B (SQL/Django) row and changes the module-load default to
  ``"memory"``. ``init()`` re-asserts an environment-aware default on top
  per the wiring step.
- **``mesh_override_store``** (D8): vestigial registry. Production
  ``circuit_mesh`` path constructs ``TwoTierMeshOverrideStore`` directly
  via cache injection, so the registry's default-name resolution is never
  exercised in production. Default stays ``"memory"`` and the docstring on
  ``discover_mesh_override_stores`` carries the cross-reference to the
  real production callsite.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8 Constant pinning (default name).
- Documentation introspection (D8 cross-reference is part of the
  mesh_override_store contract — drift would silently mislead future
  contributors looking for the L2 backend wire-up).
"""

from __future__ import annotations

import pytest


class TestSecurityRepoDefaultNameContract:
    """464 D7 — ``security_repo`` default is ``"memory"``, not ``"redis"``."""

    @pytest.mark.flaky_quarantine(
        issue="provider-registry-reset-default-asymmetry",
        first_seen="2026-05-07",
        category="state_leak",
    )
    def test_security_repo_module_load_default_is_memory(self):
        """``ProviderRegistry.security_repo`` resolves to ``"memory"`` at import.

        Quarantined: ``ProviderRegistry.reset()`` clears ``_default = None`` on
        all sub-registries but conftest ``_post_reset_provider_registry`` only
        re-asserts ``cache``/``queue`` defaults — other registries rely on
        first-registered-wins via ``_auto_register_adapters()`` re-run, which
        is xdist-schedule-sensitive.
        """
        from baldur.factory.registry import ProviderRegistry

        assert ProviderRegistry.security_repo.get_default_name() == "memory"

    def test_security_repo_default_resolves_to_a_registered_adapter(self):
        """Sanity: the ``"memory"`` adapter exists (no AdapterNotFoundError)."""
        from baldur.factory.registry import ProviderRegistry

        # discover_security_repos must register a "memory" provider.
        ProviderRegistry.security_repo.get_provider("memory")

    def test_security_repo_no_redis_provider_registered(self):
        """No Redis adapter for security_repo exists today (D7 rationale)."""
        from baldur.factory.registry import ProviderRegistry

        # `discover_*` is invoked lazily by `get_provider` so trigger it once.
        ProviderRegistry.security_repo.list_providers()
        assert not ProviderRegistry.security_repo.has_provider("redis")


class TestMeshOverrideStoreVestigialContract:
    """464 D8 / 599 D7 — ``mesh_override_store`` is a vestigial API-compat slot.

    The store implementation moved to the private distribution with the
    circuit_mesh feature; the slot keeps register_mesh_override_store /
    get_mesh_override_store importable but is EMPTY on OSS installs (no
    auto_discover, no module-load registration, no default).
    """

    def test_slot_exists_and_is_empty_on_oss(self):
        """The slot survives for API compat but ships no provider/default."""
        from baldur.factory.registry import ProviderRegistry

        slot = ProviderRegistry.mesh_override_store
        assert not slot.has_provider("memory")
        assert slot.get_default_name() is None
        assert slot._auto_discover is None

    def test_discover_function_removed(self):
        """discover_mesh_override_stores no longer exists in repositories."""
        import baldur.factory.repositories as repositories

        assert not hasattr(repositories, "discover_mesh_override_stores")
        assert "discover_mesh_override_stores" not in repositories.__all__


class TestGetEventJournalRepoBehavior:
    """570 D2 — ``get_event_journal_repo()`` consults the registry default
    instead of the (removed) ``EventJournalSettings.backend`` field.

    With ``name=None`` it delegates to ``event_journal_repo.get(None)`` so
    the wired registry default decides the backend; an explicit ``name``
    bypasses it. Each test registers a connection-free stub provider under
    ``snapshot()`` so the routing is observed without redis/sql clients.

    Verification techniques (per UNIT_TEST_GUIDELINES §8):
    - §8.5 Dependency interaction (name resolution routes through the
      registry default, not settings).
    - §8.3 Idempotency / §8.10 Singleton (cached vs fresh instances).
    """

    def test_default_name_resolves_through_registry_default(self):
        """``name=None`` returns the instance for the *registry* default,
        proving D2 routes through the registry (not ``settings.backend``)."""
        from baldur.factory.registry import ProviderRegistry

        sentinel = object()
        reg = ProviderRegistry.event_journal_repo
        with reg.snapshot():
            reg.register("test_default_probe", lambda: sentinel)
            reg.set_default("test_default_probe")
            reg.clear_instances()

            result = ProviderRegistry.get_event_journal_repo()

        assert result is sentinel

    def test_explicit_name_bypasses_registry_default(self):
        """An explicit ``name`` is used verbatim, bypassing the wired default."""
        from baldur.factory.registry import ProviderRegistry

        sentinel_default = object()
        sentinel_explicit = object()
        reg = ProviderRegistry.event_journal_repo
        with reg.snapshot():
            reg.register("probe_default", lambda: sentinel_default)
            reg.register("probe_explicit", lambda: sentinel_explicit)
            reg.set_default("probe_default")
            reg.clear_instances()

            result = ProviderRegistry.get_event_journal_repo(name="probe_explicit")

        assert result is sentinel_explicit

    def test_singleton_returns_cached_instance(self):
        """``singleton=True`` (default) returns the same cached instance."""
        from baldur.factory.registry import ProviderRegistry

        reg = ProviderRegistry.event_journal_repo
        with reg.snapshot():
            reg.register("probe_singleton", object)
            reg.set_default("probe_singleton")
            reg.clear_instances()

            first = ProviderRegistry.get_event_journal_repo()
            second = ProviderRegistry.get_event_journal_repo()

        assert first is second

    def test_non_singleton_returns_fresh_instances(self):
        """``singleton=False`` returns a fresh instance on each call."""
        from baldur.factory.registry import ProviderRegistry

        reg = ProviderRegistry.event_journal_repo
        with reg.snapshot():
            reg.register("probe_fresh", object)
            reg.set_default("probe_fresh")
            reg.clear_instances()

            first = ProviderRegistry.get_event_journal_repo(singleton=False)
            second = ProviderRegistry.get_event_journal_repo(singleton=False)

        assert first is not second
