"""
Pytest configuration and fixtures for baldur tests.

Test isolation architecture (403) — Two-Tier Reset (Google Bazel model):

Heavy tier (module scope — _module_reset_singletons):
  Autodiscovery scans all baldur.* modules for reset_*() functions and
  singleton reset_instance() methods. Calls them in safe phase order:
  thread-owners → main batch → governance cache → infrastructure.
  Then runs _post_reset_recovery() to restore import-time registrations
  (ProviderRegistry, ML models, Prometheus flags, WAL, Redis cache).
  Self-maintaining: new singletons are found automatically.

Light tier (function scope — auto_reset_all_state):
  Resets only fast-changing state: root config (settings cache), ContextVars,
  ProviderRegistry instances, governance cache. ~0.1ms per call.

Supporting fixtures:
- reset_audit_modules(auto_reset_all_state): Pops audit service modules from
  sys.modules for mock isolation. Depends on auto_reset_all_state for ordering.
- --dist loadfile (pytest.ini): Groups same-file tests on same xdist worker.
"""

import atexit
import copy
import inspect
import os
import sys
import warnings
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
import structlog

# Django settings for standalone library testing
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.testapp.settings")

# Note: dummy BALDUR_SECRETS_* env vars are injected by
# tests/testapp/settings.py module-level code, which runs before
# AppConfig.ready() and is therefore the only place that reliably
# beats pytest-django's Django setup (which import-loads baldur
# before this conftest's module-level code runs).

# Suppress log noise: must be set before baldur import
os.environ.setdefault("BALDUR_TEST_LOG_LEVEL", "WARNING")

# 429/PR1: the default scheduler (LeaderScheduler) should not auto-start in
# the unit test process — it spawns a daemon thread + Leader Election which
# has no Redis in CI. Individual tests that exercise the scheduler opt in
# explicitly by setting BALDUR_SCHEDULER_AUTOSTART=1 for their scope.
os.environ.setdefault("BALDUR_SCHEDULER_AUTOSTART", "0")

# 429/PR3: the admin HTTP server likewise must not bind a real port during
# the unit test process. Tests that exercise the server start an
# AdminServer directly with port=0 on an ephemeral loopback address.
os.environ.setdefault("BALDUR_ADMIN_AUTOSTART", "0")

# 558 D4: MetaWatchdogSettings.enabled now defaults True (detect+escalate
# slice promoted to PRO v1.0), so any init() in a test would otherwise spawn
# the watchdog daemon thread. Tests that exercise the watchdog start one
# directly. Mirrors the two autostart escape hatches above.
os.environ.setdefault("BALDUR_META_WATCHDOG_AUTOSTART", "0")

# 593 D6: with OTEL_DJANGO_INSTRUMENT_ENABLED defaulting True, any init() (or
# Django ready()) in a test that sets BALDUR_OBSERVABILITY_PROFILE=otel_collector
# would otherwise really monkey-patch the global requests/logging modules and
# settings.MIDDLEWARE, leaking across tests. Tests that exercise OTel
# instrumentation set BALDUR_OTEL_AUTOSTART=1 for their scope. Mirrors the three
# hatches above.
os.environ.setdefault("BALDUR_OTEL_AUTOSTART", "0")

# 524: the observability profile defaults to ``auto``, which resolves to
# ``otel_collector`` whenever the OTel SDK + Prometheus bridge are importable —
# both are present in the monorepo dev env. Under that resolution ``get_metrics()``
# builds ``OTELBaldurMetrics`` (real MeterProvider) instead of the prometheus
# ``BaldurMetrics`` facade, flipping the long-standing test-suite default and
# spinning up real OTel meter state in unit tests. Pin the suite to ``local`` so
# the default backend stays prometheus (the pre-524 default); tests that exercise
# OTel resolution set the profile explicitly within their own scope.
os.environ.setdefault("BALDUR_OBSERVABILITY_PROFILE", "local")

# 604 D3: PrecomputedCacheSettings.enabled defaults True and init() now starts
# the proactive-refresh worker framework-agnostically, so any init() in a test
# would otherwise spawn the refresh timer thread. Tests that exercise the worker
# start one directly. Mirrors the four autostart escape hatches above.
os.environ.setdefault("BALDUR_PRECOMPUTED_CACHE_AUTOSTART", "0")

# 608 D6: SystemMetricsCacheSettings.enabled defaults True and init() now starts
# the psutil CPU/Memory cache framework-agnostically (relocated out of the
# Django adapter), so any init() in a test would otherwise spawn the psutil
# refresh timer thread. Tests that exercise the cache start one directly.
# Mirrors the five autostart escape hatches above.
os.environ.setdefault("BALDUR_SYSTEM_METRICS_CACHE_AUTOSTART", "0")

# 650 D1: MetricsSettings.enabled defaults True and init() now schedules a
# per-process CB-state startup seed (a jittered daemon Timer that seeds
# baldur_circuit_breaker_state from the repo), so any init() in a test would
# otherwise leak a Timer thread. Tests that exercise the seed call the inner
# _seed_circuit_breaker_state() directly. Mirrors the autostart hatches above.
os.environ.setdefault("BALDUR_CB_STATE_SEED_AUTOSTART", "0")

# 709: BulkheadSettings.metrics_enabled defaults True and init() starts the
# bulkhead metrics updater framework-agnostically (relocated with the core-tier
# bulkhead primitives), so any init() in a test would otherwise spawn the poll
# daemon thread. Tests that exercise the updater start one directly. Mirrors
# the autostart hatches above.
os.environ.setdefault("BALDUR_BULKHEAD_METRICS_AUTOSTART", "0")


# =============================================================================
# Canonical Test Structlog Config (578 D2)
# =============================================================================


def _apply_canonical_test_structlog_config() -> None:
    """Re-apply the canonical test structlog configuration (578 D2).

    Routes structlog through stdlib with caching DISABLED
    (``cache_logger_on_first_use=False``) and marks the runtime
    ``_StructlogState`` as configured so production ``configure_structlog()``
    (which would re-arm ``cache_logger_on_first_use=True``) short-circuits.

    Before ``configure_structlog()``, structlog uses PrintLogger (stdout direct)
    which ignores stdlib log levels; LoggerFactory makes all structlog output
    respect the root logger level (WARNING).

    Caching is the structlog flake source: once a module-level
    ``logger = structlog.get_logger()`` proxy freezes a stdlib-routed bound
    logger on first emit, ``structlog.testing.capture_logs()`` can no longer
    intercept it and returns an empty list. Holding this ``cache=False``
    invariant at every test boundary keeps capture deterministic suite-wide
    under ``-n6``, subsuming the former per-file flake guards. See
    ``project_xdist_capture_logs_flake``.
    """

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    # Prevent configure_structlog() from reconfiguring (writes to runtime state).
    # 450 Phase 4: ``_configured`` lives on a runtime-scoped ``_StructlogState`` —
    # setting the module attr directly would create a stray module global the
    # production code never reads.
    structlog_mod = sys.modules.get("baldur.observability.structlog_config")
    if structlog_mod is not None:
        structlog_mod._structlog_state().configured = True


# The lazy-proxy type returned by ``structlog.get_logger()``, captured once via
# public API (584 D5) — never ``from structlog._config import
# BoundLoggerLazyProxy``. ``get_logger()`` always returns the lazy proxy, so its
# ``type()`` *is* the proxy class without importing the non-public path, and it
# tracks a future structlog class rename/relocation automatically.
_STRUCTLOG_PROXY_TYPE = type(structlog.get_logger())


def _unfreeze_module_loggers(modules) -> None:
    """Un-freeze every already-frozen module-global structlog proxy (584 D5).

    578's suite-wide guard holds ``cache_logger_on_first_use=False`` to PREVENT
    new proxy freezes, but it cannot *un*-freeze a proxy frozen earlier in the
    worker's life: structlog 25.5.0 stores the freeze at the instance attribute
    ``proxy.__dict__["bind"]`` (a ``finalized_bind`` closure over the
    ``BoundLogger`` whose processor chain *is* the ``_CONFIG.default_processors``
    list current at first-emit, set only when caching is armed). Once a later
    ``configure(processors=…)`` swaps in a fresh list — every test setup does —
    the frozen logger holds a stale list ``capture_logs()`` no longer mutates, so
    it silently returns ``[]`` while the event still routes to stdlib. Popping
    the ``bind`` override restores class-method resolution of ``bind``, which
    re-reads the live ``_CONFIG.default_processors`` on every emit.

    Identifies proxies by *type*, not the variable name ``logger`` — two ``src/``
    proxies are not named ``logger`` (``settings.root._root_logger`` and
    ``api.admin.routes._import_policy._logger``, the latter a live ``capture_logs``
    target), so a name-keyed lookup has a known-reachable miss.

    Reads ``vars(mod)`` directly (never ``getattr``) so a PEP 562 module
    ``__getattr__`` lazy-import is not triggered, with a narrow guard skipping any
    ``sys.modules`` entry that lacks a ``__dict__`` (a ``None`` placeholder or
    exotic non-module object) so one malformed entry cannot error every test's
    setup. The ``pop``/capture mechanism itself stays un-wrapped so a structlog
    drift surfaces through the G34 canary rather than being silently swallowed.

    ``modules`` is parameter-injected (caller passes a ``baldur*``-filtered
    snapshot of ``sys.modules``) so the G34 pin can drive it with a synthetic
    module list without patching ``sys.modules``.
    """
    for module in modules:
        try:
            module_vars = vars(module)
        except TypeError:
            continue  # None placeholder / exotic non-module object — no __dict__
        for value in list(module_vars.values()):
            if type(value) is _STRUCTLOG_PROXY_TYPE and "bind" in vars(value):
                vars(value).pop("bind", None)


