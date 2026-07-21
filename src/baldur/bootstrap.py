"""
Baldur framework-agnostic bootstrap.

Provides ``baldur.init()`` — the single entry point that all framework
adapters (Django, FastAPI, Flask, plain Python) call at startup.

Responsibilities:
1. Validate startup config (Safe Defaults / Quarantine Mode) and CRITICAL
   secrets (production boot-abort gate, every framework adapter)
2. Register default event bus handlers
3. Register shutdown handlers (GracefulShutdownCoordinator)
4. Discover PRO entry-point hooks (``baldur.bootstrap_hooks``)
5. Apply audit default provider per ``AuditSettings.enabled``
6. Start audit pipeline (WAL + AuditSyncWorker) if audit is enabled
7. Record env_snapshot through the resolved audit adapter

Idempotency:
    ``init()`` is a silent no-op on re-entry. A module-level ``_init_done``
    flag plus ``threading.Lock`` guard against concurrent and repeat calls.
"""

# Reference: 416

from __future__ import annotations

import importlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog

if TYPE_CHECKING:
    from baldur.factory.base import GenericProviderRegistry
    from baldur.runtime import BaldurRuntime

logger = structlog.get_logger()

__all__ = ["init", "reset_init_state", "start_background_workers"]

# Writable-dir resolution identities of the two audit durability surfaces
# whose statuses must agree. The checkpoint is found by purpose — a stable
# class default on one class. The audit WAL is found by the override
# variable each WAL recorded at resolution rather than by a purpose string:
# a WAL purpose is built from a caller-supplied ``file_prefix``, so it
# differs per surface and per tier, and naming a variable the resolving
# surface does not read would hand the operator a remedy that changes
# nothing.
_AUDIT_CHECKPOINT_PURPOSE = "checkpoint"
_AUDIT_CHECKPOINT_ENV_VAR = "BALDUR_AUDIT_PATH"
_AUDIT_WAL_ENV_VAR = "BALDUR_AUDIT_WAL_DIR"


# 464 D9 — declarative table of registries that init() rewires from the
# module-load "memory" baseline to an environment-aware default. Cache is
# row 1 (carried forward from 463); the remaining rows are the Group A/B
# registries that init()'s framework-agnostic wiring enumerates.
class _BackendKind(str, Enum):
    """Categorizes the per-row dispatch path used by ``_wire_registry_defaults``."""

    REDIS = "redis"
    SQL_DJANGO = "sql_django"
    # 515 D6 — probe-surface registries that pick from a declared priority
    # chain of provider names. Generalizes the retired DJANGO_OR_NOOP kind
    # (which hard-coded a Django→noop sequence); now any chain like
    # django→sql→noop is expressible as data.
    PRIORITY_CHAIN = "priority_chain"


class _RegistryWiring(NamedTuple):
    """Per-row wiring config consumed by :func:`_wire_registry_defaults`.

    ``target_name`` semantics:
    - REDIS rows: the registered name of the Redis-backed adapter.
    - SQL_DJANGO rows: the registered name of the Django-backed adapter
      (the SQL adapter is uniformly registered as ``"sql"`` across all
      Group B discover_* functions, so it is dispatched implicitly inside
      :func:`_wire_sql_django_registry`).
    - PRIORITY_CHAIN rows: ``target_name`` is unused; the row's
      ``priority_chain`` of ``(provider_name, probe_callable)`` pairs is
      consulted instead. Set ``target_name=""`` to make this explicit.

    ``fallback_target`` is REDIS-only and currently exercised by a single
    row (``rate_limit_storage``) — when Redis is unset but
    Django+DATABASES is configured, the registry falls through to its
    Django ORM adapter rather than tripping the production fail-loud
    branch.

    ``priority_chain`` and ``env_override`` apply only to PRIORITY_CHAIN
    rows. The chain is consulted in declared order; the first
    pair whose probe returns True AND whose provider is registered wins.
    ``env_override``, when set, names an env var that operators can use
    to force a specific provider (e.g. ``BALDUR_PG_ADMIN_PROVIDER=sql``).

    ``reset_baseline`` is the provider name that
    :func:`reset_init_state` Step 3.5 restores for this row — it must
    equal the row's module-load ``set_default`` so a re-init starts from
    the same cold-process baseline. It is explicit per-row data rather
    than a kind→baseline heuristic because the baseline does not follow
    the dispatch kind: the probe-surface PRIORITY_CHAIN rows reset to
    ``"noop"`` (their only safe default), while a hybrid PRIORITY_CHAIN
    row that registers no ``noop`` adapter (e.g. ``event_journal_repo``,
    memory/redis/sql) must reset to ``"memory"``.
    """

    backend_kind: _BackendKind
    registry_attr: str  # ProviderRegistry attribute name
    target_name: str
    fallback_target: str | None = None
    priority_chain: tuple[tuple[str, Callable[[], bool]], ...] = ()
    env_override: str | None = None
    reset_baseline: str = "memory"


# Module-level idempotency state.
_init_done: bool = False
_init_lock: threading.Lock = threading.Lock()

# 463 D15 — known legacy aliases of "production" that previously satisfied
# the four divergent in-tree precedents. Hard-failing them at startup
# converts the silent regression into a CrashLoopBackOff so operators see
# the misconfig immediately.
_REJECTED_LEGACY_ALIASES: frozenset[str] = frozenset(
    {"prod", "live", "release", "stable"}
)

# 463 D12 — first-access WARNING flags. Production deploys never trip these
# because init() is the framework-adapter invariant; the warnings only fire
# in ad-hoc CLI / REPL / utility scripts that import baldur without calling
# init(). Each registry warns at most once per process.
_init_not_called_cache_warned: bool = False
_init_not_called_storage_warned: bool = False

# Once-per-process guard for the CB-state startup seed (D1). Mirrors
# MetricHydrator._hydration_done so a double invocation (Django init() per
# worker + post_worker_init) schedules the seed timer at most once per
# serving process.
_cb_state_seed_done: bool = False
_cb_state_seed_lock: threading.Lock = threading.Lock()


def init(
    quarantine_callback: Callable[[Exception], None] | None = None,
    task_backend: str = "inline",
) -> None:
    """Initialize the Baldur framework.

    Idempotent — safe to call multiple times. Second and later calls are
    silent DEBUG-level no-ops.

    Args:
        quarantine_callback: Optional callable invoked with the
            ``FatalConfigError`` instance when startup config validation
            fails fatally. Django passes its own ``_activate_quarantine_mode``
            wrapper here. FastAPI/Flask adapters may pass ``None``.
        task_backend: Scheduler execution mode (429 Part 6, C13).
            - ``"inline"`` (default): jobs run on the in-process LeaderScheduler
              thread. Zero extra dependency; matches Sentry / OTEL precedent.
            - ``"celery"``: scheduled ticks enqueue via the corresponding
              Celery ``@shared_task``'s ``.delay()`` instead of running inline.
              Requires ``pip install baldur-framework[celery]`` / an already-configured
              Celery app.
            - ``"arq"``: reserved for future ``baldur-framework[arq]`` extra; currently
              falls back to inline with a WARNING log.
    """
    global _init_done

    with _init_lock:
        if _init_done:
            logger.debug("baldur.init_already_completed")
            return

        # Ensure a BaldurRuntime exists in the current Context. Lazy-create
        # via get_runtime() so re-entrant init() under the same Context keeps
        # the same runtime identity, while reset_init_state() can drop it.
        from baldur.runtime import get_runtime

        get_runtime()

        _validate_startup_config(quarantine_callback=quarantine_callback)
        _validate_critical_secrets()
        _register_default_event_handlers()
        _init_bridge_instrumentation()
        _instrument_otel_if_enabled()
        _register_shutdown_handlers()
        _wire_registry_defaults()
        _validate_idempotency_cache_in_production()
        _install_idempotency_gate()
        _emit_tier_setting_warnings()
        ext_result = _run_pro_extensions()
        _warn_unknown_env_vars()
        _apply_audit_default_provider()
        _start_audit_pipeline_if_enabled()
        _start_dlq_outbox_if_enabled()
        _configure_error_budget_if_enabled()
        _register_metrics_provider_if_configured()
        _refresh_auto_replay_arming_gauge()
        _record_env_snapshot()
        _start_default_scheduler(task_backend=task_backend)
        _register_sql_statistics_if_available()
        _start_admin_server_if_enabled()
        start_background_workers()

        report = _build_startup_report(ext_result)
        logger.info("baldur.startup_report", **report)

        _init_done = True


def reset_init_state() -> None:
    """Reset init state — for test isolation only.

    Tests that need to re-run ``init()`` (e.g. to verify the entry-point
    hook discovery path) should call this from a fixture. The chain runs
    in this order so cleanup hooks see a live runtime:

    1. ``reset_storage_backend(cleanup=True)`` — closes WAL and drains the
       Redis connection pool held by the backend's cache adapter.
    2. Cache adapter pool drain — when a non-``"memory"`` cache default is
       active, retrieve the cached instance and call ``close()`` to drop
       its socket pool. Skipped when default is already ``"memory"``.
    3. Re-assert ``ProviderRegistry.cache.set_default("memory")`` so the
       module-load default is restored before the next ``init()`` call.
    3.5. For every other registry in
       :data:`_REGISTRIES_TO_WIRE`, call ``clear_instances()`` and
       ``set_default(wiring.reset_baseline)`` to mirror Step 2/3 across
       the Group A/B/C surface. The per-row ``reset_baseline`` equals the
       row's module-load default ("memory" for Group A/B and the hybrid
       event_journal row, "noop" for the probe-surface rows). Eliminates
       the "stale Redis instance after re-init" failure mode for
       integration tests that toggle env vars between ``init()`` calls.
    4. ``reset_runtime()`` — clears the active runtime so the next
       ``init()`` rebuilds it from scratch (and re-reads
       ``BALDUR_ENVIRONMENT`` / ``BALDUR_TEST_MODE``).
    """
    global _init_done, _init_not_called_cache_warned, _init_not_called_storage_warned
    with _init_lock:
        _init_done = False
        _init_not_called_cache_warned = False
        _init_not_called_storage_warned = False

        # Step 1 — drop the storage backend, draining WAL + Redis pool (D16).
        try:
            from baldur.adapters.resilient.backend import reset_storage_backend

            reset_storage_backend(cleanup=True)
        except Exception as e:
            logger.warning("baldur.reset_storage_backend_failed", error=str(e))

        # Step 2 — drain any non-"memory" cache adapter pool, then drop it
        # from the registry so the next init() reconstructs a fresh adapter.
        try:
            from baldur.factory.registry import ProviderRegistry

            current_default = ProviderRegistry.cache.get_default_name()
            if current_default and current_default != "memory":
                cached = ProviderRegistry.cache.get_cached_instances()
                instance = cached.get(current_default)
                if instance is not None and hasattr(instance, "close"):
                    try:
                        instance.close()
                    except Exception as e:
                        logger.warning(
                            "baldur.cache_adapter_close_failed",
                            adapter=current_default,
                            error=str(e),
                        )
                ProviderRegistry.cache.clear_instances()

            # Step 3 — restore the module-load default so the next init()
            # starts from the same baseline as a cold process.
            ProviderRegistry.cache.set_default("memory")
        except Exception as e:
            logger.warning("baldur.cache_default_reset_failed", error=str(e))

        # Step 3.5 — restore wired registry defaults to module-load baseline
        # (464 D13). Group A/B/C registries do not hold heavy connection pools
        # the way cache does, so a plain clear_instances() + set_default(...)
        # is sufficient. Skips cache (already handled by Step 2/3). Each row's
        # baseline is its own ``reset_baseline`` (570 D4): the probe-surface
        # PRIORITY_CHAIN rows restore to "noop" (their only registered
        # default — "memory" is not a registered provider there and would
        # break get() resolution), while a memory/redis/sql hybrid row
        # restores to "memory". The reset target equals each row's
        # module-load default, so the two stay symmetric by construction.
        try:
            from baldur.factory.registry import ProviderRegistry

            for wiring in _REGISTRIES_TO_WIRE:
                if wiring.registry_attr == "cache":
                    continue
                registry = getattr(ProviderRegistry, wiring.registry_attr, None)
                if registry is None:
                    continue
                registry.clear_instances()
                registry.set_default(wiring.reset_baseline)
        except Exception as e:
            logger.warning("baldur.wired_registry_reset_failed", error=str(e))

        # Step 4 — drop the runtime (eager-read env vars are re-read on rebuild).
        from baldur.runtime import reset_runtime

        reset_runtime()


# =============================================================================
# Step 1 — Validate startup config (relocated from apps.py:_validate_startup_config)
# =============================================================================


def _validate_startup_config(
    quarantine_callback: Callable[[Exception], None] | None = None,
) -> None:
    """Validate config with Safe Defaults on startup.

    Fail-Safe Default enforcement:
    - Non-fatal settings: Safe Default applied, continue
    - Fatal setting violation: invoke quarantine_callback if provided

    Cluster identity validation (namespace gate per quickstart fix):
    - Skipped when BaldurRuntime.is_test_mode is True OR namespace isolation is
      disabled (``namespace_enabled`` False — the default). Cluster identity
      exists to prevent cross-cluster namespace collisions, a risk that only
      exists under namespace key-prefixing; the zero-config single-process path
      has no collision surface, so it must not be forced to set
      BALDUR_CLUSTER_ID / BALDUR_NAMESPACE_REGION.
    - When namespaced, calls ``identity.validate(fail_fast=...)`` where
      ``fail_fast`` is read from BALDUR_FAIL_FAST (default True). On non-fatal
      failure (fail_fast=False returning False), explicitly flips
      ``set_quarantine_mode(True)`` and logs the violation. The factory body
      no longer performs this check — caller-controlled timing eliminates the
      xdist test-mode contract flakies that emerged when the env was read at
      first ``get_cluster_identity()`` invocation.
    """
    _validate_cluster_identity_if_namespaced()

    try:
        from baldur.core.safe_defaults import (
            ENABLE_QUARANTINE_ON_FATAL,
            FatalConfigError,
            validate_startup_config,
        )
        from baldur.settings.root import get_config

        try:
            changes = validate_startup_config(
                get_config(), log_changes=True, raise_on_fatal=False
            )
            if changes > 0:
                logger.info(
                    "baldur.startup_config_validation_applied",
                    changes=changes,
                )
            else:
                logger.debug("baldur.startup_config_validation_all")
        except FatalConfigError as e:
            if ENABLE_QUARANTINE_ON_FATAL and quarantine_callback is not None:
                quarantine_callback(e)
            else:
                logger.critical(
                    "baldur.fatal_config_error_quarantine",
                    error=e,
                )
    except ImportError:
        logger.debug("baldur.safe_defaults_unavailable")
    except Exception as e:
        logger.warning(
            "baldur.validate_startup_config_failed",
            error=e,
        )


