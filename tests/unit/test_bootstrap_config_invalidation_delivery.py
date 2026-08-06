"""Config-invalidation delivery — the process's single CONFIG_UPDATED subscriber
and the background-worker starter that installs it.

The dispatcher is the latency shortcut on top of the delivery poll: it refreshes
whichever domain an event names, by dispatching on the event's ``config_type``
through the invalidation registry rather than through a handler per domain.
Three properties of it are wiring rather than logic, and each has a concrete way
to be silently wrong:

- its **name** must differ from every existing subscriber's, because the bus
  de-duplicates subscriptions by ``__name__`` and all three existing
  ``CONFIG_UPDATED`` handlers share one — a fourth carrying it is dropped with
  a DEBUG line and the whole fast path never fires;
- its subscription must be **fire-and-forget**, because the manager publishes
  while holding its own lock and an awaited handler that re-enters that lock
  makes every value-changing write pay the handler timeout;
- it must **reload the stored section before rebuilding**, because the consuming
  services read the manager's cached values and invalidating them alone would
  rebuild from the same stale cache on every process that did not serve the
  write.

Targets:
    - ``baldur.bootstrap._dispatch_config_invalidation``
    - ``baldur.bootstrap._setup_config_invalidation_delivery``
    - membership in ``_BACKGROUND_WORKER_STARTERS``

Verification techniques (§8):
    - Dependency interaction — reload-then-invoke ordering, and the resolution
      that is *not* attempted for an unregistered domain
    - Exception/edge — a raising provider, a manager with no reload surface, a
      payload with no ``config_type``
    - Idempotency — the ``post_worker_init`` re-run subscribes and registers once
    - Side effects — the subscription's own ``await_result`` flag
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur import bootstrap
from baldur.core.config_invalidation import (
    get_config_invalidation_targets,
    register_config_invalidation_target,
    reset_config_invalidation_targets,
)
from baldur.interfaces.runtime_config import RuntimeConfigManager
from baldur.services.event_bus.bus import BaldurEventBus, EventType, reset_event_bus
from baldur.services.event_bus.bus.convenience import configure_event_bus

SECTION = "circuit_breaker"

#: Every existing CONFIG_UPDATED subscriber in the tree exposes this one name,
#: and the bus de-duplicates on it.
_COLLIDING_HANDLER_NAME = "_on_config_updated"


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_config_invalidation_targets()
    yield
    reset_config_invalidation_targets()


@pytest.fixture
def sync_bus():
    """A bus that dispatches inline, so an emit is deterministic in-test."""
    bus = BaldurEventBus()
    bus._dispatch_mode = "sync"
    configure_event_bus(bus)
    yield bus
    reset_event_bus()


@pytest.fixture
def non_gunicorn_env(monkeypatch):
    monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)


@pytest.fixture
def gunicorn_master_env(monkeypatch):
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)


def _event(config_type=SECTION, **extra):
    return SimpleNamespace(data={"config_type": config_type, **extra})


def _raise_rebuild_failure():
    raise RuntimeError("rebuild failed")


def _patched_manager(manager):
    from baldur.factory.registry import ProviderRegistry

    return patch.object(
        ProviderRegistry.runtime_config_manager, "safe_get", return_value=manager
    )


# =============================================================================
# Dispatcher
# =============================================================================


class TestConfigDispatcherBehavior:
    """One handler for the whole process, routed by the event's config_type."""

    def test_an_unregistered_domain_returns_without_resolving_the_manager(self):
        """The registry is what makes a domain deliverable, so an event for a
        domain nobody registered must cost nothing at all."""
        from baldur.factory.registry import ProviderRegistry

        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get"
        ) as safe_get:
            bootstrap._dispatch_config_invalidation(_event())

        safe_get.assert_not_called()

    def test_a_payload_without_a_config_type_is_ignored(self):
        from baldur.factory.registry import ProviderRegistry

        register_config_invalidation_target(SECTION, lambda: None)
        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get"
        ) as safe_get:
            bootstrap._dispatch_config_invalidation(SimpleNamespace(data={}))

        safe_get.assert_not_called()

    def test_an_event_without_a_data_attribute_is_ignored(self):
        register_config_invalidation_target(SECTION, lambda: None)

        bootstrap._dispatch_config_invalidation(SimpleNamespace())

    def test_the_section_is_reloaded_before_the_targets_are_invoked(self):
        """Invalidating alone would rebuild from the same stale cache on every
        process that did not serve the write."""
        order = []
        manager = MagicMock(spec=RuntimeConfigManager)
        manager.reload_section.side_effect = lambda ct: order.append(f"reload:{ct}")
        register_config_invalidation_target(SECTION, lambda: order.append("invalidate"))

        with _patched_manager(manager):
            bootstrap._dispatch_config_invalidation(_event())

        assert order == [f"reload:{SECTION}", "invalidate"]

    def test_a_manager_without_a_reload_surface_still_invalidates(self):
        """The packages release independently, so an OSS wheel carrying this
        dispatcher can meet a manager that predates the reload method — it
        degrades to "rebuild from the cache we have" rather than raising."""
        calls = []
        register_config_invalidation_target(SECTION, lambda: calls.append("invalidate"))

        with _patched_manager(MagicMock(spec=[])):
            bootstrap._dispatch_config_invalidation(_event())

        assert calls == ["invalidate"]

    def test_no_registered_manager_still_invalidates(self):
        """The OSS-only install: the target rebuilds from environment settings."""
        calls = []
        register_config_invalidation_target(SECTION, lambda: calls.append("invalidate"))

        with _patched_manager(None):
            bootstrap._dispatch_config_invalidation(_event())

        assert calls == ["invalidate"]

    def test_a_raising_reload_logs_a_warning_and_still_invalidates(self, caplog):
        calls = []
        manager = MagicMock(spec=RuntimeConfigManager)
        manager.reload_section.side_effect = RuntimeError("backend down")
        register_config_invalidation_target(SECTION, lambda: calls.append("invalidate"))

        with _patched_manager(manager), caplog.at_level("WARNING"):
            bootstrap._dispatch_config_invalidation(_event())

        assert calls == ["invalidate"]
        assert any(
            "config_invalidation.reload_failed" in record.message
            for record in caplog.records
        )

    def test_a_raising_provider_resolution_does_not_propagate(self):
        """``safe_get`` swallows only "nothing registered"; a registered-but-
        failing provider raises straight through."""
        from baldur.factory.registry import ProviderRegistry

        calls = []
        register_config_invalidation_target(SECTION, lambda: calls.append("invalidate"))

        with patch.object(
            ProviderRegistry.runtime_config_manager,
            "safe_get",
            side_effect=RuntimeError("provider exploded"),
        ):
            bootstrap._dispatch_config_invalidation(_event())

        assert calls == ["invalidate"]

    def test_a_raising_target_does_not_propagate_to_the_dispatch_thread(self):
        register_config_invalidation_target(SECTION, _raise_rebuild_failure)

        with _patched_manager(None):
            bootstrap._dispatch_config_invalidation(_event())

    def test_the_dispatcher_name_collides_with_no_existing_subscriber(self):
        assert bootstrap._dispatch_config_invalidation.__name__ != (
            _COLLIDING_HANDLER_NAME
        )