# =============================================================================
# Pytest Configuration
# =============================================================================


def pytest_configure(config):
    """
    pytest startup: configure test environment.

    Prevents real DB/Redis connections, suppresses log noise,
    and pre-seeds negative caches for infrastructure-absent environments.
    """

    # Route structlog through stdlib immediately with caching disabled, and
    # block production configure_structlog() from re-arming it (578 D2).
    _apply_canonical_test_structlog_config()

    # Suppress infra-absent noise at collection time
    import logging as _logging

    for _name in (
        "baldur.api.django.pool_circuit_breaker",
        "baldur.adapters.audit.redis_buffer",
    ):
        _logging.getLogger(_name).setLevel(_logging.ERROR)

    # Test environment flags
    os.environ.setdefault("BALDUR_TEST_MODE", "true")

    # Use in-memory state backend: prevents cross-worker file pollution
    # when running with pytest-xdist (-n N). Default "file" backend writes
    # to logs/baldur_state/ which is shared across worker processes.
    os.environ.setdefault("BALDUR_SYSTEM_CONTROL_BACKEND", "memory")

    # Reduce I/O timeouts for test environment — prevents real network calls
    # from blocking test execution for seconds
    # Prometheus: 3s → 0.5s per call (3 calls in CellHealthAggregator)
    os.environ.setdefault("BALDUR_CELL_TOPOLOGY_PROMETHEUS_TIMEOUT_SECONDS", "0.5")
    # Notification: 10s → 1s (fail-open alert via TimeoutExecutor)
    os.environ.setdefault("BALDUR_NOTIFICATION_NOTIFICATION_TIMEOUT_SECONDS", "1")
    # Thread join: 5s → 2s / 10s → 5s (singleton reset teardowns)
    # 2s floor: consumer threads use queue.get(timeout=1.0), need margin
    os.environ.setdefault("BALDUR_THREAD_MANAGEMENT_JOIN_TIMEOUT", "2")
    os.environ.setdefault("BALDUR_THREAD_MANAGEMENT_JOIN_TIMEOUT_LONG", "5")

    # Prevent RunbookService EventBus subscriptions (Celery deadlock)
    os.environ.setdefault("BALDUR_RUNBOOK_SUBSCRIBE_EVENTS", "false")

    # EventBus handler timeout: 5s → 0.1s for tests. async_pool dispatch
    # waits on future.result(timeout=) per handler — default 5s × multiple
    # subscribers (CB OPEN has notify + snapshot default handlers that call
    # Celery .delay() which may block on broker connect) compounds into
    # 9-10s per slow CB / notification test. Tests that need a specific
    # timeout override via monkeypatch.setenv.
    os.environ.setdefault("BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS", "0.1")

    # Disable WAL: prevent actual WAL file writes in tests.
    # 416 D1: WAL gating is now driven by AuditSettings.enabled (default False).
    # We additionally clear any cached WAL instance from a previous test run.
    try:
        import baldur_pro.services.audit.base as _audit_base

        _audit_base._reset_wal_state()
    except ImportError:
        pass

    # Prevent async_audit_lifecycle atexit handler — writes runtime state.
    try:
        import baldur.audit.async_audit_lifecycle as lifecycle_module

        lifecycle_module._lifecycle_state().shutdown_registered = True
    except ImportError:
        pass

    # Pre-seed Redis negative cache to avoid ~8s TCP timeout (runtime state).
    import time as _time

    try:
        import baldur.adapters.redis as _redis_mod

        _state = _redis_mod._redis_state()
        _state.unavailable = True
        _state.fail_time = _time.monotonic()
    except ImportError:
        pass

    # ResilientStorageBackend._init_redis now natively respects Redis
    # negative cache (TTL-based), so no monkey-patch is needed here.


def pytest_unconfigure(config):
    """Cleanup on pytest exit: remove atexit handlers and temp files."""
    try:
        from baldur.audit.async_audit_lifecycle import (
            graceful_shutdown_audit_system,
        )

        atexit.unregister(graceful_shutdown_audit_system)
    except (ImportError, AttributeError):
        pass

    # Clean up temp files created by audit buffer / fallback paths
    import shutil
    from pathlib import Path

    for path in [
        Path(os.environ.get("TEMP", "/tmp")) / "baldur",
        Path("/tmp/emergency_audit.jsonl"),
        Path("/tmp/emergency_state.json"),
    ]:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)
        except Exception:
            pass

    # Silence interpreter-shutdown noise from baldur daemon threads
    # (HealthProbeManager, SelfHealerWatchdog) that may still be running
    # after pytest's stderr capture ends. Their structlog writes can
    # collide with stderr's BufferedWriter at interpreter teardown,
    # producing 'Fatal Python error: _enter_buffered_busy' messages and
    # 'cannot schedule new futures after interpreter shutdown' warnings.
    #
    # We register an atexit handler (runs after pytest exits, before
    # interpreter teardown finishes daemon threads) that redirects the
    # raw stderr fd to /dev/null. By that point pytest's own output is
    # already flushed, so only post-teardown daemon noise is dropped.
    #
    # This is a TEST-ONLY mitigation — production code paths still
    # write to stderr normally. Source code is not modified.
    def _silence_post_shutdown_stderr():
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull_fd, sys.stderr.fileno())
            os.close(devnull_fd)
        except Exception:
            pass

    atexit.register(_silence_post_shutdown_stderr)


# =============================================================================
# D1. Incremental Cached Autodiscovery Reset Registry
# =============================================================================

# Phase 1: Thread-owning singletons — must join threads before other resets
# to prevent background tasks from polluting state during reset.
_RESET_THREAD_OWNERS_FIRST = frozenset(
    {
        "baldur_pro.services.emergency_mode",
        "baldur.audit.audit_watchdog",
        "baldur.audit.sync_worker",
        "baldur.adapters.memory.layered_repository",
        "baldur.api.django.pool_circuit_breaker",
        "baldur.services.rate_limit_coordinator.coordinator",
        "baldur_dormant.services.correlation_engine.service",
    }
)

# Phase 4: Infrastructure — reset last because other reset functions may
# read settings or use ProviderRegistry during their own reset.
_RESET_INFRA_LAST = frozenset(
    {
        "baldur.factory",
        "baldur.factory.registry",
        "baldur.settings.root",
    }
)

_RESET_SKIP = frozenset(
    {
        # Functions that are destructive or have I/O side effects
        "reset_redis_client",  # would close real connections in integration tests
        "reset_kafka_producer",  # same
    }
)

# ---------------------------------------------------------------------------
# Mock-leak watchlist: functions commonly patched by tests that may leak.
# Original references are saved at first scan and restored if a mock is found.
# ---------------------------------------------------------------------------
_MOCK_LEAK_WATCHLIST: list[tuple[str, str]] = [
    ("baldur.metrics.prometheus", "get_metrics"),
    ("baldur.settings.metrics", "get_metrics_settings"),
    ("baldur.services.system_control", "is_baldur_enabled"),
]
_original_functions: dict[tuple[str, str], Any] = {}

# ---------------------------------------------------------------------------
# Incremental cache — populated once per worker, updated incrementally
# ---------------------------------------------------------------------------
_cached_thread_owners: list[Callable] = []
_cached_main_batch: list[Callable] = []
_cached_infra_last: list[Callable] = []
_scanned_modules: set[str] = set()
_seen_fn_ids: set[int] = set()


def _has_required_params(fn) -> bool:
    """Check if a function has required (non-default) parameters.

    Per-key reset functions (e.g. reset_state(service_name)) cannot be called
    with no arguments and would not reset all state even if they could.
    """
    try:
        sig = inspect.signature(fn)
        return any(
            p.default is inspect.Parameter.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
            for p in sig.parameters.values()
        )
    except (ValueError, TypeError):
        return False


