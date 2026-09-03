"""Unit tests for the ``init()`` circuit-breaker config seed step (744 D15/G23).

``_seed_circuit_breaker_config()`` builds the process-shared circuit-breaker
configuration once, at startup. Two properties depend on it and on its position
in the chain:

- No request or admission path ever builds the configuration, so none of them
  takes the runtime-config manager's lock — a lock an administrative write holds
  across a backend round trip. Moving the build off the request path needs BOTH
  the holder (a service no longer builds its own) and this seed; the holder alone
  only moves the build from construction to *first read*, and first read is a
  request thread.
- The rebuild is unconditional, not build-if-empty. A ``protect()`` call that ran
  at import time — before the runtime-config manager registered — has already
  pinned the holder to environment values; without an unconditional rebuild that
  process would never see a stored value again.

Verification techniques (§8):
  - State transition — a holder populated before ``init()`` is replaced
  - Dependency interaction — the step runs after ``_run_pro_extensions()``,
    asserted on the recorded step order rather than on wall-clock behavior
  - Exception/edge — a failing config source does not abort startup
  - Lifecycle — ``reset_init_state()`` drops the seed so the next ``init()``
    seeds from scratch
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

import pytest

from baldur import bootstrap
from baldur.services.circuit_breaker.config import (
    CircuitBreakerConfig,
    current_circuit_breaker_config,
    reset_circuit_breaker_config,
)

# Every ``init()`` step except the one under test. Patched out so the chain is
# a cheap, deterministic ordering harness rather than a real startup.
_NEIGHBOUR_STEPS = (
    "_validate_startup_config",
    "_validate_critical_secrets",
    "_register_default_event_handlers",
    "_init_bridge_instrumentation",
    "_instrument_otel_if_enabled",
    "_register_shutdown_handlers",
    "_wire_registry_defaults",
    "_validate_idempotency_cache_in_production",
    "_install_idempotency_gate",
    "_emit_tier_setting_warnings",
    "_run_pro_extensions",
    "_warn_unknown_env_vars",
    "_apply_audit_default_provider",
    "_start_audit_pipeline_if_enabled",
    "_start_dlq_outbox_if_enabled",
    "_configure_error_budget_if_enabled",
    "_register_metrics_provider_if_configured",
    "_record_env_snapshot",
    "_start_default_scheduler",
    "_register_sql_statistics_if_available",
    "_start_admin_server_if_enabled",
    "start_background_workers",
)


class _StubRuntimeConfigManager:
    """Stand-in for the PRO runtime-config manager holding a stored value."""

    def __init__(self, stored: dict[str, Any]) -> None:
        self._stored = stored

    def get_circuit_breaker_config(self) -> dict[str, Any]:
        return dict(self._stored)


def _run_init(recorder: list[str] | None = None, manager: Any = None) -> None:
    """Run ``init()`` with every step but the seed replaced by a recorder."""
    from baldur.factory.registry import ProviderRegistry

    def _track(name):
        def _impl(*_args, **_kwargs):
            if recorder is not None:
                recorder.append(name)
            return None

        return _impl

    with ExitStack() as stack:
        for step in _NEIGHBOUR_STEPS:
            stack.enter_context(patch.object(bootstrap, step, _track(step)))
        stack.enter_context(
            patch.object(bootstrap, "_build_startup_report", lambda *_a, **_kw: {})
        )
        stack.enter_context(
            patch.object(
                ProviderRegistry.runtime_config_manager,
                "safe_get",
                return_value=manager,
            )
        )
        real_seed = bootstrap._seed_circuit_breaker_config

        def _seed(*args, **kwargs):
            if recorder is not None:
                recorder.append("_seed_circuit_breaker_config")
            return real_seed(*args, **kwargs)

        stack.enter_context(
            patch.object(bootstrap, "_seed_circuit_breaker_config", _seed)
        )
        bootstrap.init()


@pytest.fixture(autouse=True)
def _isolated_init_state():
    """Every test starts from an uninitialised process with an empty holder."""
    bootstrap.reset_init_state()
    reset_circuit_breaker_config()
    yield
    bootstrap.reset_init_state()
    reset_circuit_breaker_config()


class TestInitCircuitBreakerSeedBehavior:
    """The seed's timing, its unconditional rebuild, and its failure mode."""

    def test_init_seeds_the_holder(self):
        """After ``init()`` the configuration exists without anyone reading it,
        so the first request finds a pointer rather than a build."""
        _run_init(manager=_StubRuntimeConfigManager({"failure_threshold": 9}))

        with patch.object(
            CircuitBreakerConfig,
            "from_settings",
            side_effect=AssertionError("first read must not build"),
        ):
            assert current_circuit_breaker_config().failure_threshold == 9

    def test_seed_runs_after_the_pro_extensions_register(self):
        """Position is load-bearing: seeding before the extensions register
        would build the configuration from the environment and never see the
        runtime-config manager the extensions install."""
        order: list[str] = []

        _run_init(recorder=order)

        assert order.index("_run_pro_extensions") < order.index(
            "_seed_circuit_breaker_config"
        ), f"expected _run_pro_extensions before the seed, got: {order}"

    def test_seed_runs_before_the_background_workers_start(self):
        """A worker started with no configuration in the holder would build one
        on its own thread — the build this step exists to centralise."""
        order: list[str] = []

        _run_init(recorder=order)

        assert order.index("_seed_circuit_breaker_config") < order.index(
            "start_background_workers"
        ), f"expected the seed before start_background_workers, got: {order}"

    def test_seed_replaces_a_holder_populated_before_init(self):
        """The import-time ``protect()`` case: the holder already answers with
        environment values, and an unconditional rebuild is the only thing that
        lets that process ever see a stored value."""
        # Given — a holder built before init(), with no manager registered
        from baldur.factory.registry import ProviderRegistry

        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
        ):
            early = current_circuit_breaker_config()
        assert early.failure_threshold != 9

        # When — init() runs with the manager registered
        _run_init(manager=_StubRuntimeConfigManager({"failure_threshold": 9}))

        # Then — the pre-init value did not survive
        assert current_circuit_breaker_config() is not early
        assert current_circuit_breaker_config().failure_threshold == 9

    def test_failing_config_source_does_not_abort_init(self):
        """Best-effort: a config source that is down at startup must not stop
        the process from coming up."""
        with patch.object(
            CircuitBreakerConfig,
            "from_settings",
            side_effect=RuntimeError("config source down"),
        ):
            _run_init()

        assert bootstrap._init_done is True

    def test_failing_seed_step_does_not_abort_init(self):
        """The guard is on the step itself, not only on the invalidation it
        delegates to."""
        with patch(
            "baldur.services.circuit_breaker.config.invalidate_circuit_breaker_config",
            side_effect=RuntimeError("seed exploded"),
        ):
            _run_init()

        assert bootstrap._init_done is True

    def test_failing_seed_leaves_the_previous_configuration_in_force(self):
        from baldur.factory.registry import ProviderRegistry

        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
        ):
            before = current_circuit_breaker_config()

        with patch.object(
            CircuitBreakerConfig,
            "from_settings",
            side_effect=RuntimeError("config source down"),
        ):
            _run_init()

        assert current_circuit_breaker_config() is before

    def test_reset_init_state_drops_the_seeded_configuration(self):
        """Without the drop, the seed's unconditional-rebuild property is not
        observable: a re-run would inherit the previous run's configuration."""
        _run_init(manager=_StubRuntimeConfigManager({"failure_threshold": 9}))
        seeded = current_circuit_breaker_config()

        bootstrap.reset_init_state()

        from baldur.factory.registry import ProviderRegistry

        with patch.object(
            ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
        ):
            assert current_circuit_breaker_config() is not seeded

    def test_re_running_init_reseeds_from_the_current_source(self):
        """Lifecycle: init → reset → init picks up whatever the source says the
        second time, with nothing carried over."""
        _run_init(manager=_StubRuntimeConfigManager({"failure_threshold": 9}))
        assert current_circuit_breaker_config().failure_threshold == 9

        bootstrap.reset_init_state()
        reset_circuit_breaker_config()
        _run_init(manager=_StubRuntimeConfigManager({"failure_threshold": 4}))

        assert current_circuit_breaker_config().failure_threshold == 4