def _validate_cluster_identity_if_namespaced() -> None:
    """Validate ClusterIdentity only when namespace isolation is enabled.

    Cluster identity (BALDUR_CLUSTER_ID + BALDUR_NAMESPACE_REGION) exists to
    prevent cross-cluster namespace collisions. That risk is real only when
    namespace key-prefixing is on (``namespace_enabled``) — the same condition
    ``settings/root.py`` uses to warn on a ``"default"`` cluster_id. The
    zero-config OSS path (single process, in-memory, no namespace) has no
    collision surface, so requiring those env vars there would break the
    documented zero-infrastructure quickstart. ``is_test_mode`` also skips
    (test isolation), preserving the decoupling of validation timing
    from factory invocation.
    """

    from baldur.runtime import get_runtime

    if get_runtime().is_test_mode:
        return

    try:
        from baldur.settings.namespace import get_namespace_settings
    except ImportError:
        logger.debug("baldur.namespace_settings_unavailable")
        return

    try:
        if not get_namespace_settings().namespace_enabled:
            return
    except Exception as e:
        # Settings unreadable → do not block startup on the namespace gate.
        logger.warning("baldur.namespace_settings_read_failed", error=e)
        return

    try:
        from baldur.core.cluster_identity import (
            get_cluster_identity,
            set_quarantine_mode,
        )
    except ImportError:
        logger.debug("baldur.cluster_identity_unavailable")
        return

    fail_fast = os.environ.get("BALDUR_FAIL_FAST", "true").lower() == "true"
    try:
        identity = get_cluster_identity()
        is_valid = identity.validate(fail_fast=fail_fast)
    except SystemExit:
        # fail_fast=True raised — let it propagate.
        raise
    except Exception as e:
        logger.warning("baldur.cluster_identity_validation_failed", error=e)
        return

    if not is_valid:
        set_quarantine_mode(True)
        logger.warning("quarantine_mode.activated")


def _validate_critical_secrets() -> None:
    """Abort startup in production when a CRITICAL secret is missing.

    Centralizes the secret gate that previously lived only in Django's
    ``apps.py``. Routing it through ``init()`` makes it fire on every framework
    adapter (Django / Flask / FastAPI / CLI), so a non-Django production deploy
    without ``audit_signing_key`` / ``encryption_key`` no longer boots and runs
    keyless. Placed right after ``_validate_startup_config`` so the abort is
    fail-fast — before the admin server, scheduler, or background workers start.

    Behavior:
    - Non-production: best-effort. ``validate_required_secrets`` logs the
      missing-secret counts and ``init()`` continues.
    - Production + a CRITICAL secret missing: ``validate_required_secrets``
      raises ``RuntimeError`` (under ``is_production()``); this function lets it
      propagate out of ``init()`` to abort boot.

    Non-``RuntimeError`` failures stay best-effort (logged, swallowed) so a
    transient settings-read error never blocks a non-production startup.
    """
    try:
        from baldur.settings.secrets import validate_required_secrets

        result = validate_required_secrets()

        critical_count = len(result.get("critical", []))
        warning_count = len(result.get("warning", []))

        if critical_count > 0:
            logger.error(
                "baldur.critical_secrets_configured_check",
                critical_count=critical_count,
            )
        elif warning_count > 0:
            logger.warning(
                "baldur.important_secrets_configured_check",
                warning_count=warning_count,
            )
        else:
            logger.info("baldur.all_secrets_validated_successfully")

    except RuntimeError as e:
        # Production CRITICAL secret missing -> re-raise to abort startup.
        # validate_required_secrets already logs per-secret ERROR/WARNING; this
        # adds one critical line with the aggregate error so operators get an
        # actionable signal at the boot-abort point.
        logger.critical(
            "baldur.secrets_validation_failed_resolution",
            error=e,
        )
        raise
    except Exception as e:
        # Other errors -> best-effort, continue startup.
        logger.warning(
            "baldur.secrets_validation_failed",
            error=e,
        )


def _validate_idempotency_cache_in_production() -> None:
    """Refuse to start in production when idempotency would silently degrade.

    Defense-in-depth on top of ``_wire_redis_registry``'s production cache
    enforcement (the cache row in ``_REGISTRIES_TO_WIRE`` already raises a
    ``ConfigurationError`` when ``BALDUR_REDIS_URL`` is unset in production).
    This validator catches the residual case where the cache default ends
    up as ``None`` or ``"memory"`` after wiring — either because
    ``_wire_registry_defaults`` was customized to skip the cache row, or
    because a downstream step reset the default between wiring and
    idempotency first use.

    Invariant enforced:
        production AND ``BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK=false``
        AND cache default ∈ {None, "memory"} ⇒ raise ``ConfigurationError``.

    The check is intentionally conservative: it does **not** semantically
    validate distributedness of non-``"memory"`` adapter names — arbitrary
    adapter validation is out of scope.

    No-op when:
        - ``BaldurRuntime.is_test_mode`` is True (test isolation),
        - runtime is non-production,
        - ``IdempotencySettings.allow_inmemory_fallback`` is True (operator
          explicitly accepted in-process-only semantics).
    """
    from baldur.runtime import get_runtime

    runtime = get_runtime()
    if runtime.is_test_mode or not runtime.is_production:
        return

    from baldur.settings.idempotency import get_idempotency_settings

    if get_idempotency_settings().allow_inmemory_fallback:
        return

    from baldur.core.exceptions import ConfigurationError
    from baldur.factory.registry import ProviderRegistry

    default_name = ProviderRegistry.cache.get_default_name()
    if default_name in (None, "memory"):
        raise ConfigurationError(
            "Idempotency requires a distributed cache adapter in production "
            "(BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK=false). "
            f"Current cache default: {default_name!r}. "
            "Set BALDUR_REDIS_URL=redis://<host>:<port>/<db>, or set "
            "BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK=true to accept "
            "in-process-only semantics."
        )


def _install_idempotency_gate() -> None:
    """Configure the unified idempotency-gate singleton with a real cache.

    The bare ``get_idempotency_gate()`` factory builds
    ``IdempotencyGate(cache=None)``, whose ``check_and_acquire`` returns
    CONTINUE unconditionally — a silent dedup no-op. This installer configures
    the singleton with the registry-resolved cache at ``init()`` so its direct
    consumers (``replay_service``, PRO governance ``IdempotencyCheck``) perform
    real deduplication instead of proceeding unconditionally.

    Mirrors :func:`_install_resilient_storage_backend`: a singleton (not a
    registry default) configured eagerly from the resolved cache. Resolution
    flows through the shared :func:`resolve_cache_via_registry`
    (``layer="singleton"``) so the singleton joins the single-source resolver
    rather than reading ``ProviderRegistry`` directly.

    ``raise_on_prod_no_toggle=False``: the validator above is the single-source
    prod fail-fast for the common bad cases (default ∈ {None, "memory"}), so it
    aborts before install runs. The near-impossible residuals split by failure
    mode: *no adapter registered* (``AdapterNotFoundError``) degrades to the
    in-process fallback with a WARN + Prometheus counter rather than raising a
    second, differently-worded error. A *registered-but-unconstructable* default
    raises during ``get_cache()`` construction — that exception is not an
    ``AdapterNotFoundError``, so the resolver does not catch it and it propagates
    out of this installer, aborting ``init()``. Fail-fast on a genuinely broken
    adapter is the correct outcome (not a silent degrade).

    No-op when ``BaldurRuntime.is_test_mode`` is True — symmetric with
    :func:`_validate_idempotency_cache_in_production`. The full unit suite runs
    ``BALDUR_TEST_MODE=true``; installing unconditionally would silently flip
    the singleton to a real cache for every ``init()``-calling test.

    Placement after the validator is load-bearing: a prod-misconfig aborts at
    the validator (its nicer message wins) before install runs, and prod-good
    has a guaranteed distributed cache for the resolver to return.
    """
    from baldur.runtime import get_runtime

    runtime = get_runtime()
    if runtime.is_test_mode:
        return

    from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
    from baldur.core.idempotency_gate import (
        IdempotencyGate,
        configure_idempotency_gate,
    )
    from baldur.services.idempotency._cache_resolver import (
        resolve_cache_via_registry,
    )

    cache = resolve_cache_via_registry(
        layer="singleton",
        fallback_cache=InMemoryCacheAdapter(key_prefix="idempotency_gate:"),
        raise_on_prod_no_toggle=False,
    )
    configure_idempotency_gate(IdempotencyGate(cache=cache))
    logger.debug("idempotency_gate.installed", adapter=type(cache).__name__)


# =============================================================================
# Step 2 — Register default event handlers (relocated from apps.py)
# =============================================================================


def _register_default_event_handlers() -> None:
    """Register default event bus handlers (~30+) at startup.

    Equivalent to ``_register_default_event_handlers`` in ``apps.py``.
    Framework-agnostic — only imports baldur.services.event_bus.
    """
    try:
        from baldur.services.event_bus.bus.default_handlers import (
            register_default_handlers,
        )

        register_default_handlers()
    except Exception as e:
        logger.warning(
            "baldur.register_default_handlers_failed",
            error=e,
        )


# =============================================================================
# Step 2.5 — Initialize bridge adapters (impl 451 — D8)
# =============================================================================


def _init_bridge_instrumentation() -> None:
    """Apply Level-1 bridge instrumentation when enabled in BridgeSettings.

    Currently wires the tenacity bridge: monkey-patches
    ``tenacity.Retrying.__init__`` so every Retrying instance created after
    ``init()`` returns gets Baldur's ``RETRY_EXHAUSTED`` event emission for
    free. Gated by ``BridgeSettings.tenacity_enabled`` AND
    ``BridgeSettings.tenacity_instrument`` — both must be True.

    Placement: AFTER ``_register_default_event_handlers`` so emitted retry
    events reach a configured handler chain. BEFORE
    ``_register_shutdown_handlers`` so shutdown routines can rely on the
    bridge being installed.

    Failures (including missing tenacity extra) log at DEBUG/WARNING and
    return — bridge init must never abort startup.
    """
    try:
        from baldur.settings.bridge import get_bridge_settings

        settings = get_bridge_settings()
    except Exception as e:
        logger.debug("bridge.settings_unavailable", error=e)
        return

    if not settings.tenacity_enabled:
        logger.debug("bridge.tenacity_disabled")
        return
    if not settings.tenacity_instrument:
        logger.debug("bridge.tenacity_instrument_disabled")
        return

    try:
        from baldur.bridges.tenacity import instrument_tenacity

        instrument_tenacity()
    except ImportError as e:
        logger.debug("bridge.tenacity_unavailable", error=e)
    except Exception as e:
        logger.warning("bridge.tenacity_instrument_failed", error=e)


# =============================================================================
# Step 2.6 — Initialize OTel SDK + framework-agnostic instrumentors (593 D1)
# =============================================================================


# 593 D6 — shared autostart hatch for both the framework-agnostic instrumentors
# (here) and the Django ready() request instrumentor.
def _otel_autostart_enabled() -> bool:
    """Return False when ``BALDUR_OTEL_AUTOSTART`` opts out (test/operator hatch).

    Mirrors ``BALDUR_SCHEDULER_AUTOSTART`` / ``BALDUR_ADMIN_AUTOSTART`` /
    ``BALDUR_META_WATCHDOG_AUTOSTART``: default ``"1"`` (enabled);
    ``"0"`` / ``"false"`` / ``"no"`` skips so a test that sets
    ``BALDUR_OBSERVABILITY_PROFILE=otel_collector`` does not monkey-patch the
    global ``requests`` / ``logging`` modules or ``settings.MIDDLEWARE``.
    """
    autostart = os.environ.get("BALDUR_OTEL_AUTOSTART", "1").strip().lower()
    return autostart not in {"0", "false", "no"}


# 593 D1/D4 — framework-agnostic OTel instrumentation, co-located with the
# tenacity bridge wiring. instrument_django is wired separately in
# BaldurConfig.ready() because DjangoInstrumentor mutates settings.MIDDLEWARE
# and must run before WSGIHandler.load_middleware().
def _instrument_otel_if_enabled() -> None:
    """Initialize the OTel SDK and apply the framework-agnostic instrumentors.

    Covers the outbound ``requests`` library, Celery tasks, and Python logging
    — the three instrumentors with no Django-handler load-order dependency, so
    they run wherever ``init()`` runs (Django dev-server/worker, Flask
    app-factory, FastAPI lifespan, plain-Python CLI). The Django request
    instrumentor (``instrument_django``) has a middleware-load-order constraint
    and is wired separately in ``BaldurConfig.ready()``.

    Placement is adjacent to ``_init_bridge_instrumentation`` so all third-party
    instrumentation wiring is co-located. Exact ordering within ``init()`` is
    not correctness-critical — no ``init()`` step emits OTel spans before this
    runs.

    Gating: short-circuits on the ``BALDUR_OTEL_AUTOSTART`` hatch and on
    ``ObservabilitySettings.effective_otel_enabled`` (driven by
    ``BALDUR_OBSERVABILITY_PROFILE``, default ``auto``), so the SDK is not
    initialized when OTel is off. ``initialize_opentelemetry`` runs
    before the instrumentors so the composite TraceContext+Baggage propagator is
    live when ``instrument_requests`` injects baggage. Each ``instrument_*`` is
    idempotent (``state.*_instrumented`` guard) and fail-soft (swallows
    ImportError/Exception), so this never aborts startup.
    """
    if not _otel_autostart_enabled():
        logger.debug("otel.autostart_disabled_env")
        return

    try:
        from baldur.observability import (
            initialize_opentelemetry,
            instrument_celery,
            instrument_logging,
            instrument_requests,
        )
        from baldur.settings.observability import get_observability_settings
    except ImportError as e:
        logger.debug("otel.instrumentation_unavailable", error=e)
        return

    try:
        if not get_observability_settings().effective_otel_enabled:
            logger.debug("otel.instrumentation_disabled")
            return

        # SDK init first: TracerProvider + exporter + composite
        # TraceContext+Baggage propagator must be live before the outbound
        # instrumentor injects baggage onto egress headers.
        initialize_opentelemetry()

        instrument_requests()
        instrument_celery()
        instrument_logging()
    except Exception as e:
        logger.warning("otel.instrumentation_failed", error=e)


# =============================================================================
# Step 3 — Register shutdown handlers (relocated from apps.py)
# =============================================================================