def _scan_module(mod_name: str, mod) -> None:
    """Scan a single module for reset functions and add to cached phase lists.

    Uses vars(mod) (module __dict__) instead of dir(mod) + getattr() to avoid
    triggering PEP 562 __getattr__ lazy imports. Modules like
    baldur.api.django.views define __getattr__ for lazy loading of Django
    view classes; dir() includes those names and getattr() triggers the import
    chain (→ django.contrib.auth.models → AppRegistryNotReady).
    """
    mod_dict = vars(mod)

    # --- Module-level reset_*() functions ---
    for attr_name, fn in mod_dict.items():
        if not attr_name.startswith("reset_"):
            continue
        if attr_name in _RESET_SKIP:
            continue
        if fn is None or not callable(fn) or inspect.isclass(fn):
            continue
        if inspect.ismethod(fn):  # bound method on class, skip
            continue
        if _has_required_params(fn):  # per-key resets — cannot call with no args
            continue
        if id(fn) in _seen_fn_ids:
            continue
        _seen_fn_ids.add(id(fn))

        _add_to_phase(fn, mod_name)

    # --- Singleton classmethod resets ---
    for _attr_name, cls in mod_dict.items():
        if cls is None or not inspect.isclass(cls):
            continue
        if not hasattr(cls, "_instance"):
            continue
        # Try reset methods in priority order
        for method_name in ("reset_instance", "_reset", "reset"):
            method = getattr(cls, method_name, None)
            if method is not None and callable(method):
                if id(method) not in _seen_fn_ids:
                    _seen_fn_ids.add(id(method))
                    _add_to_phase(method, mod_name)
                break  # one reset per class


def _update_cache() -> None:
    """Incrementally scan only newly loaded modules since last call."""
    # list() snapshot — background threads (lazy imports, pool refresh)
    # may mutate sys.modules mid-iteration, which raises RuntimeError on
    # a live dict view.
    current_modules = {
        k
        for k in list(sys.modules)
        if (
            k.startswith("baldur.")
            or k.startswith("baldur_pro.")
            or k.startswith("baldur_dormant.")
        )
        and sys.modules.get(k) is not None
    }
    new_modules = current_modules - _scanned_modules
    if not new_modules:
        return

    for mod_name in new_modules:
        mod = sys.modules.get(mod_name)
        if mod is not None:
            _scan_module(mod_name, mod)

    _scanned_modules.update(new_modules)

    # Save originals for mock-leak watchlist (first access only)
    for mod_name, func_name in _MOCK_LEAK_WATCHLIST:
        key = (mod_name, func_name)
        if key in _original_functions:
            continue
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        fn = getattr(mod, func_name, None)
        if fn is not None and not hasattr(fn, "_mock_name"):
            _original_functions[key] = fn


def _discover_and_call_resets():
    """Execute all cached reset functions in safe phase order.

    On first call, performs full scan. Subsequent calls only scan
    newly loaded modules (incremental cache update via set difference).
    """
    _update_cache()

    # Execute in safe order
    for fn in _cached_thread_owners:
        _safe_call(fn)
    for fn in _cached_main_batch:
        _safe_call(fn)

    # Governance cache invalidation (explicit — not caught by reset_* scan)
    gov_mod = sys.modules.get("baldur_pro.services.governance.checks")
    if gov_mod is not None:
        try:
            gov_mod.invalidate_governance_cache()
        except Exception:
            pass

    for fn in _cached_infra_last:
        _safe_call(fn)


def _add_to_phase(fn, mod_name):
    """Route a reset function to the correct execution phase (cached lists)."""
    if mod_name in _RESET_THREAD_OWNERS_FIRST:
        _cached_thread_owners.append(fn)
    elif mod_name in _RESET_INFRA_LAST:
        _cached_infra_last.append(fn)
    else:
        _cached_main_batch.append(fn)


def _safe_call(fn):
    """Call a reset function, swallowing any exception."""
    try:
        fn()
    except Exception:
        pass  # reset functions must not break test teardown


# =============================================================================
# D2. Prometheus Registry Reset — Lazy-Init Flag Reset
# =============================================================================

# Modules that use lazy initialization with _metrics_initialized flag.
# After get_or_create_* migration, resetting the flag causes re-initialization
# which safely returns existing metrics instead of raising ValueError.
_LAZY_INIT_FLAG_MODULES = {
    "baldur.resilience.policies.hooks.metrics": "_metrics_initialized",
    "baldur.adapters.kafka.metrics": "_metrics_initialized",
    "baldur.observability.log_processors": "_violation_counter_initialized",
}

# Modules that use None-check or hasattr-check lazy init.
# Reset the metric variables to None so they are re-created via get_or_create_*.
_LAZY_INIT_VAR_MODULES = {
    "baldur.audit.cascade_chain": [
        "_CASCADE_CHAIN_DEPTH_EXCEEDED",
        "_CASCADE_CYCLE_DETECTED",
    ],
    "baldur.audit.checkpoint.strategy": [
        "_CHECKPOINT_SAVE_FAILURES",
        "_CHECKPOINT_LOAD_FAILURES",
    ],
    "baldur.adapters.kafka.metrics": [
        "_TIME_LAG_GAUGE",
        "_PROCESSING_LATENCY_HISTOGRAM",
        "_OFFSET_LAG_GAUGE",
    ],
}


def _reset_prometheus_registry():
    """Reset lazy-init flags so metrics are re-initialized via get_or_create_*.

    After migration, get_or_create_* handles deduplication automatically.
    No REGISTRY.unregister() needed — no private API dependency.
    """
    for mod_name, flag_attr in _LAZY_INIT_FLAG_MODULES.items():
        mod = sys.modules.get(mod_name)
        if mod is not None:
            setattr(mod, flag_attr, False)

    for mod_name, attrs in _LAZY_INIT_VAR_MODULES.items():
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for attr in attrs:
            setattr(mod, attr, None)

    # hasattr-based modules (lua_registry, env_snapshot):
    # delete function attributes so hasattr() returns False on next call.
    for mod_name, func_name, attr_name in [
        ("baldur.audit.performance.lua_registry", "_get_lua_metrics", "_load"),
        ("baldur.audit.env_snapshot", "_get_metrics", "_env_snapshot_recorded"),
    ]:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        fn = getattr(mod, func_name, None)
        if fn is not None and hasattr(fn, attr_name):
            delattr(fn, attr_name)


# =============================================================================
# D3. ContextVar Reset — Explicit Defaults Mapping
# =============================================================================

# Explicit mapping: (module_path, var_name, default_value)
# Mutable defaults (list, dict) are deepcopied on each reset to prevent
# shared-reference contamination between tests.
_CONTEXT_VARS_DEFAULTS: list[tuple[str, str, Any]] = [
    ("baldur.audit.trace", "_trace_id_var", None),
    ("baldur.audit.trace", "_celery_context_var", None),
    ("baldur.context.cell_context", "_current_cell_id", None),
    ("baldur.context.actor_context", "_current_actor", None),
    ("baldur.context.causation_context", "_current_causation", None),
    ("baldur.decorators.domain_tag", "_current_domain", None),
    ("baldur.core.test_mode_context", "_is_synthetic_request", False),
    ("baldur.core.test_mode_context", "_synthetic_session_id", None),
    ("baldur.scaling.deadline_context", "_request_deadline", None),
    ("baldur.settings.layered_provider", "_request_overrides", {}),  # MUTABLE
    ("baldur_pro.services.throttle.audit", "_audit_event_chain", []),  # MUTABLE
]

_MUTABLE_TYPES = (list, dict, set)


def _reset_context_vars():
    """Reset all known ContextVars to their explicit default values.

    Mutable defaults (list, dict, set) are deepcopied to ensure each test
    gets a fresh instance, preventing shared-reference contamination.
    """
    for mod_name, var_name, default in _CONTEXT_VARS_DEFAULTS:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        cv = getattr(mod, var_name, None)
        if cv is None:
            continue
        value = (
            copy.deepcopy(default) if isinstance(default, _MUTABLE_TYPES) else default
        )
        cv.set(value)


# =============================================================================
# Two-Tier Reset Architecture (Google Bazel-inspired)
#
# Heavy (module scope): Full singleton + Prometheus reset between test FILES.
#   Autodiscovery finds all reset_*() and singleton reset_instance() methods.
#   Runs ~1,116 times (once per test file) instead of ~21,771 (per test).
#
# Light (function scope): Settings + ContextVars between test FUNCTIONS.
#   Only resets known high-frequency pollution sources: ~0.1ms per call.
#
# Combined with --dist loadfile (pytest.ini), this approximates Bazel's
# process-per-test-target isolation within pytest-xdist's shared-process model.
# =============================================================================


def _post_reset_provider_registry():
    """Restore ProviderRegistry to test-safe state after autodiscovery reset.

    This is test-specific logic (hardcodes "memory"/"sync" defaults) and must
    live in conftest, not in production code (Separation of Concerns).

    Adapter registration only triggers if cache._providers was wiped by a
    reset() call.

    ML strategy slots are NOT restored here: since 599 D9 the module-load
    baseline leaves anomaly_detection/forecast/classification/optimization
    empty (statistical defaults are registered by the baldur_dormant
    bootstrap hook, not at import time). Tests that need strategies must
    register them explicitly (real ones via
    ml_models.registry.register_statistical_defaults() in dormant-tier
    tests, or Protocol mocks in OSS tests).
    """
    pr_mod = sys.modules.get("baldur.factory")
    if pr_mod is None:
        return
    try:
        pr_mod.ProviderRegistry.clear_instances()
    except (AttributeError, TypeError):
        return

    reg_mod = sys.modules.get("baldur.factory.registry")
    if reg_mod is None:
        return
    try:
        if not pr_mod.ProviderRegistry.cache.has_any_providers():
            reg_mod._auto_register_adapters()
            pr_mod.ProviderRegistry.cache.set_default("memory")
            pr_mod.ProviderRegistry.queue.set_default("sync")
    except (AttributeError, TypeError):
        pass