# =============================================================================
# Starter
# =============================================================================


class TestConfigDeliveryStarterBehavior:
    """Registration is the declaration — it is what makes runtime-apply honest."""

    def test_it_is_a_background_worker_starter(self):
        """A scheduler entry would reach one process per host; this must run once
        per serving process."""
        assert (
            bootstrap._setup_config_invalidation_delivery
            in bootstrap._BACKGROUND_WORKER_STARTERS
        )

    def test_it_registers_the_circuit_breaker_invalidation_target(
        self, non_gunicorn_env, sync_bus
    ):
        from baldur.services.circuit_breaker.config import (
            invalidate_circuit_breaker_config,
        )

        bootstrap._setup_config_invalidation_delivery()

        assert invalidate_circuit_breaker_config in get_config_invalidation_targets(
            SECTION
        )

    def test_the_subscription_is_fire_and_forget(self, non_gunicorn_env, sync_bus):
        """Non-negotiable: the manager publishes while holding its own lock, and
        an awaited handler that re-enters it costs every write the handler
        timeout."""
        bootstrap._setup_config_invalidation_delivery()

        subscription = next(
            s
            for s in sync_bus._subscriptions[EventType.CONFIG_UPDATED]
            if s.handler is bootstrap._dispatch_config_invalidation
        )
        assert subscription.await_result is False

    def test_the_gunicorn_master_registers_and_subscribes_nothing(
        self, gunicorn_master_env, sync_bus
    ):
        bootstrap._setup_config_invalidation_delivery()

        assert get_config_invalidation_targets(SECTION) == []
        assert sync_bus._subscriptions.get(EventType.CONFIG_UPDATED, []) == []

    def test_running_twice_registers_once_and_subscribes_once(
        self, non_gunicorn_env, sync_bus
    ):
        """The ``post_worker_init`` hook re-runs every starter inside each forked
        worker; the bus's name dedup and the registry's membership dedup are what
        make that idempotent."""
        bootstrap._setup_config_invalidation_delivery()
        bootstrap._setup_config_invalidation_delivery()

        assert len(get_config_invalidation_targets(SECTION)) == 1
        assert len(sync_bus._subscriptions[EventType.CONFIG_UPDATED]) == 1

    def test_the_dispatcher_runs_even_beside_the_colliding_subscribers(
        self, non_gunicorn_env, sync_bus
    ):
        """The trap this handler dodges: three in-tree subscribers all expose one
        name, and a fourth carrying it would be dropped with a DEBUG line."""

        def _make_colliding_handler():
            def _on_config_updated(event):
                pass

            return _on_config_updated

        for _ in range(3):
            sync_bus.subscribe(
                EventType.CONFIG_UPDATED, _make_colliding_handler(), await_result=False
            )
        bootstrap._setup_config_invalidation_delivery()

        ran = []
        register_config_invalidation_target(SECTION, lambda: ran.append("invalidated"))
        with _patched_manager(None):
            sync_bus.emit(
                event_type=EventType.CONFIG_UPDATED,
                data={"config_type": SECTION},
                source="test",
            )

        assert ran == ["invalidated"]

    def test_a_failing_setup_does_not_propagate(self, non_gunicorn_env, caplog):
        with patch(
            "baldur.core.config_invalidation.register_config_invalidation_target",
            side_effect=RuntimeError("registry unavailable"),
        ):
            with caplog.at_level("WARNING"):
                bootstrap._setup_config_invalidation_delivery()

        assert any(
            "config_invalidation_setup_failed" in record.message
            for record in caplog.records
        )