def _register_shutdown_handlers() -> None:  # noqa: C901, PLR0912, PLR0915
    """Register all shutdown handlers with the central coordinator.

    Equivalent to ``_register_shutdown_handlers`` in ``apps.py``. Each handler
    factory is wrapped in a try/except ImportError to allow optional
    dependencies (e.g. private-distribution modules) to be missing.
    """
    try:
        from baldur.core.shutdown_coordinator import (
            RequestTracker,
            get_shutdown_coordinator,
        )

        # 471 D12 — populate coordinator._tracker unconditionally so
        # non-gunicorn deployments (runserver, hypercorn, plain Python, CLI)
        # have a tracker available for RequestTrackingMiddleware. The
        # singleton-getter is no-op on second call (only sets _tracker when
        # currently None per the shutdown_coordinator default), so gunicorn's
        # post_worker_init still works whether it runs before or after this.
        coordinator = get_shutdown_coordinator(request_tracker=RequestTracker())

        factories: list[Callable[[], Any]] = []

        # OSS handler factories — these stay as direct try-imports;
        # G15 fitness function ignores OSS->OSS imports.
        # 599 D5 — the multiregion heartbeat integration moved to the private
        # distribution; it self-registers into
        # ProviderRegistry.shutdown_integrations via the module list below
        # (flag check lives inside the moved factory).
        try:
            from baldur.services.event_bus.redis_bus import (
                integrate_with_shutdown_coordinator as bus_integrate,
            )

            factories.append(bus_integrate)
        except ImportError:
            pass
        try:
            from baldur.services.event_bus.bus import (
                integrate_dispatch_with_shutdown_coordinator as dispatch_integrate,
            )

            factories.append(dispatch_integrate)
        except ImportError:
            pass
        # 527 D6 — master-`enabled` shutdown integrations are wiring-gated.
        # Sub-flag wiring stays inside each module's own code paths.
        try:
            from baldur.settings.leader_election import get_leader_election_settings

            if get_leader_election_settings().enabled:
                from baldur.coordination.shutdown_integration import (
                    integrate_with_shutdown_coordinator as leader_integrate,
                )

                factories.append(leader_integrate)
        except ImportError:
            pass
        try:
            from baldur.services.precomputed_cache.shutdown import (
                integrate_with_shutdown_coordinator as cache_integrate,
            )

            factories.append(cache_integrate)
        except ImportError:
            pass
        # 599 D12 — the ml_models shutdown integration moved to the private
        # distribution; it self-registers into
        # ProviderRegistry.shutdown_integrations via the module list below
        # (flag check lives inside the moved factory).
        try:
            from baldur.scaling.shutdown import (
                integrate_hpa_exporter_with_shutdown_coordinator,
                integrate_rate_controller_with_shutdown_coordinator,
            )

            factories.append(integrate_rate_controller_with_shutdown_coordinator)
            factories.append(integrate_hpa_exporter_with_shutdown_coordinator)
        except ImportError:
            pass
        try:
            from baldur.settings.meta_watchdog import get_meta_watchdog_settings

            if get_meta_watchdog_settings().enabled:
                from baldur.meta.shutdown import (
                    integrate_with_shutdown_coordinator as watchdog_integrate,
                )

                factories.append(watchdog_integrate)
        except ImportError:
            pass

        # 516 D4 part 2 — collapsed PRO shutdown integrations. Each module
        # imported below registers its factory with
        # ProviderRegistry.shutdown_integrations at import time; we then
        # iterate the registry to collect factories in registration order
        # (chaos → bulkhead → hedging → auto_tuning → saga → emergency_mode).
        # The bootstrap import-order is the authoritative ordering source —
        # GenericProviderRegistry uses an insertion-ordered dict.
        import importlib

        _PRO_SHUTDOWN_MODULES = [
            "baldur_pro.services.chaos.scheduler.shutdown",
            "baldur_pro.services.bulkhead.shutdown",
            "baldur_pro.services.hedging.shutdown",
            "baldur_pro.services.auto_tuning.shutdown",
            "baldur_pro.services.saga.shutdown",
            "baldur_pro.services.canary.shutdown",
            "baldur_pro.services.emergency_mode.shutdown_handler",
            # 599 D12 — relocated Dormant cluster (ships in the same
            # baldur-pro wheel; absent on OSS-only installs -> skipped).
            "baldur_dormant.services.ml_models.shutdown",
            # 599 D5 — relocated multiregion heartbeat (Layer 1 peer
            # notification); integrate factory is flag-gated internally.
            "baldur_dormant.multiregion.heartbeat",
        ]
        for module_path in _PRO_SHUTDOWN_MODULES:
            try:
                importlib.import_module(module_path)
            except ImportError:
                continue
            except Exception as exc:
                logger.debug(
                    "baldur.pro_shutdown_module_import_failed",
                    module=module_path,
                    error=exc,
                )

        from baldur.factory.registry import ProviderRegistry

        for name in ProviderRegistry.shutdown_integrations.list_providers():
            try:
                factories.append(
                    ProviderRegistry.shutdown_integrations.get_provider(name)
                )
            except Exception as exc:
                logger.debug(
                    "baldur.shutdown_integration_resolve_failed",
                    name=name,
                    error=exc,
                )

        for factory in factories:
            try:
                handler = factory()
                if handler:
                    coordinator.register_handler(handler)
            except Exception as exc:
                logger.debug(
                    "baldur.shutdown_handler_registration_failed",
                    factory=getattr(factory, "__module__", "unknown"),
                    error=exc,
                )

        # AuditShutdownHandler
        try:
            from baldur.audit.shutdown_handler import AuditShutdownHandler

            coordinator.register_handler(AuditShutdownHandler())
        except Exception as exc:
            logger.debug(
                "baldur.shutdown_handler_registration_failed",
                handler="AuditShutdownHandler",
                error=exc,
            )

        # Wire OS signal handlers to the coordinator (597 D2/D3/D4).
        # Disposition-sensitive: a host server's handler (e.g. uvicorn's
        # handle_exit, installed before init() in the lifespan startup
        # path) is chained — drain initiated first, host handler second —
        # and a SIG_DFL disposition arms a post-drain re-raise so a
        # standalone process terminates instead of swallowing SIGTERM/
        # SIGINT. SIG_IGN skips registration (ignore intent preserved).
        # Self-skips under gunicorn (master OR worker) via
        # is_under_gunicorn() — SERVER_SOFTWARE-based, so the guard fires
        # throughout the entire gunicorn lifecycle including worker
        # pre-post_worker_init; gunicorn manages worker signal lifecycle
        # via worker_int and the chained SIGTERM handler installed by
        # baldur.adapters.gunicorn.hooks.
        try:
            coordinator.register_signals()
        except Exception as exc:
            logger.warning(
                "baldur.register_signals_failed",
                error=exc,
            )

        # 471 D2/D5/D6/D7 — Layer-2 silent-dormancy guard. When running under
        # gunicorn but the user has not wired baldur.adapters.gunicorn.hooks,
        # neither register_signals (self-skipped via is_under_gunicorn) nor the
        # hooks (never imported) connect SIGTERM to the coordinator. Bootstrap
        # is the only path guaranteed to run in any deployment, so it owns
        # this check. The Timer delay outlasts post_worker_init's hooks-side
        # import path so the WARNING does not double-fire when wiring is
        # correct but slow.
        _schedule_gunicorn_hooks_check()

    except Exception as e:
        logger.warning(
            "baldur.register_shutdown_handlers_failed",
            error=e,
        )


def _schedule_gunicorn_hooks_check() -> None:
    """Schedule a one-shot WARNING when running under gunicorn without hooks.

    Detection signals:

    - ``is_under_gunicorn()`` (SERVER_SOFTWARE-based) — phase-independent,
      reliable throughout the gunicorn lifecycle.
    - ``'baldur.adapters.gunicorn.hooks' not in sys.modules`` — both
      supported wiring patterns (``gunicorn -c <hooks-path>`` and re-export
      in user's ``gunicorn.conf.py``) load the hooks module before workers
      start. A module-level marker set inside ``post_worker_init`` would
      false-positive in master under ``--preload``; the sys.modules check
      is master/worker-symmetric.

    Failures (settings unavailable, Timer creation fails) silently fall
    through — bootstrap must never block startup on a diagnostic.
    """
    try:
        import sys
        import threading

        from baldur.core.process_utils import is_under_gunicorn
        from baldur.settings.recovery_shutdown import (
            get_recovery_shutdown_settings,
        )

        delay = get_recovery_shutdown_settings().hooks_check_delay_seconds

        def _check_hooks_installed() -> None:
            try:
                if not is_under_gunicorn():
                    return
                if "baldur.adapters.gunicorn.hooks" in sys.modules:
                    return
                logger.warning(
                    "baldur.gunicorn_hooks_not_installed",
                    hint=(
                        "Running under gunicorn but "
                        "baldur.adapters.gunicorn.hooks was not imported. "
                        "Wire via 'gunicorn -c <path-to-hooks-module>' or "
                        "re-export the hooks in your gunicorn.conf.py. See "
                        "docs/runbooks/gunicorn-graceful-shutdown.md."
                    ),
                )
            except Exception as exc:
                logger.debug(
                    "baldur.gunicorn_hooks_check_failed",
                    error=exc,
                )

        timer = threading.Timer(delay, _check_hooks_installed)
        timer.daemon = True
        timer.start()
    except Exception as exc:
        logger.debug("baldur.gunicorn_hooks_check_schedule_failed", error=exc)


# =============================================================================
# Step 3.6 — Emit operator WARNINGs for Deferred/Dormant env-var overrides (527 D9)
# =============================================================================


# Truthy literals recognised for `BALDUR_SUPPRESS_TIER_WARNING` and for
# tier-setting env-var overrides. Mirrors Pydantic v2's default truthy set.
_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on", "y", "t"})


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in _TRUTHY_ENV_VALUES


def _locate_v1_launch_manifest() -> Any | None:
    """Return the manifest file Path if reachable, else None.

    The manifest is package-native (``baldur._data/V1_LAUNCH_MANIFEST.yaml``),
    so it ships with ``src/baldur`` in both editable and wheel installs and
    resolves via ``importlib.resources`` uniformly. Operators may override the
    path with ``BALDUR_TIER_MANIFEST_PATH``. Returns None when no candidate is
    reachable — the tier-warning feature then silently no-ops.
    """
    from importlib.resources import files as resource_files
    from pathlib import Path

    override = os.getenv("BALDUR_TIER_MANIFEST_PATH")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        return None
    try:
        resource = resource_files("baldur._data").joinpath("V1_LAUNCH_MANIFEST.yaml")
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    return Path(str(resource)) if resource.is_file() else None


def _emit_tier_setting_warnings() -> None:  # noqa: C901
    """Emit one WARNING per process per non-launch-set tier env-var override.

    Reads the ``V1_LAUNCH_MANIFEST.yaml`` file and walks every entry whose
    tier is held out of the v1.0 launch set. For each entry whose
    operator-facing ``env_var`` is set to a truthy value, emits one structlog
    WARNING. Suppressed wholesale by ``BALDUR_SUPPRESS_TIER_WARNING=true``.

    Failure modes (yaml unavailable, manifest unreachable, malformed entry)
    silently degrade — startup must never fail on a diagnostic.
    """
    if _env_truthy(os.getenv("BALDUR_SUPPRESS_TIER_WARNING")):
        return
    manifest_path = _locate_v1_launch_manifest()
    if manifest_path is None:
        return
    try:
        import yaml
    except ImportError:
        return
    try:
        with manifest_path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.debug("baldur.tier_manifest_load_failed", error=str(exc))
        return
    entries = data.get("entries")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tier = entry.get("tier")
        if tier not in ("Deferred", "Dormant"):
            continue
        env_var = entry.get("env_var")
        if not env_var:
            continue
        if not _env_truthy(os.getenv(env_var)):
            continue
        logger.warning(
            "baldur.tier_setting_overridden",
            setting_path=(
                f"{entry.get('module', '?')}.{entry.get('class', '?')}."
                f"{entry.get('field', '?')}"
            ),
            tier=tier,
            env_var=env_var,
            version_target="post-v1.0",
        )


# =============================================================================
# Step 3.7 — Warn on unknown BALDUR_* env vars (576 D2/D3/D5)
# =============================================================================


def _warn_unknown_env_vars() -> None:
    """Emit one WARNING per ``BALDUR_*`` env var that maps to no known config sink.

    Compares the ``BALDUR_*`` keys present in ``os.environ`` against the union of
    every Pydantic ``(env_prefix, field)`` AND the catalogued direct-read
    registry (``baldur.settings.introspection``). A key matching neither is a
    likely typo (``BALDUR_DLQ_MAX_SIE``) or a removed var — the operator set it
    believing it took effect, but ``extra="ignore"`` silently dropped it. Emits
    one ``baldur.unknown_env_var_detected`` WARNING carrying the var **name**
    (and the nearest known var name via ``difflib``) — **never the value**, so a
    typo'd secret-bearing var (``BALDUR_SECRET_KYE``) never leaks its plaintext
    to the log sink (precedent: ``_emit_tier_setting_warnings`` logs name-only).

    Placed after ``_run_pro_extensions()`` so ``baldur_pro`` settings classes and
    the pro direct-read registrations are visible — a best-effort reduction of
    pro false positives (not load-bearing; warn-only is fail-safe either way).

    The ``os.environ`` filter is case-insensitive (``key.upper().startswith``),
    matching Pydantic's ``case_sensitive=False`` consumption and additionally
    catching a lower-case typo Pydantic would silently drop.

    Suppressed wholesale by ``BALDUR_SUPPRESS_UNKNOWN_ENV_WARNING=true``.
    Fail-safe: any internal failure degrades to DEBUG and never fails boot.
    """
    if _env_truthy(os.getenv("BALDUR_SUPPRESS_UNKNOWN_ENV_WARNING")):
        return
    try:
        import difflib

        from baldur.settings.introspection import (
            build_prefix_index,
            is_known_env_var,
            known_env_var_names,
        )

        index = build_prefix_index()
        known_names = known_env_var_names(index)
        for key in list(os.environ):
            if not key.upper().startswith("BALDUR_"):
                continue
            if is_known_env_var(key, index):
                continue
            suggestion = difflib.get_close_matches(key.upper(), known_names, n=1)
            logger.warning(
                "baldur.unknown_env_var_detected",
                env_var=key,
                nearest=suggestion[0] if suggestion else None,
            )
    except Exception as exc:
        logger.debug("baldur.unknown_env_var_scan_failed", error=str(exc))


# =============================================================================
# Step 3.5 — Wire registry defaults (463 D2 / 464 D9-D11)
# =============================================================================


# 515 D6 — probe functions consumed by the PRIORITY_CHAIN rows in
# ``_REGISTRIES_TO_WIRE``. Defined before the tuple so the tuple literal
# can capture references.


def _django_databases_configured() -> bool:
    """Return True iff Django is importable and ``settings.DATABASES`` is non-empty.

    Env-guard on ``DJANGO_SETTINGS_MODULE`` first (mirroring
    ``settings/system_control.py:_django_settings_fallback``), then lazy
    ``from django.conf import settings`` inside ``try/except Exception``.
    The env-guard avoids importing Django at all in non-Django runtimes
    (Flask/FastAPI/CLI), which is more conservative than probing
    ``settings.configured``. Any exception during the probe (ImportError,
    ImproperlyConfigured, attribute error) is treated as "Django not
    usable" and the function returns False.
    """
    # 464 R4 mitigation — treat any probe failure as "Django not usable".
    if not os.environ.get("DJANGO_SETTINGS_MODULE"):
        return False
    try:
        from django.conf import settings as django_settings

        return bool(getattr(django_settings, "DATABASES", None))
    except Exception as e:
        logger.debug("baldur.django_databases_probe_failed", error=str(e))
        return False


def _postgres_dsn_configured() -> bool:
    """Return True iff ``BALDUR_SQL_DSN`` or any ``BALDUR_POSTGRES_*`` env is set.

    The DB-API SQL adapter family reads its connection details
    via ``baldur.settings.sql.resolve_dsn``, which honors
    ``BALDUR_SQL_DSN`` first and falls back to ``BALDUR_POSTGRES_*``
    components. The probe mirrors that precedence — a non-empty value at
    either layer is sufficient to consider the SQL adapter usable.
    """
    if (os.environ.get("BALDUR_SQL_DSN") or "").strip():
        return True
    return any(
        (os.environ.get(name) or "").strip()
        for name in (
            "BALDUR_POSTGRES_HOST",
            "BALDUR_POSTGRES_PORT",
            "BALDUR_POSTGRES_DATABASE",
            "BALDUR_POSTGRES_USER",
        )
    )