def _reset_root_config():
    """Reset Root Settings singleton — invalidates all settings caches."""
    root_mod = sys.modules.get("baldur.settings.root")
    if root_mod is not None:
        try:
            root_mod.reset_config()
        except (AttributeError, TypeError):
            pass


def _repin_observability_profile():
    """Re-pin ``BALDUR_OBSERVABILITY_PROFILE=local`` before each test function (524).

    The observability profile defaults to ``auto``, which resolves to
    ``otel_collector`` whenever the OTel SDK + Prometheus bridge are importable
    (true in the monorepo dev env). Under that resolution ``get_metrics()``
    builds the OTel metrics backend instead of the prometheus ``BaldurMetrics``
    facade. The suite pins the profile to ``local`` at session start
    (``tests/testapp/settings.py`` + this conftest's module-level setdefault),
    but many tests permanently strip env keys (``os.environ.pop`` sweeps,
    ``patch.dict(..., clear=True)`` snapshots taken after another test already
    dropped the key, etc.), so the pin is lost mid-suite and later facade /
    metric-recording tests on the same worker silently resolve ``otel``.
    Re-establishing the pin per function (``setdefault`` respects an explicit
    operator/CI override and any in-test ``patch.dict`` override applied after
    this runs) keeps the metrics backend deterministically prometheus.
    """
    os.environ.setdefault("BALDUR_OBSERVABILITY_PROFILE", "local")


def _clear_provider_instances():
    """Clear ProviderRegistry instance caches (keeps registrations).

    ML strategy slots are intentionally NOT restored (599 D9): the
    module-load baseline is empty — see _post_reset_provider_registry.
    """
    pr_mod = sys.modules.get("baldur.factory")
    if pr_mod is not None:
        try:
            pr_mod.ProviderRegistry.clear_instances()
        except (AttributeError, TypeError):
            pass


def _reset_startup_integrations_slot():
    """Clear the startup_integrations slot registrations between test functions.

    615 D1/Testability Notes: ``_clear_provider_instances()`` keeps
    registrations, so a leaked startup-integration registration would survive
    the per-function reset and turn a later same-process
    ``start_background_workers()`` into a PRO thread spawn. Clearing
    registrations is safe for this slot specifically — its module-load
    baseline is empty and the test process never populates it through
    entitlement (``register_pro_services()`` is skipped). Tests still own
    teardown of anything they *started* (threads outlive a registry reset).
    """
    pr_mod = sys.modules.get("baldur.factory")
    if pr_mod is not None:
        try:
            pr_mod.ProviderRegistry.startup_integrations.reset()
        except (AttributeError, TypeError):
            pass


def _clear_inmemory_lock_registry():
    """Clear the class-level InMemoryLock registry between test functions.

    Held cooldown locks (and any other in-memory distributed lock) live in
    the class-level ``InMemoryLock._locks`` registry, which survives
    ``_clear_provider_instances`` (that drops adapter instances, not the
    class registry). UNM cooldown holds its lock for the full window by
    design, so without this clear a successful ``notify()`` would leak a
    window-length lock into later tests on the same xdist worker.
    """
    mod = sys.modules.get("baldur.adapters.cache.memory_adapter")
    if mod is not None:
        try:
            mod.InMemoryLock.clear_all_locks()
        except (AttributeError, TypeError):
            pass


def _invalidate_governance_cache():
    """Invalidate governance check cache (TTL-based, stale within-file)."""
    gov_mod = sys.modules.get("baldur_pro.services.governance.checks")
    if gov_mod is not None:
        try:
            gov_mod.invalidate_governance_cache()
        except Exception:
            pass


def _reset_system_control_light():
    """Reset SystemControlManager cached state to default (enabled=True).

    SystemControlManager is a singleton whose _cached_state persists across
    function-scope resets. Tests that patch is_baldur_enabled may fail
    when the underlying singleton retains stale state from a prior test.

    Calling the full reset_system_control() (which joins threads, etc.) is
    too heavy for function scope. Instead we just reset the cached state
    to the safe default — system enabled with default flags.
    """
    sc_mod = sys.modules.get("baldur.services.system_control")
    if sc_mod is None:
        return
    mgr = getattr(sc_mod, "_system_control", None)
    if mgr is None:
        return
    try:
        # Reset cached state to defaults (enabled=True, dry_run=False)
        state_cls = getattr(sc_mod, "SystemState", None)
        if state_cls is not None:
            mgr._cached_state = state_cls()
    except Exception:
        pass


def _restore_leaked_mocks():
    """Restore key module functions if a test leaked a mock via @patch.

    Some tests patch baldur.metrics.prometheus.get_metrics (and similar).
    If the patch teardown runs after the module-scoped fixture teardown,
    or a test crashes before patch cleanup, the mock persists. We detect
    this by checking for the _mock_name attribute (present on all Mock objects)
    and restore the original from a saved reference.
    """
    for mod_name, func_name in _MOCK_LEAK_WATCHLIST:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        current = getattr(mod, func_name, None)
        if current is not None and hasattr(current, "_mock_name"):
            original = _original_functions.get((mod_name, func_name))
            if original is not None:
                setattr(mod, func_name, original)


def _post_reset_recovery():
    """Restore import-time registrations and test-env state after autodiscovery.

    Autodiscovery calls all reset_*() which may destroy:
    1. ProviderRegistry registrations → re-register adapters + ML models
    2. Prometheus lazy-init flags → reset so get_or_create_* re-initializes safely
    3. WAL state → re-disable for tests (autodiscovery may reset via audit.base)
    4. Redis negative cache → re-seed to avoid 8s TCP timeout
    5. Leaked mocks → restore original functions
    """
    _restore_leaked_mocks()
    _post_reset_provider_registry()
    _reset_prometheus_registry()

    # Re-register default EventBus handlers after reset.
    # bus.reset() clears _handlers_registered, so re-registration is safe.
    bus_mod = sys.modules.get("baldur.services.event_bus.bus.default_handlers")
    if bus_mod is not None:
        try:
            bus_mod.register_default_handlers()
        except Exception:
            pass

    # Re-disable WAL (test env invariant).
    # 416 D1: clear cached WAL instance + init-failed flag so the next test
    # picks up whatever AuditSettings.enabled is at that point.
    wal_mod = sys.modules.get("baldur_pro.services.audit.base")
    if wal_mod is not None:
        try:
            wal_mod._reset_wal_state()
        except (AttributeError, TypeError):
            pass

    # Re-seed Redis negative cache via runtime state (test env invariant).
    import time as _time

    redis_mod = sys.modules.get("baldur.adapters.redis")
    if redis_mod is not None:
        try:
            _state = redis_mod._redis_state()
            _state.unavailable = True
            _state.fail_time = _time.monotonic()
        except AttributeError:
            pass

    # Suppress CellHealthAggregator Prometheus half-open probe (test env invariant)
    # When aggregator singleton exists after reset, seed failure state so it stays
    # in fallback mode instead of attempting real HTTP calls to localhost:9090.
    # Pattern: same as Redis negative cache above.
    cell_mod = sys.modules.get("baldur.services.cell_topology.health")
    if cell_mod is not None:
        agg = getattr(cell_mod, "_aggregator", None)
        if agg is not None:
            try:
                agg._prometheus_consecutive_failures = (
                    agg._settings.prometheus_max_consecutive_failures
                )
                agg._last_prometheus_failure_time = _time.monotonic()
            except AttributeError:
                pass


# =============================================================================
# D4 — Subscription Count Sentinel
# =============================================================================


def _get_subscription_count() -> int:
    """Return total EventBus subscription count (0 if bus not loaded)."""
    bus_mod = sys.modules.get("baldur.services.event_bus.bus.convenience")
    if bus_mod is None:
        return 0
    try:
        bus = bus_mod.get_event_bus()
        return sum(len(subs) for subs in bus._subscriptions.values())
    except Exception:
        return 0


# =============================================================================
# Two-Tier Fixtures
# =============================================================================


@pytest.fixture(autouse=True, scope="module")
def _module_reset_singletons():
    """Heavy autodiscovery reset between test FILES.

    Scans all baldur.* modules for reset_*() functions and singleton
    reset methods, calls them in safe phase order (thread-owners first,
    infrastructure last), then recovers import-time registrations.

    Self-maintaining: new singletons with reset_*() are found automatically.
    Only the recovery phase (_post_reset_recovery) needs manual updates when
    a new "recovery-needed" singleton is added (rare — ~1-2 per year).

    Runs ~1,116 times (once per test module), not ~21,800 (per function).
    """
    _discover_and_call_resets()
    _post_reset_recovery()
    setup_count = _get_subscription_count()
    yield
    _discover_and_call_resets()
    _post_reset_recovery()
    teardown_count = _get_subscription_count()
    if teardown_count != setup_count:
        warnings.warn(
            f"EventBus subscription count drift: setup had {setup_count}, "
            f"teardown has {teardown_count}. "
            f"Recovery is non-deterministic (possible race condition).",
            stacklevel=2,
        )


def _reset_runtime_caches():
    """Reset the runtime's settings + singleton caches between test functions.

    Closes the within-file leak class (403 two-tier model) that the heavy
    between-FILE reset catches but the light between-FUNCTION tier previously
    missed: a test that populates ``BaldurRuntime._settings`` (e.g.
    ``get_protect_settings()`` under a patched ``BALDUR_*`` env) or
    ``_singletons`` (e.g. the shared ``state_backend`` store) leaks that cache
    to later tests on the same xdist worker, surfacing as nondeterministic
    ``--dist=loadfile`` flakes whenever an unrelated test-file add/move
    reshuffles file→worker co-location.

    Resets in place (``reset_settings(None)`` + clearing ``_singletons``)
    rather than via ``reset_runtime()`` — the latter nulls the process-global
    ``_default_runtime`` and hands out a fresh object, breaking any caller that
    captured a runtime reference (the identity risk 403 cited when deferring
    this promotion). Reaching ``_singletons`` directly (no public bulk reset
    exists) mirrors ``_reset_system_control_light``'s "the full public reset is
    too heavy for function scope" precedent. Per the established light-tier
    philosophy (see ``_reset_startup_integrations_slot``), this drops cached
    references only — tests still own teardown of anything they started.

    Promotion validated by a full ``-n6`` A/B (30016 tests, both arms 0 failed,
    +0.9% wall = noise): zero #636-class tests depended on the leaked state.
    """
    lp_mod = sys.modules.get("baldur.settings.layered_provider")
    if lp_mod is not None:
        reset_fn = getattr(lp_mod, "reset_layered_settings_cached", None)
        if reset_fn is not None:
            reset_fn()

    rt_mod = sys.modules.get("baldur.runtime")
    if rt_mod is None:
        return
    try:
        rt = rt_mod.get_runtime()
        rt.reset_settings(None)
        rt._singletons.clear()
    except (AttributeError, TypeError):
        pass


def _reset_rate_limit_coordinator():
    """Drop the RateLimitCoordinator singleton and its warn-once dedup state.

    The synchronous retry stage resolves this process-global singleton by
    default, so any test that drives the retry loop with a 429-classified
    failure and an identified domain touches it without asking. Left standing,
    the instance carries cooldown state (and pending cooldown ``Timer`` threads)
    into unrelated tests on the same xdist worker. ``reset_instance()`` cancels
    those timers, which also serves the daemon-thread-leak watch.

    The dedup set behind the unidentified-domain WARNING is process-global for
    the same reason and is cleared here so a test can assert the log fires.
    """
    coord_mod = sys.modules.get("baldur.services.rate_limit_coordinator.coordinator")
    if coord_mod is not None:
        try:
            coord_mod.RateLimitCoordinator.reset_instance()
        except (AttributeError, TypeError):
            pass

    policy_mod = sys.modules.get("baldur.services.retry_handler.policy")
    if policy_mod is not None:
        warned = getattr(policy_mod, "_unidentified_key_warned", None)
        if warned is not None:
            warned.clear()


def _iter_provider_registry_slots():
    """Yield the ProviderRegistry's GenericProviderRegistry slots (``[]`` if the
    factory package is not imported yet)."""
    pr_mod = sys.modules.get("baldur.factory")
    if pr_mod is None:
        return []
    try:
        from baldur.factory.base import GenericProviderRegistry
    except ImportError:
        return []
    return [
        v
        for v in vars(pr_mod.ProviderRegistry).values()
        if isinstance(v, GenericProviderRegistry)
    ]


def _snapshot_provider_registrations():
    """Snapshot every provider slot's full state, paired with
    ``_restore_provider_registrations`` around the test body.

    ``_clear_provider_instances`` keeps registrations (to preserve the
    import-time cache=memory/queue=sync defaults), so a registration a test
    leaks into a slot would otherwise survive into later same-worker tests and
    let a stale manager intercept a ``from_settings()`` env-fallback read (the
    #636 ProviderRegistry leak vector). Snapshot-at-setup / restore-at-teardown
    preserves the per-test baseline (import-time defaults + PRO autouse
    registrations) while rolling back only the test's own additions.
    """
    return [(slot, slot.save_state()) for slot in _iter_provider_registry_slots()]


def _restore_provider_registrations(snapshots):
    """Restore slots captured by ``_snapshot_provider_registrations``."""
    for slot, state in snapshots:
        try:
            slot.restore_state(state)
        except (KeyError, TypeError):
            pass


@pytest.fixture(autouse=True)
def auto_reset_all_state():
    """Light reset between test FUNCTIONS.

    Handles fast-changing state only: settings cache, ContextVars,
    ProviderRegistry instances + registrations, runtime settings/singleton
    caches, governance cache, system control state. ~0.1ms per call — safe for
    ~21,800 invocations per suite.
    """
    _repin_observability_profile()
    _reset_root_config()
    _reset_context_vars()
    _clear_provider_instances()
    _reset_startup_integrations_slot()
    _clear_inmemory_lock_registry()
    _invalidate_governance_cache()
    _reset_system_control_light()
    _reset_runtime_caches()
    _reset_rate_limit_coordinator()
    _registration_snapshot = _snapshot_provider_registrations()

    yield

    _reset_runtime_caches()
    _reset_rate_limit_coordinator()
    _restore_provider_registrations(_registration_snapshot)
    _repin_observability_profile()
    _reset_root_config()
    _reset_context_vars()
    _clear_provider_instances()
    _reset_startup_integrations_slot()
    _clear_inmemory_lock_registry()
    _invalidate_governance_cache()
    _reset_system_control_light()


# Directories whose tests explicitly verify governance blocking behavior.
# These opt out of the governance bypass fixture below.
_GOVERNANCE_TEST_DIRS = frozenset(
    {
        "services/governance",
        "governance",
    }
)


def _is_governance_test(item) -> bool:
    """Check if a test item should opt out of governance auto-bypass."""
    # Explicit marker takes priority
    if "governance" in {m.name for m in item.iter_markers()}:
        return True
    # Auto-detect by directory: tests/unit/services/governance/, tests/unit/governance/
    nodeid = item.nodeid.replace("\\", "/")
    return any(d in nodeid for d in _GOVERNANCE_TEST_DIRS)


_PRO_SINGLETON_SLOTS: tuple[tuple[str, str, str], ...] = (
    # (registry_slot, source_module_path, getter_name) — mirrors
    # baldur_pro.__init__._register_singleton_providers() (519 PR 2).
    # Source module paths (where the function is defined) — tests that
    # patch ``baldur_pro.services.X.get_Y`` rely on the slot factory
    # re-resolving the function at each call, not capturing the reference
    # at registration time.
    (
        "emergency_manager",
        "baldur_pro.services.emergency_mode",
        "get_emergency_manager",
    ),
    (
        "adaptive_throttle",
        "baldur_pro.services.throttle.adaptive",
        "get_adaptive_throttle",
    ),
    (
        "bulkhead_registry",
        "baldur_pro.services.bulkhead",
        "get_bulkhead_registry",
    ),
    (
        "runtime_config_manager",
        "baldur_pro.services.runtime_config",
        "get_runtime_config_manager",
    ),
    (
        "chaos_scheduler",
        "baldur_pro.services.chaos",
        "get_chaos_scheduler",
    ),
    ("report_generator", "baldur_pro.services.chaos.reports", "get_report_generator"),
    ("safety_guard", "baldur_pro.services.chaos.safety_guard", "get_safety_guard"),
    ("dlq_service", "baldur_pro.services.dlq", "get_dlq_service"),
    ("dlq_repository", "baldur_pro.services.dlq.base", "get_dlq_repository"),
    (
        "selfhealer_watchdog",
        "baldur_pro.services.meta_watchdog",
        "get_selfhealer_watchdog",
    ),
    (
        "error_budget_service",
        "baldur_pro.services.error_budget",
        "get_error_budget_service",
    ),
    (
        "error_budget_gate",
        "baldur_pro.services.error_budget_gate",
        "get_error_budget_gate",
    ),
    (
        "canary_rollout_service",
        "baldur_pro.services.canary",
        "get_canary_rollout_service",
    ),
    (
        "blast_radius_manager",
        "baldur_pro.services.chaos.blast_radius",
        "get_blast_radius_manager",
    ),
)