def _redis_url_configured() -> bool:
    """Return True iff ``BALDUR_REDIS_URL`` is set and non-empty (after strip).

    Mirrors the inline ``redis_set`` computation in
    :func:`_wire_registry_defaults` (Group A reads ``os.environ`` directly
    rather than ``RedisSettings.url`` because the settings default
    ``redis://localhost:6379/0`` would mask the unset case). Consumed by
    the ``event_journal_repo`` PRIORITY_CHAIN row as the first probe in
    its ``redis > sql > memory`` chain.
    """
    redis_url = os.environ.get("BALDUR_REDIS_URL")
    return bool(redis_url and redis_url.strip())


# 464 D9 — ordered table of registries that ``init()`` rewires from the
# module-load "memory" baseline to an environment-aware default. Cache is
# row 1 (carried forward from 463); subsequent rows are the Group A/B
# registries that init()'s framework-agnostic wiring enumerates.
#
# Order matters: Group A rows come first so the ResilientStorageBackend
# special case (executed once, between phases) sees a consistent
# Group A verdict.
_REGISTRIES_TO_WIRE: tuple[_RegistryWiring, ...] = (
    # Group A — Redis-backed (cache + 4 stores).
    _RegistryWiring(_BackendKind.REDIS, "cache", "redis"),
    _RegistryWiring(_BackendKind.REDIS, "config_history_store", "redis"),
    _RegistryWiring(_BackendKind.REDIS, "canary_rollout_store", "redis"),
    _RegistryWiring(_BackendKind.REDIS, "chaos_experiment_store", "redis"),
    _RegistryWiring(_BackendKind.REDIS, "cross_cluster_store", "redis"),
    # rate_limit_storage uniquely has a Django ORM "database" adapter
    # alongside redis/memory — D11 wires the cross-row fallback so a
    # Django-only non-production deployment lands on the database
    # adapter instead of warning + memory. Note: the fallback is
    # observable only in non-production. In production with Redis
    # unset, the cache row (row 1) raises ConfigurationError before
    # this row is reached, so the production fail-loud invariant for
    # the cluster as a whole is unchanged.
    _RegistryWiring(
        _BackendKind.REDIS,
        "rate_limit_storage",
        "redis",
        fallback_target="database",
    ),
    # Group B — SQL/Django-backed (464 D7 reclassifies security_repo here;
    # 570 D5 adds postmortem_repo, structurally identical — memory/django/sql).
    _RegistryWiring(_BackendKind.SQL_DJANGO, "recovery_session_repo", "django"),
    _RegistryWiring(_BackendKind.SQL_DJANGO, "security_repo", "django"),
    _RegistryWiring(_BackendKind.SQL_DJANGO, "postmortem_repo", "django"),
    # Group C — probe-surface (473 D1, generalized in 515 D6). Each row
    # declares a priority chain of provider candidates; first one that
    # probes True and is registered wins. Production fail-loud is
    # intentionally absent because FastAPI/Flask/CLI runtimes legitimately
    # ship without Django, and SQLite-only deployments legitimately ship
    # without Postgres.
    _RegistryWiring(
        _BackendKind.PRIORITY_CHAIN,
        "database_health",
        target_name="",
        priority_chain=(
            ("django", _django_databases_configured),
            ("sql", _postgres_dsn_configured),
            ("noop", lambda: True),
        ),
        env_override="BALDUR_DATABASE_HEALTH_PROVIDER",
        reset_baseline="noop",
    ),
    _RegistryWiring(
        _BackendKind.PRIORITY_CHAIN,
        "pg_admin",
        target_name="",
        priority_chain=(
            ("django", _django_databases_configured),
            ("sql", _postgres_dsn_configured),
            ("noop", lambda: True),
        ),
        env_override="BALDUR_PG_ADMIN_PROVIDER",
        reset_baseline="noop",
    ),
    _RegistryWiring(
        _BackendKind.PRIORITY_CHAIN,
        "pool_info",
        target_name="",
        priority_chain=(
            ("django", _django_databases_configured),
            ("noop", lambda: True),
        ),
        env_override="BALDUR_POOL_INFO_PROVIDER",
        reset_baseline="noop",
    ),
    # 570 D1 — event_journal_repo is a memory/redis/sql hybrid (not a
    # probe-surface registry): PRIORITY_CHAIN is the only kind that keeps
    # all three adapters reachable while preserving the public
    # BALDUR_EVENT_JOURNAL_BACKEND operator knob (mapped onto env_override).
    # No production fail-loud is needed here — the cache row (Group A, row
    # 1) already raises ConfigurationError whenever BALDUR_REDIS_URL is
    # unset in production, so by the time this row is reached Redis is
    # guaranteed present and the "redis" probe matches. reset_baseline is
    # "memory" (NOT "noop"): event_journal registers no noop adapter, so
    # the probe-surface "noop" baseline would set a non-registered default.
    _RegistryWiring(
        _BackendKind.PRIORITY_CHAIN,
        "event_journal_repo",
        target_name="",
        priority_chain=(
            ("redis", _redis_url_configured),
            ("sql", _postgres_dsn_configured),
            ("memory", lambda: True),
        ),
        env_override="BALDUR_EVENT_JOURNAL_BACKEND",
        reset_baseline="memory",
    ),
)


def _wire_redis_registry(
    registry: GenericProviderRegistry,
    target_name: str,
    fallback_target: str | None,
    redis_set: bool,
    django_set: bool,
    runtime: BaldurRuntime,
) -> None:
    """Apply the trigger matrix to a single Redis-backed row.

    ``runtime.is_test_mode`` is handled by the caller's early return;
    this helper only sees prod/non-prod paths.
    """
    from baldur.core.exceptions import ConfigurationError

    adapter_type = registry._adapter_type

    if redis_set:
        registry.set_default(target_name)
        logger.info(
            "baldur.registry_default_wired",
            registry=adapter_type,
            backend=target_name,
            source="BALDUR_REDIS_URL",
        )
        return

    if fallback_target and django_set:
        registry.set_default(fallback_target)
        logger.info(
            "baldur.registry_default_wired",
            registry=adapter_type,
            backend=fallback_target,
            source="django_databases",
        )
        return

    if runtime.is_production:
        signals = (
            "BALDUR_REDIS_URL or Django DATABASES"
            if fallback_target
            else "BALDUR_REDIS_URL"
        )
        raise ConfigurationError(
            f"{signals} is not set in production for "
            f"ProviderRegistry.{adapter_type}. The framework cannot "
            "silently fall back to per-worker memory storage without "
            "breaking distributed guarantees. Set BALDUR_REDIS_URL=redis://"
            "<host>:<port>/<db>"
            + (" (or configure Django DATABASES)" if fallback_target else "")
            + ", or run with BALDUR_TEST_MODE=true for a deliberate "
            "memory-only mode."
        )

    # non-production + no signal — WARNING + memory fallback.
    logger.warning(
        "baldur.registry_memory_fallback",
        registry=adapter_type,
        reason="redis_url_unset",
        hint=(
            "Set BALDUR_REDIS_URL for distributed state. "
            "Set BALDUR_TEST_MODE=true to suppress this warning."
        ),
    )
    registry.set_default("memory")


def _wire_sql_django_registry(
    registry: GenericProviderRegistry,
    sql_target: str,
    django_target: str,
    sql_set: bool,
    django_set: bool,
    runtime: BaldurRuntime,
) -> None:
    """Apply the trigger matrix to a single SQL/Django-backed row.

    Priority: ``sql > django > memory``. ``runtime.is_test_mode``
    is handled by the caller's early return.
    """
    # D5: backend priority sql > django > memory.
    from baldur.core.exceptions import ConfigurationError

    adapter_type = registry._adapter_type

    if sql_set:
        registry.set_default(sql_target)
        logger.info(
            "baldur.registry_default_wired",
            registry=adapter_type,
            backend=sql_target,
            source="BALDUR_SQL_DSN",
        )
        return

    if django_set:
        registry.set_default(django_target)
        logger.info(
            "baldur.registry_default_wired",
            registry=adapter_type,
            backend=django_target,
            source="django_databases",
        )
        return

    if runtime.is_production:
        raise ConfigurationError(
            f"Neither BALDUR_SQL_DSN nor Django DATABASES is configured in "
            f"production for ProviderRegistry.{adapter_type}. The framework "
            "cannot silently archive audit-relevant state to per-worker "
            "memory. Set BALDUR_SQL_DSN=postgresql://... or configure "
            "Django DATABASES, or run with BALDUR_TEST_MODE=true for a "
            "deliberate memory-only mode."
        )

    # non-production + no signal — WARNING + memory fallback.
    logger.warning(
        "baldur.registry_memory_fallback",
        registry=adapter_type,
        reason="sql_django_unset",
        hint=(
            "Set BALDUR_SQL_DSN or DJANGO_SETTINGS_MODULE+DATABASES "
            "for durable archival. Set BALDUR_TEST_MODE=true to "
            "suppress this warning."
        ),
    )
    registry.set_default("memory")


def _wire_priority_chain_registry(
    registry: GenericProviderRegistry,
    wiring: _RegistryWiring,
    runtime: BaldurRuntime,
) -> None:
    """Apply the priority-chain trigger matrix to a probe-surface row.

    Probe-surface registries (``database_health``, ``pg_admin``,
    ``pool_info``) cannot safely fail loud in production because
    non-Django runtimes (FastAPI/Flask/CLI) legitimately ship without the
    Django adapter and SQLite-only deployments ship without Postgres.

    Resolution order:

    1. If ``env_override`` is set and the env var names a registered
       provider, that provider wins. An invalid value is logged at
       WARNING and resolution continues with the priority chain.
    2. Each ``(name, probe)`` pair in ``priority_chain`` is consulted in
       declared order. The first pair whose probe returns True AND whose
       provider name is registered wins.
    3. When ≥ 2 probes match, an ``info``-level log records the winner
       and all candidates so dual-config states (e.g. Django installed
       AND ``BALDUR_SQL_DSN`` set) surface visibly without a debugger.
       Dual-config is a legitimate state, hence ``info`` rather than
       WARNING — only an invalid ``env_override`` warrants a warning.

    ``runtime.is_test_mode`` is handled by the caller's early return.
    """
    adapter_type = registry._adapter_type

    if wiring.env_override:
        env_val = (os.environ.get(wiring.env_override) or "").strip()
        if env_val:
            if registry.has_provider(env_val):
                registry.set_default(env_val)
                logger.info(
                    "baldur.registry_default_wired",
                    registry=adapter_type,
                    backend=env_val,
                    source=wiring.env_override,
                )
                return
            logger.warning(
                "baldur.registry_env_override_invalid",
                registry=adapter_type,
                env_var=wiring.env_override,
                value=env_val,
            )

    matched = [
        name
        for name, probe in wiring.priority_chain
        if probe() and registry.has_provider(name)
    ]
    if not matched:
        # Probe-surface fallback is intentional, not degradation. DEBUG
        # (not WARNING/INFO) avoids noise on legitimate non-Django
        # deployments while still giving operators a diagnostic
        # breadcrumb. The chain's final ("noop", lambda: True) row
        # should normally pre-empt this branch — reaching here means
        # "noop" itself is not registered.
        logger.debug(
            "baldur.registry_priority_chain_unmatched",
            registry=adapter_type,
        )
        return

    winner = matched[0]
    registry.set_default(winner)
    if len(matched) > 1:
        logger.info(
            "baldur.registry_priority_chain_resolved",
            registry=adapter_type,
            resolved=winner,
            candidates=matched,
            env_override=wiring.env_override,
        )
    else:
        logger.debug(
            "baldur.registry_default_wired",
            registry=adapter_type,
            backend=winner,
            source="priority_chain",
        )


def _install_resilient_storage_backend(runtime: BaldurRuntime) -> None:
    """Eagerly construct ResilientStorageBackend and install via configure_*.

    Mirrors the production WAL fail-fast: after construction,
    ``backend._wal_initialized=False`` in production raises
    ``ConfigurationError`` so the bad volume mount surfaces at deploy time.
    Non-production silently logs and proceeds (dev laptop tolerates the
    WAL failure).
    """
    from baldur.adapters.resilient.backend import (
        ResilientStorageBackend,
        configure_storage_backend,
    )
    from baldur.core.exceptions import ConfigurationError
    from baldur.settings.redis import get_redis_settings
    from baldur.settings.resilient_storage import (
        ResilientStorageSettings,
        get_resilient_storage_settings,
    )

    redis_url = get_redis_settings().url
    base_settings = get_resilient_storage_settings()
    # Carry over only the fields an operator actually supplied. Dumping every
    # field would mark them all as explicitly set, and the backend infers
    # "did the operator choose this directory?" from ``model_fields_set`` —
    # a full dump makes every default look operator-chosen.
    operator_supplied = {
        name: value
        for name, value in base_settings.model_dump().items()
        if name in base_settings.model_fields_set
    }
    settings = ResilientStorageSettings(**{**operator_supplied, "redis_url": redis_url})
    backend = ResilientStorageBackend(settings=settings)
    configure_storage_backend(backend)

    if runtime.is_production and not backend._wal_honors_configured_dir:
        raise ConfigurationError(
            f"WAL initialization did not honor {backend.config.wal_dir} in "
            "production"
            + (
                f" (fell back to {backend._wal.wal_dir})"
                if backend._wal_on_fallback_dir and backend._wal is not None
                else ""
            )
            + ". Check container volume mount or directory permissions — "
            "the WAL-First Write Protocol cannot be honored without a "
            "writable wal_dir. To prioritize availability during an "
            "infrastructure incident, set "
            "BALDUR_RESILIENT_STORAGE_WAL_DIR to any writable path; an "
            "ephemeral one is accepted, because choosing it explicitly "
            "makes the durability trade-off yours rather than ours."
        )

    logger.info(
        "baldur.resilient_storage_backend_installed",
        redis_url_source="BALDUR_REDIS_URL",
        wal_initialized=backend._wal_initialized,
        wal_on_fallback_dir=backend._wal_on_fallback_dir,
    )