def _make_pro_singleton_factory(module_path: str, getter_name: str):
    """Return a slot factory that re-resolves the PRO getter at each call.

    Re-resolution preserves test compatibility with ``mock.patch
    ("baldur_pro.services.X.get_Y", ...)`` — captured references would
    short-circuit such patches.
    """
    import importlib

    def factory():
        module = importlib.import_module(module_path)
        return getattr(module, getter_name)()

    return factory


@pytest.fixture(autouse=True)
def _pro_singleton_providers_registered():
    """Register PRO singleton getters with ProviderRegistry during tests.

    519 PR 2 (c) migration: OSS callsites resolve singletons via
    ``ProviderRegistry.<slot>.safe_get()``. In the test process,
    ``baldur_pro.register_pro_services()`` is skipped because the entitlement
    check returns INACTIVE, so each slot would otherwise stay empty and
    ``safe_get()`` would return ``None``. Tests that exercise the
    OSS->PRO path expect the resolved PRO singleton — register the same
    getter set as production here.
    """
    from baldur.factory.registry import ProviderRegistry

    registered: list[tuple[str, str | None]] = []
    for slot_name, module_path, getter_name in _PRO_SINGLETON_SLOTS:
        pro_mod = sys.modules.get(module_path)
        if pro_mod is None or not hasattr(pro_mod, getter_name):
            continue
        slot = getattr(ProviderRegistry, slot_name)
        prior_default = slot.get_default_name()
        slot.register("pro", _make_pro_singleton_factory(module_path, getter_name))
        slot.set_default("pro")
        slot.clear_instances()
        registered.append((slot_name, prior_default))
    try:
        yield
    finally:
        for slot_name, prior_default in registered:
            slot = getattr(ProviderRegistry, slot_name)
            if prior_default is None:
                slot.reset()
            else:
                slot.set_default(prior_default)
                slot.clear_instances()


@pytest.fixture(autouse=True)
def _pro_governance_provider_registered():
    """Register PROGovernanceChecker as the default provider during tests.

    518 (b) migration: OSS callsites resolve governance via
    ``ProviderRegistry.governance.get()``. In the test process,
    ``baldur_pro.register_pro_services()`` is skipped because the entitlement
    check returns INACTIVE, so the registry would otherwise hand back the
    OSS NoOp default. Tests that patch
    ``baldur_pro.services.governance.checks.<func>`` rely on PROGovernanceChecker
    sitting on the call path and resolving the patched module attribute
    dynamically — register it here so that path exists.
    """
    from baldur.factory.registry import ProviderRegistry

    pro_mod = sys.modules.get("baldur_pro.services.governance")
    prior_default = ProviderRegistry.governance.get_default_name()
    registered = False
    if pro_mod is not None and hasattr(pro_mod, "PROGovernanceChecker"):
        ProviderRegistry.governance.register("pro", pro_mod.PROGovernanceChecker)
        ProviderRegistry.governance.set_default("pro")
        ProviderRegistry.governance.clear_instances()
        registered = True
    try:
        yield
    finally:
        if registered and prior_default is not None:
            ProviderRegistry.governance.set_default(prior_default)
            ProviderRegistry.governance.clear_instances()


@pytest.fixture(autouse=True)
def _governance_test_bypass(request, _pro_governance_provider_registered):
    """Bypass governance checks for non-governance tests.

    Governance singletons (SystemControlManager, EmergencyManager, ErrorBudget)
    retain cached state across function-scope resets. Rather than resetting all
    of them per-function (~expensive, thread-unsafe), we mock check_all_governance()
    to return allowed=True for tests that don't care about governance.

    Opt-out: @pytest.mark.governance or test path containing 'governance/'.
    """
    if _is_governance_test(request.node):
        yield
        return

    # No-op when baldur_pro is absent (OSS-only checkout — governance resolves to
    # the OSS NoOp checker, which already allows, so there is nothing to bypass).
    # mock.patch() imports its string target at __enter__, so an unguarded patch
    # of the baldur_pro path errors every non-governance test's setup when the PRO
    # tier is absent. Mirrors the _recovery_gate_metrics_bypass guard below.
    try:
        import baldur_pro.services.governance.checks as _gov_checks
    except ImportError:
        yield
        return

    from unittest.mock import patch

    gov_mod = sys.modules.get("baldur.models.governance")
    if gov_mod is not None:
        result = gov_mod.GovernanceCheckResult.allowed_result()
    else:
        # Module not imported yet — use a simple object
        from unittest.mock import MagicMock

        result = MagicMock(allowed=True)

    with patch.object(_gov_checks, "check_all_governance", return_value=result):
        yield


@pytest.fixture(autouse=True)
def _recovery_gate_metrics_bypass(request):
    """Neutralise the recovery gate's live metric read for non-opted-in tests.

    608 D3 makes the recovery gate's DEFAULT metrics checker live: it reads
    live CPU from the global SystemMetricsCache and the aggregate
    circuit-breaker failure rate. Every test that drives the emergency
    manager's gate through its default construction path —
    GracefulDegradationManager() -> RecoveryGate(config=...) with no injected
    checker -> deactivate(force=False) / start_gradual_recovery() — would
    otherwise run _live_metrics_checker against the unstarted global cache (no
    Django adapter in unit tests), fail closed, and raise
    RecoveryNotAllowedError. These tests passed before only because the old
    placeholder always reported "stable".

    Patch the default-checker METHOD (not check_recovery_allowed): tests that
    inject their own checker bypass it entirely (so the recovery_gate gate
    tests keep asserting real block/allow logic), and only the gate's metric
    READ is neutralised.

    Opt-out: @pytest.mark.recovery_gate_live (the live-metrics test file),
    which exercises the real _live_metrics_checker via its own cache /
    CB-service monkeypatching.

    No-op when baldur_pro is absent (OSS-only checkout — the PRO manager slot
    is empty anyway).
    """
    if "recovery_gate_live" in {m.name for m in request.node.iter_markers()}:
        yield
        return

    try:
        from baldur_pro.services.emergency_mode.recovery_gate import RecoveryGate
    except ImportError:
        yield
        return

    from unittest.mock import patch

    with patch.object(
        RecoveryGate,
        "_live_metrics_checker",
        return_value={"cpu_percent": 0.0, "error_rate": 0.0},
    ):
        yield


# =============================================================================
# Logging State Isolation (session + function scope)
# =============================================================================


@pytest.fixture(autouse=True, scope="session")
def _isolate_logging_state_session():
    """
    One-time session setup: force baldur loggers propagate=True,
    remove handlers, set root log level from BALDUR_TEST_LOG_LEVEL.

    Root cause: django.setup() applies LOGGING config that sets
    propagate=False + StreamHandler on baldur loggers, preventing
    caplog from seeing records (caplog handler lives on root logger).

    Performance: session scope avoids per-test Logger.setLevel() which
    calls _clear_cache() and traverses all loggers (Python 3.12).
    """
    import logging as _logging

    root = _logging.getLogger()
    root_level = root.level
    root_handlers = list(root.handlers)

    # Override log level from env
    test_log_level_name = os.environ.get("BALDUR_TEST_LOG_LEVEL", "WARNING")
    test_log_level = getattr(_logging, test_log_level_name.upper(), _logging.WARNING)
    root.setLevel(test_log_level)

    # Suppress noisy loggers
    _noisy_loggers = ("faker", "faker.factory", "urllib3", "asyncio", "parso")
    _infra_noise_loggers = (
        "baldur.api.django.pool_circuit_breaker",
        "baldur.adapters.audit.redis_buffer",
    )
    _noisy_saved: dict[str, int] = {}
    for _name in _noisy_loggers:
        _lg = _logging.getLogger(_name)
        _noisy_saved[_name] = _lg.level
        _lg.setLevel(test_log_level)
    for _name in _infra_noise_loggers:
        _lg = _logging.getLogger(_name)
        _noisy_saved[_name] = _lg.level
        _lg.setLevel(_logging.ERROR)

    # Save + force propagate on baldur namespace
    _saved: dict[str, tuple[int, bool, list]] = {}
    for name, logger_obj in _logging.Logger.manager.loggerDict.items():
        if isinstance(logger_obj, _logging.Logger) and name.startswith("baldur"):
            _saved[name] = (
                logger_obj.level,
                logger_obj.propagate,
                list(logger_obj.handlers),
            )
            logger_obj.propagate = True
            logger_obj.handlers = []

    yield

    # Restore on session end
    root.setLevel(root_level)
    root.handlers = root_handlers
    for _name, _prev_level in _noisy_saved.items():
        _logging.getLogger(_name).setLevel(_prev_level)
    for name, (level, propagate, handlers) in _saved.items():
        logger_obj = _logging.getLogger(name)
        logger_obj.setLevel(level)
        logger_obj.propagate = propagate
        logger_obj.handlers = handlers


@pytest.fixture(autouse=True)
def _ensure_baldur_propagates():
    """
    Per-test: ensure baldur AND baldur_pro root loggers propagate=True.

    django.setup() -> dictConfig() can re-set propagate=False + console handler.
    baldur_pro test modules (saga, throttle, etc.) emit through their own
    logger namespace — without restoring propagate on it, caplog cannot
    capture their WARNING/ERROR records.
    """
    import logging as _logging

    for ns in ("baldur", "baldur_pro"):
        sh = _logging.getLogger(ns)
        sh.propagate = True
        sh.handlers = []
    return


@pytest.fixture(autouse=True)
def _restore_canonical_structlog_config():
    """Hold the canonical (cache=False, configured=True) structlog config at
    every test boundary — the suite-wide containment for the ``capture_logs``
    xdist flake (578 D2).

    The flake is a *freeze*: production ``configure_structlog()`` sets
    ``cache_logger_on_first_use=True``, and once a module-level
    ``logger = structlog.get_logger()`` proxy first-emits while caching is armed
    it permanently caches a stdlib-routed bound logger that ``capture_logs``
    can no longer intercept. Containment is two-pronged (584): restoring
    ``cache=False`` + holding ``configured=True`` PREVENTS *new* freezes, and
    ``_unfreeze_module_loggers`` UN-freezes any proxy already frozen earlier in
    the worker's life (which ``cache=False`` alone cannot reach — 578's residual).

    Applied at BOTH setup and teardown:

    - **Setup** closes the freeze window and un-freezes. A module-boundary
      autodiscovery reset (``reset_structlog_config()`` → ``configured=False``)
      otherwise leaves the first test of each module able to re-arm caching via
      any unmocked ``protect()`` / ``baldur.init()``; forcing ``configured=True``
      at setup makes those calls no-op so no victim proxy freezes. This does NOT
      race the poison-test files (``settings/test_structlog_config.py``,
      ``test_public_api.py``): a root-conftest autouse fixture's setup runs
      BEFORE any module-local autouse fixture, so their own fixtures re-assert
      ``configured=False`` afterward and still exercise ``configure_structlog()``.
      The un-freeze walk (584 D5) then repairs any ``baldur*`` proxy a prior test
      on this worker froze, so a victim test sees a capturable proxy regardless of
      what poisoned it earlier. Un-freeze is setup-only: it repairs the *prior*
      freeze, so a teardown un-freeze would run after this test's assertions and
      be redone by the next setup — strictly redundant.
    - **Teardown** runs LAST (root-conftest autouse finalizes in reverse setup
      order, after the poison file's own teardown) so the next test starts clean.

    Subsumes the former per-file ``capture_ready_*`` rebind and
    ``_structlog_reset*`` guards. See ``project_xdist_capture_logs_flake``.
    """
    _apply_canonical_test_structlog_config()
    # Un-freeze any baldur* module proxy frozen by a prior test on this worker
    # (584 D5). Snapshot via list(...) so the walk never races a concurrent
    # sys.modules mutation (#453 iteration-race constraint).
    _unfreeze_module_loggers(
        module
        for name, module in list(sys.modules.items())
        if name.startswith("baldur")
    )
    yield
    _apply_canonical_test_structlog_config()


# =============================================================================
# Watchdog Singleton Reset (non-autouse — explicit opt-in)
# =============================================================================


def _cleanup_watchdog(aw_module):
    """Watchdog instance cleanup helper."""
    instance = getattr(aw_module, "_watchdog_instance", None)
    if instance is None:
        return
    try:
        instance.stop()
        thread = getattr(instance, "_thread", None)
        if thread and thread.is_alive():
            thread.join(timeout=0.2)
    except Exception:
        pass
    aw_module._watchdog_instance = None


@pytest.fixture
def reset_watchdog_singleton():
    """Reset AuditWatchdog singleton before/after test (explicit opt-in)."""
    aw_module = sys.modules.get("baldur.audit.audit_watchdog")
    if aw_module is None:
        import baldur.audit.audit_watchdog as aw_module

    _cleanup_watchdog(aw_module)
    yield
    _cleanup_watchdog(aw_module)


# =============================================================================
# Audit Module Reload Fixture (mock isolation)
# =============================================================================

# Audit modules to pop from sys.modules (dependency-reverse order)
_AUDIT_MODULES_TO_CLEAR = frozenset(
    [
        "baldur_pro.services.audit",
        "baldur_pro.services.audit",
        "baldur_pro.services.audit.retry_audit",
        "baldur_pro.services.audit.chaos_audit",
        "baldur_pro.services.audit.dlq_audit",
        "baldur_pro.services.audit.compliance_audit",
        "baldur_pro.services.audit.storage_audit",
        "baldur_pro.services.audit.cb_audit",
        "baldur_pro.services.audit.base",
    ]
)


@pytest.fixture(autouse=True)
def reset_audit_modules(auto_reset_all_state):
    """Remove audit modules from sys.modules for mock isolation.

    Depends on auto_reset_all_state to guarantee teardown order:
      auto_reset_all_state teardown (scan + reset) → reset_audit_modules teardown (pop)
    Without this dependency, reset_audit_modules may pop modules BEFORE
    autodiscovery scans them, causing reset function leaks.
    """
    loaded = _AUDIT_MODULES_TO_CLEAR & sys.modules.keys()
    if not loaded:
        yield
        loaded = _AUDIT_MODULES_TO_CLEAR & sys.modules.keys()
        for mod_name in loaded:
            sys.modules.pop(mod_name, None)
        return

    for mod_name in loaded:
        sys.modules.pop(mod_name, None)

    yield

    loaded = _AUDIT_MODULES_TO_CLEAR & sys.modules.keys()
    for mod_name in loaded:
        sys.modules.pop(mod_name, None)


# =============================================================================
# OSS->PRO Helper Cache Reset (518 batch a)
# =============================================================================
# The baldur.{audit,notification,dlq}.helpers modules cache the resolved PRO
# module in module-level globals (_pro/_resolved). When reset_audit_modules
# pops baldur_pro.* from sys.modules between tests, the cached reference
# becomes stale (points at the popped module object). These autouse fixtures
# clear the cache so the next helper call re-resolves against the current
# sys.modules state.


@pytest.fixture(autouse=True)
def reset_audit_helpers():
    """Reset baldur.audit.helpers._pro/_resolved cache between tests."""
    import baldur.audit.helpers as h

    h._pro = None
    h._resolved = False
    yield
    h._pro = None
    h._resolved = False


@pytest.fixture(autouse=True)
def reset_notification_helpers():
    """Reset baldur.notification.helpers._pro/_resolved cache between tests."""
    import baldur.notification.helpers as h

    h._pro = None
    h._resolved = False
    yield
    h._pro = None
    h._resolved = False


@pytest.fixture(autouse=True)
def reset_dlq_helpers():
    """Reset baldur.dlq.helpers caches + the OSS DLQ capture singleton.

    The DLQ-store backing resolves through the provider registry (not a module
    import cache), so only the compression / postmortem module caches remain
    here; "PRO absent" for the store path is simulated by leaving the
    ``dlq_service`` slot empty. ``reset_dlq_capture_service`` clears the OSS
    capture singleton so a prior test's config / repository does not leak.
    """
    import baldur.dlq.helpers as h
    from baldur.services.dlq_capture import reset_dlq_capture_service

    h._pro_dlq_compression = None
    h._pro_postmortem_store = None
    h._resolved_dlq_compression = False
    h._resolved_postmortem_store = False
    reset_dlq_capture_service()
    yield
    h._pro_dlq_compression = None
    h._pro_postmortem_store = None
    h._resolved_dlq_compression = False
    h._resolved_postmortem_store = False
    reset_dlq_capture_service()


# =============================================================================
# DB Test Auto-Skip
# =============================================================================


def pytest_sessionstart(session):
    """Assert the BALDUR_TEST_MODE contract once per session (453 D7).

    The framework's "in-test" signal is eager-read by BaldurRuntime at
    construction time (during ``django.setup() → apps.ready() → baldur.init()``,
    which fires inside ``pytest_load_initial_conftests``). If something in the
    parent process or CI overrides the testapp/settings.py setdefault,
    BaldurRuntime sees production mode and the bootstrap-driven
    cluster_identity validation flips quarantine globally — silently breaking
    any test that does not explicitly opt in. This sessionstart check turns
    that drift into an immediate hard error.

    Tests that need to exercise the per-call BALDUR_TEST_MODE checks in
    runtime code (audit shutdown handler, runbook service, etc.) still use
    ``monkeypatch.setenv`` / ``patch.dict`` for the scope of that test —
    those scoped overrides are restored at test teardown and never reach
    this contract check.
    """
    if os.environ.get("BALDUR_TEST_MODE", "").lower() != "true":
        raise pytest.UsageError(
            "BALDUR_TEST_MODE is not set to 'true' at session start. "
            "Check parent process / CI env for an override; per-test "
            "production-mode coverage should use monkeypatch.setenv "
            "(or @patch.dict) for the scope of that test."
        )

    _disarm_init_installed_signal_handlers()