def _wire_registry_defaults() -> None:
    """Install environment-aware registry defaults at startup.

    Implements the environment-aware registry-default wiring across the
    14 registries
    enumerated in :data:`_REGISTRIES_TO_WIRE` (cache + 5 Group A + 4
    Group B + 4 PRIORITY_CHAIN). Placed before :func:`_run_pro_extensions`
    so PRO entry-point hooks see the post-wiring registry state and can
    override when needed.

    Phase order:

    1. **Legacy alias rejection** — first line. Hard-fails
       ``BALDUR_ENVIRONMENT`` set to one of the four known legacy aliases
       so a deployment carrying ``BALDUR_ENVIRONMENT=prod`` from
       pre-converged precedents fails CrashLoop instead of silently
       regressing the three security gates.
    2. **Test mode early return** — ``BALDUR_TEST_MODE=true`` skips all
       wiring silently (no WARNING, no eager backend construction).
    3. **Group A phase** — read ``BALDUR_REDIS_URL`` once, dispatch each
       Group A row through :func:`_wire_redis_registry`. The
       ``rate_limit_storage`` row carries ``fallback_target="database"``,
       allowing a Django-only **non-production** deployment
       to land on the database adapter instead of warning + memory. In
       production the fallback is unreachable: the cache row (row 1)
       raises :class:`ConfigurationError` whenever Redis is unset, so
       the cluster-wide fail-loud invariant is unchanged.
    4. **ResilientStorageBackend special case** — when Redis is selected
       (production with URL set, or non-production with URL set), eagerly
       construct the backend and install via
       :func:`configure_storage_backend`. WAL directory creation runs at
       construction time; production WAL init failure raises
       :class:`ConfigurationError`.
    5. **Group B phase** — read SQL/Django signals, dispatch each Group B
       row through :func:`_wire_sql_django_registry`. Group A and Group
       B verdicts run independently — a deploy that satisfies one but
       not the other still fails loudly at the deficient phase.
    6. **PRIORITY_CHAIN phase** — the probe-surface registries
       (``database_health``, ``pg_admin``, ``pool_info``) plus the
       ``event_journal_repo`` memory/redis/sql hybrid dispatch
       through :func:`_wire_priority_chain_registry`. Each row carries a
       data-driven priority chain consulted in declared order; an
       optional env override (e.g. ``BALDUR_PG_ADMIN_PROVIDER``,
       ``BALDUR_EVENT_JOURNAL_BACKEND``) lets operators force a specific
       provider. No production fail-loud in this phase: non-Django
       runtimes legitimately ship without the probe-surface adapters, and
       SQLite-only deployments ship without Postgres. ``event_journal``
       is not silently memory-degraded in production despite the absent
       fail-loud — the cache row (row 1) already raised
       :class:`ConfigurationError` if Redis was unset, so by this phase
       Redis is guaranteed present and the ``redis`` probe matches.

    URL-unset detection reads ``os.environ`` directly because
    ``RedisSettings.url`` defaults to ``redis://localhost:6379/0`` and
    would mask the unset case. ``BALDUR_SQL_DSN`` is read directly for
    consistency with the Redis path.
    """
    from baldur.core.exceptions import ConfigurationError
    from baldur.factory.registry import ProviderRegistry
    from baldur.runtime import get_runtime

    runtime = get_runtime()

    # D15 / 463 R1 — reject known legacy aliases at startup so a stale
    # alias cannot silently regress the three security gates.
    raw_env = os.environ.get("BALDUR_ENVIRONMENT", "").strip().lower()
    if raw_env in _REJECTED_LEGACY_ALIASES:
        raise ConfigurationError(
            f"BALDUR_ENVIRONMENT={raw_env!r} is a known legacy alias of "
            "'production'. Use 'production' for prod, or "
            "'staging'/'development' otherwise."
        )

    # Test mode wins: silent memory, no WARNING, no eager backend
    # construction. Local-dev offline workflow uses BALDUR_TEST_MODE=true
    # to silence non-prod WARNINGs without introducing a new env var.
    if runtime.is_test_mode:
        logger.debug("baldur.wire_registry_defaults_skipped_test_mode")
        return

    # Read all signals once. The Group A and Group B phases consult these
    # in lockstep across all rows so one structured-log entry per row
    # carries the full verdict.
    redis_url_raw = os.environ.get("BALDUR_REDIS_URL")
    redis_set = bool(redis_url_raw and redis_url_raw.strip())

    sql_dsn_raw = os.environ.get("BALDUR_SQL_DSN", "")
    sql_set = bool(sql_dsn_raw.strip())

    django_set = _django_databases_configured()

    # Phase 1 — Group A (Redis-backed). Cache is row 1; ConfigurationError
    # from any row aborts the loop and propagates out of _wire_*.
    for wiring in _REGISTRIES_TO_WIRE:
        if wiring.backend_kind is not _BackendKind.REDIS:
            continue
        registry = getattr(ProviderRegistry, wiring.registry_attr)
        _wire_redis_registry(
            registry,
            wiring.target_name,
            wiring.fallback_target,
            redis_set,
            django_set,
            runtime,
        )

    # Phase 1.5 — ResilientStorageBackend special case. Constructed once,
    # only when Redis was selected (so non-prod+URL-unset skips the eager
    # backend, matching #463 behavior). Storage backend is a singleton,
    # not a registry default, so it lives outside the table.
    if redis_set:
        _install_resilient_storage_backend(runtime)

    # Phase 2 — Group B (SQL/Django-backed). Independent verdict — a
    # deploy with Redis set but no SQL/Django signal raises here even
    # though Group A succeeded.
    for wiring in _REGISTRIES_TO_WIRE:
        if wiring.backend_kind is not _BackendKind.SQL_DJANGO:
            continue
        registry = getattr(ProviderRegistry, wiring.registry_attr)
        _wire_sql_django_registry(
            registry,
            sql_target="sql",
            django_target=wiring.target_name,
            sql_set=sql_set,
            django_set=django_set,
            runtime=runtime,
        )

    # Phase 3 — PRIORITY_CHAIN (probe-surface 473 D1 / 515 D6 +
    # event_journal hybrid 570 D1). No production fail-loud in this phase:
    # non-Django runtimes legitimately ship without the probe-surface
    # adapters, SQLite-only deployments ship without Postgres, and
    # event_journal's production Redis guarantee is supplied transitively
    # by the cache row (Phase 1) which already raised if Redis was unset.
    for wiring in _REGISTRIES_TO_WIRE:
        if wiring.backend_kind is not _BackendKind.PRIORITY_CHAIN:
            continue
        registry = getattr(ProviderRegistry, wiring.registry_attr)
        _wire_priority_chain_registry(registry, wiring, runtime)


# =============================================================================
# Step 4 — Discover PRO entry-point hooks (D4 / D4-G)
# =============================================================================


@dataclass
class ExtensionResult:
    """Result of PRO entry-point hook discovery and execution."""

    found: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _run_pro_extensions() -> ExtensionResult:
    """Discover and invoke ``baldur.bootstrap_hooks`` entry points.

    PRO package authors register a single hook function in their
    pyproject.toml that flips audit settings to enabled and sets the
    ``ProviderRegistry.audit`` default to ``"file_hashchain"``.

    Hook failures are logged at WARNING and do NOT abort init().
    """
    result = ExtensionResult()
    try:
        from importlib.metadata import entry_points
    except Exception:
        return result

    try:
        hooks = entry_points(group="baldur.bootstrap_hooks")
    except Exception:
        return result

    for hook in hooks:
        result.found.append(hook.name)
        try:
            fn = hook.load()
            fn()
            result.executed.append(hook.name)
            logger.debug("baldur.pro_extension_invoked", name=hook.name)
        except Exception as exc:
            result.failed.append(hook.name)
            logger.warning(
                "baldur.pro_extension_failed",
                name=hook.name,
                error=exc,
            )

    return result


_FEATURE_SCAN: list[tuple[str, str, str, str]] = [
    # (display_name, settings_module, getter_function, flag_field)
    ("audit", "baldur.settings.audit", "get_audit_settings", "enabled"),
    (
        "error_budget_gate",
        "baldur.settings.error_budget_gate",
        "get_error_budget_gate_settings",
        "enabled",
    ),
    (
        "compliance",
        "baldur.settings.compliance",
        "get_compliance_settings",
        "enabled",
    ),
    (
        "governance",
        "baldur.settings.governance",
        "get_governance_settings",
        "break_glass_enabled",
    ),
    ("chaos", "baldur.settings.chaos", "get_chaos_settings", "enabled"),
    (
        "correlation_engine",
        "baldur.settings.correlation_engine",
        "get_correlation_engine_settings",
        "enabled",
    ),
]


def _build_startup_report(ext_result: ExtensionResult) -> dict[str, Any]:
    """Build unified startup capability report.

    Combines entry-point hook results (installation) with settings flag scan
    (configuration) into a single structured dict for INFO logging.
    """
    report: dict[str, Any] = {
        "extensions": {
            "found": ext_result.found,
            "executed": ext_result.executed,
            "failed": ext_result.failed,
        },
    }

    features: dict[str, bool] = {}
    for name, module_path, getter_name, flag in _FEATURE_SCAN:
        try:
            mod = importlib.import_module(module_path)
            getter = getattr(mod, getter_name)
            settings = getter()
            features[name] = getattr(settings, flag, False)
        except Exception:
            features[name] = False
    report["features"] = features

    # DLQ capture backing tier (pro when the PRO DLQService is registered under
    # ACTIVE entitlement, else the OSS capture backing). Operator visibility for
    # "does dlq=True durably capture on this install?" — it always does now.
    try:
        from baldur.services.dlq_capture import resolve_dlq_backing_tier

        report["dlq_capture_backing"] = resolve_dlq_backing_tier()
    except Exception:
        report["dlq_capture_backing"] = "oss"

    # Durability directory resolutions. Tells an operator which storage
    # surfaces are on their configured directory and which fell back — the
    # boot-time counterpart to the primitive's one-time warning. Lazily
    # constructed surfaces resolve after the report and appear in logs only.
    try:
        from baldur.utils.fs import get_writable_dir_resolutions

        storage_dirs = get_writable_dir_resolutions()
        report["storage_dirs"] = storage_dirs
        _warn_on_audit_durability_split(storage_dirs)
    except Exception:
        report["storage_dirs"] = {}

    return report


def _warn_on_audit_durability_split(
    storage_dirs: dict[str, dict[str, str]],
) -> None:
    """Warn when the audit checkpoint and audit WAL differ in durability.

    The two directories resolve independently, so an operator who sets only
    one of the audit path variables can end up with a durable WAL and a
    fallen-back checkpoint. The lost checkpoint resets the resume point, and
    the surviving WAL files are re-read as duplicate audit events. Detection
    only: forcing a shared fate would undo purpose independence, and
    duplicates are the correct fail direction for an audit trail.
    """
    checkpoint = _find_resolution(storage_dirs, _AUDIT_CHECKPOINT_PURPOSE)
    if checkpoint is None:
        return

    for wal in _find_resolutions_by_env(storage_dirs, _AUDIT_WAL_ENV_VAR):
        if checkpoint["status"] == wal["status"]:
            continue

        logger.warning(
            "storage.audit_dir_durability_split",
            checkpoint_status=checkpoint["status"],
            checkpoint_path=checkpoint["path"],
            checkpoint_env=_AUDIT_CHECKPOINT_ENV_VAR,
            wal_status=wal["status"],
            wal_path=wal["path"],
            wal_env=_AUDIT_WAL_ENV_VAR,
        )


def _find_resolution(
    storage_dirs: dict[str, dict[str, str]],
    purpose: str,
) -> dict[str, str] | None:
    """Find a resolution registry entry by its purpose."""
    prefix = f"{purpose}-"
    for key, entry in storage_dirs.items():
        if key.startswith(prefix):
            return entry
    return None


def _find_resolutions_by_env(
    storage_dirs: dict[str, dict[str, str]],
    env_var: str,
) -> list[dict[str, str]]:
    """Find resolution entries whose surface reads ``env_var``.

    Keys off what each surface recorded, so the variable this report names
    as the remedy is one that surface actually reads.
    """
    return [
        entry for entry in storage_dirs.values() if entry.get("override_env") == env_var
    ]


# =============================================================================
# Step 5 — Apply audit default provider (D11 / D15 — null when disabled)
# =============================================================================


def _apply_audit_default_provider() -> None:
    """Apply the audit default provider per AuditSettings.enabled.

    OSS path:  ``enabled=False`` → leaves the module-level default which
                is already ``"null"`` (set by ``registry.py`` at module
                load), so this is a no-op.
    PRO path:  the entry-point hook already called
               ``ProviderRegistry.audit.set_default("file_hashchain")``,
               so this function only logs the resolved name as
               confirmation.
    """
    try:
        from baldur.factory import ProviderRegistry
        from baldur.settings.audit import get_audit_settings

        settings = get_audit_settings()
        current = ProviderRegistry.audit.get_default_name()
        if not settings.enabled and current != "null":
            # Defense-in-depth: re-assert null even if a hook tried to override.
            ProviderRegistry.audit.set_default("null")
            current = "null"
        logger.debug("audit.default_provider_set", provider=current)
    except Exception as e:
        logger.debug("audit.default_provider_set_failed", error=e)


# =============================================================================
# Step 6 — Start audit pipeline (Pipeline A: WAL + SyncWorker)
# =============================================================================


def _start_audit_pipeline_if_enabled() -> None:
    """Start the async audit pipeline (WAL + SyncWorker) if enabled.

    Gated by ``AuditSettings.enabled`` AND wrapped in
    ``try/except ImportError`` for build-time exclusion of the
    ``audit/`` package.
    """
    try:
        from baldur.settings.audit import get_audit_settings

        if not get_audit_settings().enabled:
            logger.debug("audit.startup_skipped", reason="disabled")
            return
    except Exception as e:
        logger.debug("audit.startup_skipped", reason="settings_unavailable", error=e)
        return

    try:
        from baldur.audit.async_audit_lifecycle import startup_async_audit_system

        ok = startup_async_audit_system()
        if ok:
            logger.info("audit.startup_completed")
        else:
            logger.debug("audit.startup_skipped", reason="already_started")
    except ImportError as e:
        logger.debug("audit.startup_skipped", reason="audit_unavailable", error=e)
    except Exception as e:
        logger.warning("audit.startup_failed", error=e)


# =============================================================================
# Step 6b — Start DLQ outbox worker (impl doc 486 D7)
# =============================================================================


def _refresh_auto_replay_arming_gauge() -> None:
    """Prime the on-recovery auto-replay armed gauge at startup (non-I/O links).

    The worker ping (broker I/O) is skipped so init stays non-blocking — the
    gauge reflects the non-I/O prerequisites (enabled, celery importable, map
    configured, handler registered). The full worker-inclusive evaluation runs
    on each arming-probe invocation. Fail-open.
    """
    try:
        from baldur.services.replay_service.arming import refresh_armed_gauge

        refresh_armed_gauge(check_worker=False)
    except Exception as e:
        logger.debug("baldur.auto_replay_arming_gauge_skipped", error=str(e))


def _start_dlq_outbox_if_enabled() -> None:
    """Start the DLQ outbox worker if enabled.

    Gated by ``DLQOutboxSettings.enabled`` (BALDUR_DLQ_OUTBOX_ENABLED).
    Idempotent — re-entry is a silent no-op via the module-level lock in
    ``setup_dlq_outbox``.
    """
    try:
        from baldur.settings.dlq_outbox import get_dlq_outbox_settings

        if not get_dlq_outbox_settings().enabled:
            logger.debug("dlq_outbox.startup_skipped", reason="disabled")
            return
    except Exception as e:
        logger.debug(
            "dlq_outbox.startup_skipped", reason="settings_unavailable", error=e
        )
        return

    # PRO durable wrapper (D1, G8) — must install BEFORE setup_dlq_outbox
    # so the worker captures the wrapped sync_writer.
    try:
        from baldur_pro.services.dlq_outbox import setup_durable_outbox_if_enabled

        setup_durable_outbox_if_enabled()
    except ImportError:
        pass

    try:
        from baldur.services.dlq_outbox import setup_dlq_outbox

        ok = setup_dlq_outbox()
        if ok:
            logger.info("dlq_outbox.startup_completed")
        else:
            logger.debug("dlq_outbox.startup_skipped", reason="already_started")
    except ImportError as e:
        logger.debug(
            "dlq_outbox.startup_skipped", reason="dlq_outbox_unavailable", error=e
        )
    except Exception as e:
        logger.warning("dlq_outbox.startup_failed", error=e)


# =============================================================================
# Step 6c — Wire real stats into the Error Budget service (impl doc 622 D1)
# =============================================================================