def _disarm_init_installed_signal_handlers() -> None:
    """Unwind the ``baldur.init()``-installed OS signal handlers (597).

    The session-start ``baldur.init()`` (see ``pytest_sessionstart``
    docstring) runs ``coordinator.register_signals()`` for real in every
    pytest process — controller and each xdist worker. Since 597 the
    coordinator handler OWNS process exit: a SIG_DFL-tail (defer-exit)
    handler that gets invoked arms a post-drain ``os.kill(os.getpid(),
    signum)`` from the drain thread, which on Windows is an immediate
    TerminateProcess. Any test that invokes a captured handler chain —
    e.g. the gunicorn chained-handler contract tests in
    ``tests/unit/adapters/gunicorn/test_hooks.py`` — would therefore
    hard-kill its xdist worker (``node down: Not properly terminated``)
    and stall the whole run. Pre-597 the same invocation merely started
    a silent drain, which is why this was never visible before.

    Unit tests must never run against live coordinator handlers
    (UNIT_TEST_GUIDELINES xdist safety; 597 Testability Notes). Walk the
    Baldur chain markers back to the pre-init disposition and restore it.
    Production wiring is untouched — this runs only in pytest processes.
    """
    import signal as _signal

    for sig in (_signal.SIGTERM, _signal.SIGINT):
        handler = _signal.getsignal(sig)
        if not callable(handler) or not (
            hasattr(handler, "_baldur_coordinator")
            or hasattr(handler, "_baldur_chained_original")
        ):
            continue
        # Walk the chain markers to the pre-Baldur tail.
        node = handler
        seen: set[int] = set()
        while (
            callable(node)
            and hasattr(node, "_baldur_chained_original")
            and id(node) not in seen
        ):
            seen.add(id(node))
            node = node._baldur_chained_original
        if node is None:
            # C-level disposition cannot be restored from Python; leave
            # the chain in place rather than guess.
            continue
        _signal.signal(sig, node)


# Single source of truth for the @pytest.mark.flaky_quarantine `category=` enum.
# Maps to UNIT_TEST_GUIDELINES.md §6.6 taxonomy. Tools that introspect the
# marker (validator below, audit tooling, future dashboards) import this set
# directly so the vocabulary stays consistent.
FLAKY_CATEGORIES: frozenset[str] = frozenset(
    {
        "state_leak",
        "timing",
        "race_condition",
        "external_dep",
        "mock_leak",
        "env_isolation",
        "unknown",
    }
)


def pytest_collection_modifyitems(config, items):
    """Auto-skip django_db tests when database is not available.

    Also validates marker schemas:
    - ``@pytest.mark.broken`` items must have a ``ref:`` comment linking to
      a tracking issue or document.
    - ``@pytest.mark.flaky_quarantine`` items must declare ``issue=`` and
      ``first_seen=`` (ISO YYYY-MM-DD); ``category=`` must be in
      ``FLAKY_CATEGORIES`` when supplied.
    """
    # --- django_db auto-skip ---
    db_available = os.environ.get("BALDUR_TEST_DB_AVAILABLE", "false").lower() == "true"

    if not db_available:
        skip_db = pytest.mark.skip(
            reason="Database not available (set BALDUR_TEST_DB_AVAILABLE=true to run)"
        )
        for item in items:
            if "django_db" in [marker.name for marker in item.iter_markers()]:
                item.add_marker(skip_db)

    # --- broken ref-validator ---
    validated_paths: set = set()
    for item in items:
        if "broken" not in [m.name for m in item.iter_markers()]:
            continue
        path = item.path
        if path in validated_paths:
            continue
        validated_paths.add(path)
        source = path.read_text(encoding="utf-8")
        if "ref:" not in source:
            raise pytest.UsageError(
                f"{path}: @pytest.mark.broken requires a 'ref:' comment "
                f"linking to a tracking issue or document"
            )

    # --- flaky_quarantine schema-validator ---
    # Mirrors the broken-validator pattern above (iter_markers, not
    # get_closest_marker) so the same _FakeItem stand-in tests both branches.
    for item in items:
        marker = next(
            (m for m in item.iter_markers() if m.name == "flaky_quarantine"),
            None,
        )
        if marker is None:
            continue
        kwargs = getattr(marker, "kwargs", {}) or {}
        node_id = getattr(item, "nodeid", None) or str(item.path)

        issue = kwargs.get("issue")
        if not isinstance(issue, str) or not issue:
            raise pytest.UsageError(
                f"{node_id}: @pytest.mark.flaky_quarantine requires a non-empty "
                f"'issue=' kwarg (e.g. issue='GH-477' or issue='455')"
            )

        first_seen = kwargs.get("first_seen")
        if not isinstance(first_seen, str) or not first_seen:
            raise pytest.UsageError(
                f"{node_id}: @pytest.mark.flaky_quarantine requires a "
                f"'first_seen=' kwarg in ISO format YYYY-MM-DD"
            )
        try:
            datetime.strptime(first_seen, "%Y-%m-%d")
        except ValueError as exc:
            raise pytest.UsageError(
                f"{node_id}: @pytest.mark.flaky_quarantine first_seen="
                f"{first_seen!r} is not a valid ISO date (YYYY-MM-DD)"
            ) from exc

        category = kwargs.get("category")
        if category is not None and category not in FLAKY_CATEGORIES:
            raise pytest.UsageError(
                f"{node_id}: @pytest.mark.flaky_quarantine category="
                f"{category!r} is not in the allowed set "
                f"{sorted(FLAKY_CATEGORIES)} (see UNIT_TEST_GUIDELINES.md §6.6)"
            )


# =============================================================================
# Core Type Fixtures
# =============================================================================


@pytest.fixture
def sample_failed_operation_data():
    """Sample failed operation data for tests."""
    from baldur.interfaces.repositories import FailedOperationData

    return FailedOperationData(
        id=1,
        domain="order",
        failure_type="network",
        status="pending",
        created_at=datetime.now(),
        metadata={"order_id": 123, "amount": 10000},
        error_message="Connection timeout",
        retry_count=0,
        max_retries=3,
    )


@pytest.fixture
def sample_circuit_breaker_data():
    """Sample circuit breaker state data for tests."""
    from baldur.interfaces.repositories import CircuitBreakerStateData

    return CircuitBreakerStateData(
        service_name="external-gateway",
        state="closed",
        failure_count=0,
        success_count=10,
    )


@pytest.fixture
def sample_config():
    """Sample configuration for tests."""
    from baldur.core.config import BaldurConfig

    return BaldurConfig()


# =============================================================================
# Chaos Engineering Fixtures
# =============================================================================


@pytest.fixture
def failure_injector():
    """Provides a configurable failure injector for chaos tests."""
    from tests.chaos.conftest import FailureInjector

    return FailureInjector(failure_rate=0.3)


@pytest.fixture
def burst_failure_injector():
    """Provides a burst failure pattern injector."""
    from tests.chaos.conftest import BurstFailureInjector

    return BurstFailureInjector(burst_size=10, burst_interval=50)


@pytest.fixture
def latency_injector():
    """Provides a latency injector for slow degradation tests."""
    from tests.chaos.conftest import LatencyInjector

    return LatencyInjector(
        min_latency_ms=100,
        max_latency_ms=30000,
        degradation_rate=100,
    )


@pytest.fixture
def resource_simulator():
    """Provides a resource exhaustion simulator."""
    from tests.chaos.conftest import ResourceExhaustionSimulator

    return ResourceExhaustionSimulator(max_connections=100)


# =============================================================================
# Pluggable Architecture Fixtures
# =============================================================================


@pytest.fixture
def memory_cache_adapter():
    """Provides an in-memory cache adapter for testing."""
    from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter

    adapter = InMemoryCacheAdapter(key_prefix="test:")
    yield adapter
    adapter.flush_all()


@pytest.fixture
def sync_queue_adapter():
    """Provides a synchronous task queue adapter for testing."""
    from baldur.adapters.queues.sync_adapter import SyncTaskAdapter

    return SyncTaskAdapter()


@pytest.fixture(autouse=False)
def test_provider_registry():
    """Setup and teardown provider registry for tests."""
    from baldur.factory import ProviderRegistry

    ProviderRegistry.set_defaults(
        cache="memory",
        queue="sync",
    )
    ProviderRegistry.clear_instances()

    yield ProviderRegistry

    ProviderRegistry.clear_instances()


# =============================================================================
# Tier Simulation Fixtures
# =============================================================================


@pytest.fixture
def mock_oss_tier(monkeypatch):
    """Simulate an install without the PRO distribution.

    Patches the canonical presence probe — never ``importlib.util.find_spec``
    itself, whose blast radius is every import in the process.
    """
    monkeypatch.setattr("baldur.utils.tier.is_pro_installed", lambda: False)


@pytest.fixture
def mock_pro_tier(monkeypatch):
    """Simulate an install with the PRO distribution present."""
    monkeypatch.setattr("baldur.utils.tier.is_pro_installed", lambda: True)