def _configure_error_budget_if_enabled() -> None:
    """Wire real DLQ stats into the Error Budget service when enabled.

    Flip-to-activate: gated by ``ErrorBudgetSettings.enabled``
    (BALDUR_ERROR_BUDGET_ENABLED). When OFF (the v1.0 default) the service stays
    unwired and its live-status consumers honestly skip — no fake-healthy verdict
    on simulation data. When ON, the service is configured with the DLQ
    windowed-inflow stats source so consumers read real data.

    Operator precedence: a pre-init manual ``configure_error_budget_service(...)``
    is never clobbered — skip auto-wiring when the service is already wired.
    """
    try:
        from baldur.settings.error_budget import get_error_budget_settings

        if not get_error_budget_settings().enabled:
            logger.debug("error_budget.startup_skipped", reason="disabled")
            return
    except Exception as e:
        logger.debug(
            "error_budget.startup_skipped", reason="settings_unavailable", error=e
        )
        return

    try:
        from baldur_pro.services.error_budget import (
            configure_error_budget_service,
            is_error_budget_service_wired,
        )
        from baldur_pro.services.error_budget.stats_sources import (
            dlq_failed_operation_stats,
        )
    except ImportError as e:
        # OSS install (no baldur_pro) — Error Budget is a PRO feature.
        logger.debug("error_budget.startup_skipped", reason="pro_unavailable", error=e)
        return

    if is_error_budget_service_wired():
        logger.debug("error_budget.startup_skipped", reason="already_wired")
        return

    # Retention <-> window disclosure (impl 622 D1 / Risk R3a): a DLQ retention
    # shorter than the SLO window undercounts the window tail (non-conservative).
    # Defaults align (30d == 30d), so the warning fires only on operator-shortened
    # retention. Best-effort — a check failure must not block wiring.
    try:
        from baldur.settings.dlq import get_dlq_settings
        from baldur.slo import SLOConfig

        retention_days = get_dlq_settings().retention_days
        slo = SLOConfig.default_config().get_slo("availability")
        slo_window_days = slo.window_days if slo else 30
        if retention_days < slo_window_days:
            logger.warning(
                "error_budget.retention_window_mismatch",
                retention_days=retention_days,
                slo_window_days=slo_window_days,
            )
    except Exception as e:
        logger.debug("error_budget.retention_check_skipped", error=e)

    configure_error_budget_service(
        get_failed_operation_stats=dlq_failed_operation_stats
    )
    logger.info("error_budget.stats_wired", source="dlq")


# =============================================================================
# Step 6d — Register the remote-Prometheus time-series metrics provider
# =============================================================================


def _register_metrics_provider_if_configured() -> None:
    """Register the remote-Prometheus metrics provider when a URL is configured.

    Gated by ``PrometheusSettings.url`` (BALDUR_PROMETHEUS_URL). Unset = off: no
    provider is registered and manual DI via ``set_metrics_provider`` stays
    available. There is deliberately no separate enable flag — an unset URL is
    the off switch.

    Fail-open: a malformed url/headers raises a pydantic ``ValidationError`` at
    settings load; it is surfaced non-silently as a WARNING and boot continues.
    This is an optional feature (unset URL = off), so it uses the same
    catch-warn-skip posture as the other settings-gated optional steps rather
    than the fail-loud posture reserved for required connection adapters.

    Registration alone changes no promotion behaviour — the live-evaluation gate
    short-circuits on its own enable flag before the provider is consulted.
    """
    try:
        from baldur.settings.prometheus import get_prometheus_settings

        settings = get_prometheus_settings()
    except Exception as e:
        logger.warning("prometheus_provider.registration_failed", error=e)
        return

    if not settings.url:
        logger.debug("prometheus_provider.registration_skipped", reason="url_unset")
        return

    try:
        from baldur.services.config_shadow.providers.prometheus import (
            setup_prometheus_metrics_provider,
        )

        setup_prometheus_metrics_provider(settings=settings)
        logger.debug("prometheus_provider.registered", url=settings.url)
    except Exception as e:
        logger.warning("prometheus_provider.registration_failed", error=e)


# =============================================================================
# Step 7 — Record env snapshot (D21)
# =============================================================================


def _record_env_snapshot() -> None:
    """Record BALDUR_* environment variable snapshot for audit trail.

    Framework-agnostic — runs in Django, FastAPI, Flask alike.
    Routes through ``ProviderRegistry.get_audit_adapter()`` so OSS gets
    ``NullAuditLogAdapter`` (silent), PRO gets
    ``HashChainFileAuditLogAdapter`` (compliance-grade record).

    Best-effort: any failure is logged at WARNING and init() continues.
    """
    try:
        from baldur.audit.env_snapshot import log_env_snapshot_to_audit

        log_env_snapshot_to_audit()
        logger.debug("baldur.env_snapshot_recorded")
    except ImportError:
        logger.debug("baldur.env_snapshot_unavailable")
    except Exception as e:
        logger.warning("baldur.env_snapshot_failed", error=e)


# =============================================================================
# Step 8 — Start default scheduler (429 Part 6 / D6)
# =============================================================================


# Intervals chosen to match pre-existing Celery Beat cadence across the codebase.
# Keep in seconds so the ScheduledJob dataclass signature is satisfied directly.
_DEFAULT_SCHEDULED_JOBS: tuple[tuple[str, str, str, float], ...] = (
    # (job_name, python_module_path, python_callable, interval_seconds)
    (
        "daily_report",
        "baldur.tasks.daily_report",
        "generate_daily_autonomous_report",
        24 * 60 * 60.0,
    ),
    (
        "sla_drift",
        "baldur.tasks.drift_detection",
        "_synthetic_sla_drift_check",
        60 * 60.0,
    ),
    (
        "cb_recovery",
        "baldur.services",
        "_synthetic_cb_recovery_check",
        60.0,
    ),
    (
        "archive_old_dlq_entries",
        "baldur.tasks.cleanup_tasks",
        "archive_old_dlq_entries",
        24 * 60 * 60.0,
    ),
    (
        "cleanup_expired_config",
        "baldur.tasks.cleanup_tasks",
        "cleanup_expired_config",
        60 * 60.0,
    ),
    # 665 D2 — apply due DELAYED/GRACEFUL config changes (30s). Resolved via a
    # celery-free synthetic callable (NOT baldur.tasks.config_apply, which
    # hard-imports celery and is unimportable on a celery-less inline install).
    (
        "config_apply",
        "baldur.services",
        "_synthetic_config_apply",
        30.0,
    ),
)


# Default jobs whose capability is PRO-only — registered only when the PRO
# distribution is installed. The scheduled DLQ retention lifecycle is a PRO
# capability, so on an OSS-only install this job could only fail on cadence.
_PRO_GATED_JOBS: tuple[str, ...] = ("archive_old_dlq_entries",)


def _tier_resolved_scheduled_jobs() -> tuple[tuple[str, str, str, float], ...]:
    """Return the default scheduled jobs runnable on the installed tier.

    Registration-time filter over ``_DEFAULT_SCHEDULED_JOBS`` — the tuple itself
    stays the full job contract; only what actually gets registered narrows.
    """
    from baldur.utils.tier import is_pro_installed

    if is_pro_installed():
        return _DEFAULT_SCHEDULED_JOBS

    for job_name in _PRO_GATED_JOBS:
        logger.debug("scheduler.pro_gated_job_skipped", job=job_name)
    return tuple(
        job for job in _DEFAULT_SCHEDULED_JOBS if job[0] not in _PRO_GATED_JOBS
    )


# Maps a scheduled job name → Celery @shared_task name, for task_backend="celery".
# When a job has no Celery counterpart, the scheduler falls back to inline with
# a debug log.
_CELERY_TASK_NAMES: dict[str, str] = {
    "daily_report": "baldur.tasks.daily_report.generate_daily_autonomous_report",
    "sla_drift": "baldur.celery_tasks.check_sla_drift",
    "cb_recovery": "baldur.celery_tasks.check_circuit_breaker_recovery",
    "config_apply": "baldur.apply_pending_config_changes",
}


def _resolve_job_callable(module_path: str, attr: str) -> Callable[[], Any] | None:
    """Resolve a scheduled-job target by dotted path.

    Synthetic callables (CB recovery, SLA drift) are built here rather than
    imported because the underlying services/detectors require DI wiring the
    plain Celery tasks do at call time.

    Returns None when the module or attribute is missing so the caller can
    skip registration without crashing init().
    """
    # Synthetic callables — composed from existing services, no module import.
    if attr == "_synthetic_cb_recovery_check":
        return _build_cb_recovery_callable()
    if attr == "_synthetic_sla_drift_check":
        return _build_sla_drift_callable()
    if attr == "_synthetic_config_apply":
        return _build_config_apply_callable()

    try:
        mod = importlib.import_module(module_path)
    except ImportError as e:
        logger.debug("scheduler.job_module_missing", module=module_path, error=e)
        return None
    target = getattr(mod, attr, None)
    if target is None:
        logger.debug("scheduler.job_attr_missing", module=module_path, attr=attr)
        return None
    return target


def _build_cb_recovery_callable() -> Callable[[], Any]:
    """Return a zero-arg callable that triggers a CB recovery transition check.

    Bypasses the Celery-bound ``check_circuit_breaker_recovery(self)`` so the
    inline scheduler does not need a Celery ``self`` instance.
    """

    def _tick() -> dict[str, Any]:
        from baldur.services import get_circuit_breaker_service

        service = get_circuit_breaker_service()
        return service.check_recovery_transitions()

    _tick.__name__ = "cb_recovery_tick"
    return _tick


def _build_sla_drift_callable() -> Callable[[], Any]:
    """Return a zero-arg callable that runs a full SLA drift check.

    Composes ``SLADriftDetector`` with the same DI helpers the Celery task
    uses (``_get_sla_thresholds``, ``_get_failed_operations_factory``,
    ``_record_sla_breach``). Returns an empty no-op dict if any dependency
    is unreachable — we never crash the scheduler thread on DI failure.
    """

    def _tick() -> dict[str, Any]:
        try:
            from baldur.celery_tasks.drift_detection_tasks import (
                _get_failed_operations_factory,
                _get_sla_thresholds,
                _record_sla_breach,
            )
            from baldur.tasks.drift_detection import SLADriftDetector
        except ImportError as e:
            logger.debug("scheduler.sla_drift_unavailable", error=e)
            return {"success": False, "error": "sla_drift_deps_missing"}

        detector = SLADriftDetector(
            get_sla_thresholds=_get_sla_thresholds,
            get_failed_operations=_get_failed_operations_factory(),
            record_sla_breach=_record_sla_breach,
        )
        return detector.check_drift()

    _tick.__name__ = "sla_drift_tick"
    return _tick


def _build_config_apply_callable() -> Callable[[], Any]:
    """Return a zero-arg callable that applies due pending config changes.

    Composes ``ConfigApplyService.apply_pending_changes`` directly, bypassing
    the Celery-bound ``apply_pending_config_changes(self)`` task. Crucially it
    does NOT import ``baldur.tasks.config_apply`` (that module hard-imports
    celery, an optional extra, so it is unimportable on a celery-less inline
    install — the exact deployment this synthetic callable serves). The service
    layer already performs Emergency-Mode governance pre-checks and PRO-absent
    guards, so this delegates only — identical in shape to the cb_recovery /
    sla_drift synthetics.
    """

    def _tick() -> dict[str, Any]:
        from baldur.services.execution_services import get_config_apply_service

        service = get_config_apply_service()
        return service.apply_pending_changes()

    _tick.__name__ = "config_apply_tick"
    return _tick


def _wrap_with_context(func: Callable[[], Any]) -> Callable[[], Any]:
    """Propagate contextvars (ActorContext, OTEL baggage) into the worker thread.

    LeaderScheduler runs jobs on a worker thread, so without
    ``contextvars.copy_context()`` the leader init-thread's actor /
    baggage state is lost at the scheduler boundary.
    """
    # D6 addendum — copy_context() preserves actor / OTEL baggage across
    # the scheduler worker-thread boundary.
    import contextvars

    ctx = contextvars.copy_context()

    def _run() -> Any:
        return ctx.run(func)

    _run.__name__ = getattr(func, "__name__", "_scheduled_job")
    return _run


def _build_celery_delegator(job_name: str) -> Callable[[], Any] | None:
    """Return a zero-arg callable that enqueues the Celery task for ``job_name``.

    Resolution uses Celery's task registry so module import order is
    irrelevant. Returns None when Celery or the specific task is unavailable.
    """
    task_name = _CELERY_TASK_NAMES.get(job_name)
    if not task_name:
        return None
    try:
        from celery import current_app
    except ImportError:
        logger.debug("scheduler.celery_unavailable", job=job_name)
        return None

    def _enqueue() -> None:
        try:
            task = current_app.tasks.get(task_name)
        except Exception as e:
            logger.warning(
                "scheduler.celery_task_lookup_failed",
                job=job_name,
                task=task_name,
                error=e,
            )
            return
        if task is None:
            logger.warning(
                "scheduler.celery_task_not_registered",
                job=job_name,
                task=task_name,
            )
            return
        task.delay()

    _enqueue.__name__ = f"{job_name}_celery_delegate"
    return _enqueue


def _resolve_scheduler_elector(resource_name: str) -> Any | None:
    """Pick the inline scheduler's elector based on leader-election settings.

    Returns a ``LocalFileLeaderElector`` when distributed leader election is
    disabled (the single-host default — it elects exactly one process per host
    so scheduled jobs run out-of-box), or ``None`` when enabled so
    ``get_leader_scheduler`` resolves the distributed elector (Redis/K8s) via
    the factory. The DLQ-consumer path is untouched (it never passes an
    elector).
    """
    try:
        from baldur.coordination.config import get_leader_election_settings

        le_enabled = get_leader_election_settings().enabled
    except Exception as e:
        logger.debug("scheduler.leader_election_settings_unavailable", error=e)
        le_enabled = False
    if le_enabled:
        return None
    from baldur.coordination.local_file_elector import LocalFileLeaderElector

    return LocalFileLeaderElector(resource_name)


def _start_default_scheduler(task_backend: str = "inline") -> None:  # noqa: C901
    """Register default scheduled jobs on ``get_leader_scheduler()`` and start it.

    All failures are logged at WARNING and init() continues — a broken
    scheduler must never block startup of the rest of the framework.

    Escape hatch: ``BALDUR_SCHEDULER_AUTOSTART=0`` skips registration and
    start entirely. Intended for unit tests that call ``init()`` but do not
    want a live Leader Election / thread running in the test process.
    """

    autostart = os.environ.get("BALDUR_SCHEDULER_AUTOSTART", "1").strip().lower()
    if autostart in {"0", "false", "no"}:
        logger.debug("scheduler.autostart_disabled_env")
        return

    if task_backend not in {"inline", "celery", "arq"}:
        logger.warning(
            "scheduler.unknown_task_backend",
            task_backend=task_backend,
            fallback="inline",
        )
        task_backend = "inline"

    if task_backend == "arq":
        logger.warning(
            "scheduler.arq_backend_not_implemented",
            fallback="inline",
            reason="baldur-framework[arq] extra not yet shipped",
        )
        task_backend = "inline"

    try:
        from baldur.coordination.scheduler import (
            DEFAULT_SCHEDULER_RESOURCE,
            get_leader_scheduler,
        )

        scheduler = get_leader_scheduler(
            DEFAULT_SCHEDULER_RESOURCE,
            elector=_resolve_scheduler_elector(DEFAULT_SCHEDULER_RESOURCE),
        )
    except Exception as e:
        logger.warning("scheduler.unavailable", error=e)
        return

    registered = 0
    for job_name, module_path, attr, interval in _tier_resolved_scheduled_jobs():
        func: Callable[[], Any] | None
        if task_backend == "celery":
            func = _build_celery_delegator(job_name)
            if func is None:
                # Fall back to inline resolution when Celery task not available.
                func = _resolve_job_callable(module_path, attr)
        else:
            func = _resolve_job_callable(module_path, attr)

        if func is None:
            continue
        try:
            scheduler.add_job(
                name=job_name,
                func=_wrap_with_context(func),
                interval_seconds=interval,
            )
            registered += 1
        except Exception as e:
            logger.warning("scheduler.job_registration_failed", job=job_name, error=e)

    if registered == 0:
        logger.debug("scheduler.no_jobs_registered")
        return

    try:
        scheduler.start()
        logger.info(
            "scheduler.default_jobs_started",
            job_count=registered,
            task_backend=task_backend,
        )
    except Exception as e:
        logger.warning("scheduler.start_failed", error=e)


# =============================================================================
# Step 9 — Start built-in admin server (429 Part 2 / PR3)
# =============================================================================


def _start_admin_server_if_enabled() -> None:
    """Auto-start the framework-free admin HTTP server when enabled.

    Gated by ``AdminServerSettings.enabled`` (``BALDUR_ADMIN_ENABLED``) and
    ``AdminServerSettings.autostart`` (``BALDUR_ADMIN_AUTOSTART``). The latter
    is the test-process escape hatch — ``tests/conftest.py`` disables it so
    unit tests that call ``init()`` do not leave a listening socket behind.

    Startup failures are logged at WARNING and do not abort ``init()`` — a
    broken admin server must never block the rest of framework startup.
    ``AdminAuthRequiredError`` (non-localhost bind without an API key) is the
    one exception: it is re-raised because silently refusing to start an
    insecurely-configured admin server would mask a serious misconfiguration.
    """
    try:
        from baldur.settings.admin import get_admin_server_settings

        settings = get_admin_server_settings()
    except Exception as e:
        logger.debug("admin.settings_unavailable", error=e)
        return

    if not settings.enabled:
        logger.debug("admin.autostart_disabled_feature_flag")
        return
    if not settings.autostart:
        logger.debug("admin.autostart_disabled_env")
        return

    try:
        from baldur.api.admin import start_admin_server
        from baldur.api.admin.auth import AdminAuthRequiredError
    except Exception as e:
        logger.warning("admin.import_failed", error=e)
        return

    try:
        start_admin_server()
    except AdminAuthRequiredError:
        # Re-raise — refusing to start here is the whole point of the guard.
        raise
    except Exception as e:
        logger.warning("admin.autostart_failed", error=e)


def _register_sql_statistics_if_available() -> None:
    """Auto-register SQLStatisticsRepository when SQL DSN is configured.

    Skipped when a statistics adapter is already registered (e.g. by Django
    host app) or when no SQL DSN is configured. Failures are logged at
    DEBUG — a missing statistics adapter falls back to NullStatisticsRepository.
    """
    try:
        from baldur.factory import ProviderRegistry

        if ProviderRegistry.has_statistics_adapter():
            return

        from baldur.settings.sql import get_sql_settings

        sql_settings = get_sql_settings()
        if not sql_settings.dsn:
            return

        from baldur.adapters.sql.connection import build_connection_factory
        from baldur.adapters.sql.statistics import SQLStatisticsRepository

        adapter = SQLStatisticsRepository(build_connection_factory())
        ProviderRegistry.register_statistics_adapter(adapter)
    except ImportError:
        logger.debug("baldur.sql_statistics_import_unavailable")
    except Exception as e:
        logger.debug("baldur.sql_statistics_registration_skipped", error=e)


# =============================================================================
# Step 10 — Background services (framework-agnostic startup)
# =============================================================================
#
# Each service has its own `enabled` flag in settings. Dormant services
# (capacity_reservation) default to `enabled=False`, so the call is a no-op
# unless an operator explicitly opts in — matching the FEATURE_CATALOG
# Dormant policy of "code maintained, not productized".
#
# Service-side idempotency: each `start()` checks an internal `_active`
# flag, so repeat calls (e.g. Django apps.py invoking init() then a Gunicorn
# worker_init hook) are no-ops at the service layer.


def _start_capacity_reservation_if_enabled() -> None:
    """Initialize + start the CapacityReservationService scheduler when enabled.

    Non-productized feature (not in the v1.0 launch set) — defaults to
    disabled. Settings flag is the only gate; no autostart on import.

    Framework-agnostic wiring: this starter owns both ``initialize()`` (DI +
    EventCalendar/PreWarmer construction + state restore) and ``start()`` (the
    scheduler thread), so every adapter — Django / Flask / FastAPI / plain-Python
    CLI — gets a working scheduler from the single ``start_background_workers()``
    entry point. ``start()`` requires a prior ``initialize()``; co-locating both
    here guarantees the precondition is met regardless of adapter. Both calls are
    idempotent (``initialize()`` guards on ``_initialized``, ``start()`` on a live
    scheduler-thread check), so the Django runserver double-call is benign.

    Fork-safety: skipped in the Gunicorn master because the scheduler is
    thread-based and threads die after ``fork()`` — and ``init()`` is not re-run
    in workers. The framework-agnostic ``post_worker_init`` hook re-runs
    ``start_background_workers()`` per worker after setting ``GUNICORN_WORKER=1``,
    where ``is_gunicorn_master()`` flips to False, so the worker performs the
    initialize + start itself (the master never builds fork-doomed state).
    """
    try:
        from baldur.core.process_utils import is_gunicorn_master

        if is_gunicorn_master():
            logger.debug("capacity_reservation.start_skipped_gunicorn_master")
            return

        from baldur.settings.capacity_reservation import (
            get_capacity_reservation_settings,
        )

        if not get_capacity_reservation_settings().enabled:
            logger.debug("capacity_reservation.start_skipped", reason="disabled")
            return

        from baldur.scaling.graceful_degradation import get_graceful_degradation
        from baldur.services.capacity_reservation.safety_valve_provider import (
            SystemMetricsSafetyValveProvider,
        )
        from baldur.services.capacity_reservation.service import (
            CapacityReservationService,
        )
        from baldur.settings.system_metrics_cache import (
            get_system_metrics_cache_settings,
        )

        # The safety valve reads CPU from the SystemMetricsCache; if that cache
        # is disabled the provider raises on every read and the valve is inert.
        # Surface that at start time so the conditionality is visible.
        if not get_system_metrics_cache_settings().enabled:
            logger.warning("capacity_reservation.safety_valve_metrics_source_disabled")

        service = CapacityReservationService()
        # Wire the safety valve: without a metrics_provider check_safety_valve()
        # is a permanent no-op, and without graceful_degradation the CRITICAL
        # override cannot apply (the two-layer false guarantee this closes).
        service.initialize(
            metrics_provider=SystemMetricsSafetyValveProvider(),
            graceful_degradation=get_graceful_degradation(),
        )
        service.start()
        logger.info("baldur.capacity_reservation_started")
    except ImportError:
        logger.debug("baldur.capacity_reservation_module_not_available")
    except Exception as e:
        logger.warning("baldur.start_capacity_reservation_failed", error=e)


def _start_cell_topology_if_enabled() -> None:
    """Start CellTopologyService (EventBus + anti-entropy + health) when enabled.

    Fork-safety: skipped in the Gunicorn master because the service is
    thread-based and threads die after ``fork()`` — and ``init()`` is not re-run
    in workers. The framework-agnostic ``post_worker_init`` hook re-runs
    ``start_background_workers()`` per worker after setting ``GUNICORN_WORKER=1``,
    where ``is_gunicorn_master()`` flips to False. ``get_cell_topology_service()``
    returns a singleton with an ``_active``-guarded idempotent ``start()``, so the
    Django runserver double-call is benign.
    """
    try:
        from baldur.core.process_utils import is_gunicorn_master

        if is_gunicorn_master():
            logger.debug("cell_topology.start_skipped_gunicorn_master")
            return

        from baldur.settings.cell_topology import get_cell_topology_settings

        if not get_cell_topology_settings().enabled:
            logger.debug("cell_topology.start_skipped", reason="disabled")
            return

        from baldur.services.cell_topology.service import (
            get_cell_topology_service,
        )

        get_cell_topology_service().start()
        logger.info("baldur.cell_topology_started")
    except ImportError:
        logger.debug("baldur.cell_topology_module_not_available")
    except Exception as e:
        logger.warning("baldur.start_cell_topology_failed", error=e)


def _start_meta_watchdog_if_enabled() -> None:
    """Start the SelfHealerWatchdog (detect + escalate) when enabled.

    Framework-independent start path called from ``init()`` so Flask / FastAPI /
    plain-Python CLI get the same wiring Django gets. At v1.0 defaults the
    watchdog runs in slice A (``recovery_enabled=False``) — a detect+escalate
    loop that pages on a stuck/dead subsystem but takes no recovery action.

    Fork-safety: skipped in the Gunicorn master because the watchdog is
    thread-based and threads die after ``fork()`` — and ``init()`` is not re-run
    in workers. The framework-agnostic ``post_worker_init`` hook re-runs
    ``start_background_workers()`` per worker after setting ``GUNICORN_WORKER=1``,
    where ``is_gunicorn_master()`` flips to False. The ``watchdog.start()`` is
    idempotent (``_running`` guard), so the Django runserver double-call
    (``init()`` started it; the service-level guard absorbs any repeat) is benign.

    Escape hatch: ``BALDUR_META_WATCHDOG_AUTOSTART=0`` skips the start — used by
    the unit-test process (``enabled`` now defaults True, so any ``init()`` in a
    test would otherwise spawn the daemon thread). Mirrors
    ``BALDUR_SCHEDULER_AUTOSTART`` / ``BALDUR_ADMIN_AUTOSTART``.

    No-ops in OSS: ``selfhealer_watchdog.safe_get()`` returns None when
    ``baldur_pro`` is absent.
    """
    # 558 D4 — promotes the detect+escalate slice to a framework-agnostic start.
    autostart = os.environ.get("BALDUR_META_WATCHDOG_AUTOSTART", "1").strip().lower()
    if autostart in {"0", "false", "no"}:
        logger.debug("watchdog.autostart_disabled_env")
        return

    try:
        from baldur.core.process_utils import is_gunicorn_master

        if is_gunicorn_master():
            logger.debug("watchdog.start_skipped_gunicorn_master")
            return

        from baldur.settings.meta_watchdog import get_meta_watchdog_settings

        if not get_meta_watchdog_settings().enabled:
            logger.debug("watchdog.start_skipped", reason="disabled")
            return

        from baldur.factory import ProviderRegistry

        watchdog = ProviderRegistry.selfhealer_watchdog.safe_get()
        if watchdog is None:
            logger.debug("baldur.meta_watchdog_unavailable")
            return

        watchdog.start()
        logger.info("baldur.meta_watchdog_started")
    except ImportError:
        logger.debug("baldur.meta_watchdog_module_not_available")
    except Exception as e:
        logger.warning("baldur.start_meta_watchdog_failed", error=e)


def _start_precomputed_cache_if_enabled() -> None:
    """Start the Precomputed Cache proactive-refresh worker when enabled.

    Framework-independent start path called from ``init()`` so Flask / FastAPI /
    plain-Python CLI get the same proactive 3-tier refresh + L1↔L2 drift
    detection Django already gets. Without it, those adapters only run the lazy
    read-path (cache fills on demand) — the background loop that keeps tiers
    warm never executes, so each cold status endpoint pays full L3 compute.

    Fork-safety: skipped in the Gunicorn master because the worker is
    thread-based and threads die after ``fork()`` — and ``init()`` is not re-run
    in workers. The framework-agnostic ``post_worker_init`` hook re-runs
    ``start_background_workers()`` per worker after setting ``GUNICORN_WORKER=1``,
    where ``is_gunicorn_master()`` flips to False. The worker's ``start()`` is
    idempotent (``_running`` guard), so the Django runserver double-call
    (``init()`` started it; the service-level guard absorbs any repeat) is benign.

    Escape hatch: ``BALDUR_PRECOMPUTED_CACHE_AUTOSTART=0`` skips the start — used
    by the unit-test process (``enabled`` defaults True, so any ``init()`` in a
    test would otherwise spawn the refresh timer thread). Mirrors
    ``BALDUR_META_WATCHDOG_AUTOSTART`` / ``BALDUR_SCHEDULER_AUTOSTART``.

    Fail-soft: an ImportError or runtime crash is swallowed — ``init()`` must
    continue, and the lazy read-path remains the safety net.
    """
    autostart = (
        os.environ.get("BALDUR_PRECOMPUTED_CACHE_AUTOSTART", "1").strip().lower()
    )
    if autostart in {"0", "false", "no"}:
        logger.debug("precomputed_cache.autostart_disabled_env")
        return

    try:
        from baldur.core.process_utils import is_gunicorn_master

        if is_gunicorn_master():
            logger.debug("precomputed_cache.start_skipped_gunicorn_master")
            return

        from baldur.settings.precomputed_cache import (
            get_precomputed_cache_settings,
        )

        if not get_precomputed_cache_settings().enabled:
            logger.debug("precomputed_cache.start_skipped", reason="disabled")
            return

        from baldur.services.precomputed_cache import (
            register_default_compute_functions,
            start_precomputed_cache,
        )

        register_default_compute_functions()
        start_precomputed_cache()
        logger.info("baldur.precomputed_cache_started")
    except ImportError:
        logger.debug("baldur.precomputed_cache_module_not_available")
    except Exception as e:
        logger.warning("baldur.start_precomputed_cache_failed", error=e)


def _start_system_metrics_cache_if_enabled() -> None:
    """Start the psutil-backed SystemMetricsCache worker when enabled.

    Framework-independent start path called from ``init()`` so Flask /
    FastAPI / plain-Python CLI get the same background CPU/Memory cache that
    Django gets. The cache is the live-CPU source for the emergency-mode
    recovery gate (and ``rate_controller`` starvation relief); without it
    those consumers fail closed (gate holds emergency mode, relief blocked)
    until an operator forces the release.

    Escape hatch: ``BALDUR_SYSTEM_METRICS_CACHE_AUTOSTART=0`` skips the start —
    used by the unit-test process (``enabled`` defaults True, so any ``init()``
    in a test would otherwise spawn the psutil refresh timer thread). Mirrors
    ``BALDUR_PRECOMPUTED_CACHE_AUTOSTART`` / ``BALDUR_META_WATCHDOG_AUTOSTART``.

    Fork-safety: skipped in the Gunicorn master because the cache is
    thread-based and threads die after ``fork()`` — and ``init()`` is not re-run
    in workers. The framework-agnostic ``post_worker_init`` hook re-runs
    ``start_background_workers()`` per worker after setting ``GUNICORN_WORKER=1``,
    where ``is_gunicorn_master()`` flips to False. The cache's ``start()`` is
    idempotent (``_running`` guard), so the Django runserver double-call is
    benign.

    Fail-soft: an ImportError or runtime crash is swallowed — ``init()`` must
    continue.
    """
    autostart = (
        os.environ.get("BALDUR_SYSTEM_METRICS_CACHE_AUTOSTART", "1").strip().lower()
    )
    if autostart in {"0", "false", "no"}:
        logger.debug("system_metrics_cache.autostart_disabled_env")
        return

    try:
        from baldur.core.process_utils import is_gunicorn_master

        if is_gunicorn_master():
            logger.debug("system_metrics_cache.start_skipped_gunicorn_master")
            return

        from baldur.settings.system_metrics_cache import (
            get_system_metrics_cache_settings,
        )

        smc_settings = get_system_metrics_cache_settings()
        if not smc_settings.enabled:
            logger.debug("baldur.system_metrics_cache_disabled")
            return

        from baldur.services.system_metrics_cache import (
            get_system_metrics_cache,
            start_system_metrics_cache,
        )

        cache = get_system_metrics_cache()
        cache._refresh_interval = smc_settings.refresh_interval
        cache._sample_interval = smc_settings.sample_interval
        cache._max_age_seconds = smc_settings.max_age_seconds

        start_system_metrics_cache()
        logger.info("baldur.system_metrics_cache_started")
    except ImportError:
        logger.debug("baldur.system_metrics_cache_module_not_available")
    except Exception as e:
        logger.warning("baldur.start_system_metrics_cache_failed", error=e)


def _start_rate_controller_if_enabled() -> None:
    """Start the scaling RateController backpressure loop when enabled.

    OSS ``baldur.scaling`` code that was started only from Django ``apps.py``.
    Direct ``_BACKGROUND_WORKER_STARTERS`` membership so Flask / FastAPI /
    plain-Python CLI get the loop too. ``backpressure_enabled`` defaults False,
    so no AUTOSTART env hatch is needed. The service's ``start()`` re-checks
    the flag internally (defense-in-depth) and is ``_running``-idempotent.

    Fork-safety: skipped in the gunicorn master (thread dies after ``fork()``
    and ``init()`` is not re-run per worker); ``post_worker_init`` re-runs the
    start per worker once ``GUNICORN_WORKER=1`` flips the master check.
    """
    try:
        from baldur.core.process_utils import is_gunicorn_master

        if is_gunicorn_master():
            logger.debug("rate_controller.start_skipped_gunicorn_master")
            return

        from baldur.settings.backpressure import get_backpressure_settings

        if not get_backpressure_settings().backpressure_enabled:
            logger.debug("rate_controller.start_skipped", reason="disabled")
            return

        from baldur.scaling.rate_controller import get_rate_controller

        get_rate_controller().start()
        logger.info("baldur.rate_controller_started")
    except ImportError:
        logger.debug("baldur.rate_controller_module_not_available")
    except Exception as e:
        logger.warning("baldur.rate_controller_start_failed", error=e)


def _start_hpa_exporter_if_enabled() -> None:
    """Start the HPA custom-metrics exporter loop when enabled.

    OSS ``baldur.scaling`` code that was started only from Django ``apps.py``.
    Direct ``_BACKGROUND_WORKER_STARTERS`` membership so Flask / FastAPI /
    plain-Python CLI get the loop too. Both ``hpa_enabled`` and
    ``metrics_enabled`` default False, so no AUTOSTART env hatch is needed. The
    service's ``start()`` re-checks both flags internally (defense-in-depth)
    and is ``_running``-idempotent.

    Fork-safety: skipped in the gunicorn master (thread dies after ``fork()``
    and ``init()`` is not re-run per worker); ``post_worker_init`` re-runs the
    start per worker once ``GUNICORN_WORKER=1`` flips the master check.
    """
    try:
        from baldur.core.process_utils import is_gunicorn_master

        if is_gunicorn_master():
            logger.debug("hpa_exporter.start_skipped_gunicorn_master")
            return

        from baldur.settings.backpressure import get_backpressure_settings

        bp_settings = get_backpressure_settings()
        if not (bp_settings.hpa_enabled and bp_settings.metrics_enabled):
            logger.debug("hpa_exporter.start_skipped", reason="disabled")
            return

        from baldur.scaling.hpa_exporter import get_hpa_metrics_exporter

        get_hpa_metrics_exporter().start()
        logger.info("baldur.hpa_exporter_started")
    except ImportError:
        logger.debug("baldur.hpa_exporter_module_not_available")
    except Exception as e:
        logger.warning("baldur.hpa_exporter_start_failed", error=e)


def _start_bulkhead_metrics_updater_if_enabled() -> None:
    """Start the bulkhead Prometheus metrics updater when enabled.

    Keeps the bulkhead gauges (active/max/waiting/utilization) live on every
    install — the series are registered by the shared recorder, so a frozen-
    at-zero gauge would be a false signal. Routes through
    ``start_metrics_updater(interval=...)`` so
    ``BALDUR_BULKHEAD_METRICS_UPDATE_INTERVAL`` is honored — this starter is
    the sole production first-caller of ``get_metrics_updater``, which
    captures ``interval`` at first call. The updater reads the registry via
    the resolution chain, so a populated provider slot is observed
    transparently.

    Escape hatch: ``BALDUR_BULKHEAD_METRICS_AUTOSTART=0`` skips the start —
    used by the unit-test process (``metrics_enabled`` defaults True, so any
    ``init()`` in a test would otherwise spawn the daemon thread). Mirrors the
    sibling AUTOSTART hatches (meta_watchdog / precomputed_cache /
    system_metrics_cache / cb_state_seed).

    Fork-safety: skipped in the gunicorn master (thread dies after ``fork()``
    and ``init()`` is not re-run per worker); the framework-agnostic
    ``post_worker_init`` hook re-runs the start per worker. ``start()`` is
    idempotent (``_running`` guard), so the runserver double-call is benign.
    """
    autostart = os.environ.get("BALDUR_BULKHEAD_METRICS_AUTOSTART", "1").strip().lower()
    if autostart in {"0", "false", "no"}:
        logger.debug("bulkhead_metrics_updater.autostart_disabled_env")
        return

    try:
        from baldur.core.process_utils import is_gunicorn_master

        if is_gunicorn_master():
            logger.debug("bulkhead_metrics_updater.start_skipped_gunicorn_master")
            return

        from baldur.settings.bulkhead import get_bulkhead_settings

        settings = get_bulkhead_settings()
        if not settings.metrics_enabled:
            logger.debug("bulkhead_metrics_updater.start_skipped", reason="disabled")
            return

        from baldur.services.bulkhead.metrics import start_metrics_updater

        start_metrics_updater(interval=settings.metrics_update_interval)
        logger.info("baldur.bulkhead_metrics_updater_started")
    except ImportError as exc:
        logger.debug("baldur.bulkhead_metrics_updater_module_not_available", error=exc)
    except Exception as exc:
        logger.warning("baldur.bulkhead_metrics_updater_start_failed", error=exc)


def _seed_circuit_breaker_state() -> None:
    """Seed the ``baldur_circuit_breaker_state`` gauge from the repo.

    Reads every persisted circuit-breaker state via the reused
    ``update_circuit_breaker_gauges()`` (which iterates
    ``get_circuit_breaker_repo().get_all_states()`` and sets each gauge to the
    breaker's *actual* current state) so a freshly scraped process exposes a
    series for each breaker without waiting for an in-process transition. The
    multi-process benefit: a worker that loaded an OPEN state from shared Redis
    but never itself transitions still publishes the correct gauge.

    Split out from the scheduling wrapper so tests invoke it synchronously. The
    repo read is best-effort: a transient unavailability leaves the gauge empty
    for this process (re-seeded at the next restart, and any transition still
    sets it via the original path).
    """
    try:
        from baldur.services.metrics.updaters import update_circuit_breaker_gauges

        states = update_circuit_breaker_gauges()
        logger.debug("cb_state_seed.seed_completed", count=len(states))
    except ImportError:
        logger.debug("cb_state_seed.updater_unavailable")
    except Exception as e:
        logger.warning("cb_state_seed.seed_failed", error=e)


def _seed_circuit_breaker_state_if_enabled() -> None:
    """Schedule the per-process CB-state startup seed when enabled.

    Registered in ``_BACKGROUND_WORKER_STARTERS`` so it runs once per serving
    process: the single-process admin-server / CLI (non-gunicorn) and each
    gunicorn worker (re-invoked via ``post_worker_init`` once
    ``GUNICORN_WORKER=1`` flips ``is_gunicorn_master()`` False). A direct
    ``init()`` step would not reach forked workers — ``init()`` runs once in the
    master and a Timer scheduled there does not survive ``fork()``.

    The seed runs on a daemon ``threading.Timer`` with jitter (mirrors the
    Django MetricHydrator) so it never blocks startup nor stampedes shared Redis
    on multi-server restarts. Jitter is sourced from the existing
    ``MetricsSettings.jitter_enabled`` / ``jitter_max_delay_seconds`` — no
    dedicated env var.

    Gating mirrors the sibling starters in this registry:
    - ``BALDUR_CB_STATE_SEED_AUTOSTART=0`` escape hatch (the unit-test process
      sets this; ``metrics.enabled`` defaults True, so any ``init()`` in a test
      would otherwise schedule the daemon Timer). Mirrors the sibling AUTOSTART
      hatches (meta_watchdog / system_metrics_cache / precomputed_cache).
    - skipped in the gunicorn master (the master serves no scrapes and its Timer
      would not survive ``fork()``).
    - gated on ``get_metrics_settings().enabled``.
    - a once-per-process done-flag so a double invocation (Django ``init()`` per
      worker + ``post_worker_init``) schedules at most once.

    Fail-soft: an ImportError or runtime crash is swallowed — ``init()`` must
    continue.
    """
    autostart = os.environ.get("BALDUR_CB_STATE_SEED_AUTOSTART", "1").strip().lower()
    if autostart in {"0", "false", "no"}:
        logger.debug("cb_state_seed.autostart_disabled_env")
        return

    try:
        from baldur.core.process_utils import is_gunicorn_master

        if is_gunicorn_master():
            logger.debug("cb_state_seed.start_skipped_gunicorn_master")
            return

        from baldur.settings.metrics import get_metrics_settings

        settings = get_metrics_settings()
        if not settings.enabled:
            logger.debug("cb_state_seed.start_skipped", reason="metrics_disabled")
            return

        global _cb_state_seed_done
        with _cb_state_seed_lock:
            if _cb_state_seed_done:
                logger.debug("cb_state_seed.already_seeded")
                return
            _cb_state_seed_done = True

        jitter = 0.0
        if settings.jitter_enabled:
            import random

            jitter = random.uniform(0, settings.jitter_max_delay_seconds)

        timer = threading.Timer(jitter, _seed_circuit_breaker_state)
        timer.daemon = True
        timer.start()
        logger.info("cb_state_seed.seed_scheduled", jitter=jitter)
    except ImportError:
        logger.debug("cb_state_seed.module_not_available")
    except Exception as e:
        logger.warning("cb_state_seed.start_failed", error=e)


def _reset_cb_state_seed() -> None:
    """Reset the once-per-process CB-state seed guard — test isolation only."""
    global _cb_state_seed_done
    with _cb_state_seed_lock:
        _cb_state_seed_done = False


# =============================================================================
# Background daemon-worker registry — single source of truth (D4)
# =============================================================================
#
# The OSS ``init()``-started background daemon workers, enumerated once so that
# both ``init()`` and the gunicorn ``post_worker_init`` hook start the identical
# set with no divergent hand-maintained list. Each starter carries its own
# ``is_gunicorn_master()`` early-return, which makes the single
# ``start_background_workers()`` correct in both contexts: the skip fires during
# ``init()`` (master / pre-fork) and passes after ``post_worker_init`` sets
# ``GUNICORN_WORKER=1`` (per-worker, post-fork).
#
# ``rate_controller`` / ``hpa_exporter`` (615 D4/G6) are OSS ``baldur.scaling``
# loops that were Django-only (apps.py F30); both default-OFF and
# ``_running``-idempotent, so they join directly with no AUTOSTART hatch.
# ``bulkhead_metrics_updater`` joined the tuple when the bulkhead primitives
# went core-tier (709); default-ON, so it carries a
# ``BALDUR_BULKHEAD_METRICS_AUTOSTART`` test hatch.
# ``circuit_mesh`` and the other PRO Django-only extras (crisis-multiplier
# invalidation, auto-tuning) are ``baldur_pro``-internal and live in
# ``ProviderRegistry.startup_integrations`` instead (615 D1), iterated
# by ``start_background_workers()`` after this OSS tuple.
# ``audit`` / ``dlq_outbox`` / ``default_scheduler`` / ``admin_server`` are
# excluded: they have distinct per-process semantics (PID-isolated WAL,
# leader-election-gated scheduler, single-socket admin server) that the uniform
# daemon-thread restart does not fit.
# ``_seed_circuit_breaker_state_if_enabled`` is a one-shot (not a daemon loop):
# it schedules a single jittered Timer that seeds the per-process
# ``baldur_circuit_breaker_state`` gauge from the repo's actual state, so each
# serving process exposes the CB-state series without waiting for an in-process
# transition. It carries the same ``is_gunicorn_master()`` skip plus a
# once-per-process done-flag, and a ``BALDUR_CB_STATE_SEED_AUTOSTART`` test
# hatch (``metrics.enabled`` defaults True, unlike the default-OFF scaling
# loops).

_BACKGROUND_WORKER_STARTERS: tuple[Callable[[], None], ...] = (
    _start_capacity_reservation_if_enabled,
    _start_cell_topology_if_enabled,
    _start_meta_watchdog_if_enabled,
    _start_precomputed_cache_if_enabled,
    _start_system_metrics_cache_if_enabled,
    _start_rate_controller_if_enabled,
    _start_hpa_exporter_if_enabled,
    _start_bulkhead_metrics_updater_if_enabled,
    _seed_circuit_breaker_state_if_enabled,
)


def start_background_workers() -> None:
    """Start the OSS ``init()``-started background daemon workers.

    The single entry point consumed by both ``init()`` (where the per-starter
    ``is_gunicorn_master()`` skip suppresses every start in the master / before
    ``fork()``) and the gunicorn ``post_worker_init`` hook (where every enabled
    worker starts per-forked-worker once ``GUNICORN_WORKER=1`` is set). Threads
    do not survive ``fork()`` and ``init()`` is not re-run in workers, so the
    post-fork hook is the only place a non-Django gunicorn worker starts these
    proactive loops.

    Each starter is independently fail-soft (it swallows its own
    ImportError/Exception) and order-independent, so iterating the registry can
    neither raise nor be reordered into a wrong state.

    After the OSS tuple, the PRO ``startup_integrations`` slot is iterated —
    the framework-agnostic mirror of the ``shutdown_integrations`` iteration.
    The PRO package populates the slot under ACTIVE entitlement; an empty slot
    (OSS-only / unentitled install) iterates to a no-op. Each slot starter is
    itself fail-soft, but the per-name try/except mirrors the
    shutdown-integration consumer shape so a resolve failure cannot abort the
    remaining starters.
    """
    for starter in _BACKGROUND_WORKER_STARTERS:
        starter()

    from baldur.factory.registry import ProviderRegistry

    for name in ProviderRegistry.startup_integrations.list_providers():
        try:
            ProviderRegistry.startup_integrations.get_provider(name)()
        except Exception as exc:
            logger.debug(
                "baldur.startup_integration_resolve_failed",
                name=name,
                error=exc,
            )
