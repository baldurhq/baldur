"""
Django App Configuration for Baldur.

This allows the baldur.adapters.django module to be used
as a Django app in INSTALLED_APPS.

Lifecycle Hooks:
    1. ready() - Called on every server start
       - Environment variable snapshot logging (audit trail)
       - Metric Gauge hydration (Startup Hydration)
       - Runs every time because env vars can change between restarts

    2. post_migrate signal - Called only after migrations
       - RBAC group creation (DB schema initialization)
       - Runs only when DB schema changes (efficient)

RBAC Groups:
    - baldur_viewer: Read-only access (dashboard, status, audit logs)
    - baldur_operator: Operational tasks (DLQ replay, archive)
    - baldur_admin: Full access (CB control, system enable/disable, config)

Design Rationale:
    - Environment variables = Process lifecycle (ready)
    - Database schema = Data lifecycle (post_migrate)
    - Metric Gauges = Server lifecycle (ready, with jitter)
    - Industry standard: 12-Factor App, Spring Boot ApplicationReadyEvent

Note:
    As of v2.0.0, Redis is the default storage backend.
    This app config still provides:
    - RBAC group auto-creation
    - Environment variable audit
    - Startup hydration for Prometheus gauges
    - Pre-computed cache worker

Startup responsibilities are delegated to the ``startup`` sub-package:
    - RBACInitializer: RBAC group creation via post_migrate signal
    - EnvironmentAuditor: Environment variable snapshot + hash chain sync
    - MetricHydrator: Prometheus gauge hydration
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import structlog
from django.apps import AppConfig

from baldur.adapters.django.startup import (
    BALDUR_GROUPS,
    EnvironmentAuditor,
    MetricHydrator,
    RBACInitializer,
    create_baldur_groups,
)

if TYPE_CHECKING:
    pass

__all__ = [
    "BALDUR_GROUPS",
    "BaldurConfig",
    "create_baldur_groups",
]

logger = structlog.get_logger()


class BaldurConfig(AppConfig):
    """Django app configuration for baldur."""

    name = "baldur.adapters.django"
    label = "baldur"
    verbose_name = "Baldur System"
    default_auto_field = "django.db.models.BigAutoField"

    # 317: Correlation Engine Analysis Loop
    _correlation_loop_started: bool = False
    _correlation_loop_lock: threading.Lock = threading.Lock()

    def ready(self):
        """
        Called when the app is ready (every server start).

        Responsibilities:
        1. Connect post_migrate signal for RBAC group creation
        2. Log environment variable snapshot for audit trail
        3. Validate config with Safe Defaults (Fail-Safe Default)
        4. Hydrate metric gauges with jitter (Startup Hydration)
        5. Start pre-computed cache worker (V3 Optimization)

        Note: Environment snapshot is logged here (not in post_migrate) because
        env vars can change on every restart, not just during migrations.
        This aligns with 12-Factor App principles and Spring Boot patterns.
        """
        # Connect post_migrate signal for RBAC group creation
        # sender=self ensures it only runs when this app's migrations complete
        RBACInitializer.connect_post_migrate(self)

        # Connect session signal handlers (user_logged_in / user_logged_out)
        self._connect_session_signals()

        # Celery autodiscover: baldur.celery_tasks registration (223 Host App Decoupling)
        self._autodiscover_celery_tasks()

        # Sync hash chain state (Redis <-> Local file).
        # Stays in apps.py because it depends on Django settings — Wave 5.5C
        # will migrate this to a framework-agnostic settings adapter.
        EnvironmentAuditor.sync_hash_chain_on_startup()

        # 416: framework-agnostic init() — handles config validation, default
        # event handlers, shutdown handlers, PRO entry-point hooks, audit
        # default provider, audit pipeline startup, and env_snapshot recording.
        # Django-specific quarantine activation is passed as a callback.
        import baldur

        baldur.init(quarantine_callback=self._activate_quarantine_mode)

        # 593: Django OTel request instrumentation. Must run here (ready(),
        # after baldur.init() registered the composite TraceContext+Baggage
        # propagator) and BEFORE WSGIHandler.load_middleware() reads
        # settings.MIDDLEWARE — DjangoInstrumentor inserts its _DjangoMiddleware
        # at position 0, which only takes effect if the insert precedes
        # load_middleware(). get_wsgi_application() runs django.setup() (-> this
        # ready()) then constructs WSGIHandler, so the ordering holds in
        # dev-server, gunicorn --preload master, and no-preload worker alike.
        self._instrument_django_if_enabled()

        # 653: HTTP RED latency parity — auto-inject the sync HttpMetricsMiddleware
        # so the zero-config Django path emits baldur_http_request_duration_seconds
        # out of the box (Flask/FastAPI already do via their BaldurMiddleware). Runs
        # in the same "after baldur.init(), before WSGIHandler.load_middleware()"
        # window the OTel insert above relies on.
        self._inject_http_metrics_middleware_if_enabled()

        # 317: Orphan service wiring — pure-memory initialization (Category A)
        self._initialize_orphan_services()

        # 320: Celery signal missing detection warning
        self._warn_if_celery_signals_missing()

        # Admission control middleware absence warning (PRO Django user who
        # forgot to register AdmissionControlMiddleware). Called after
        # baldur.init() so PRO registration (bulkhead_registry) is done.
        self._warn_if_admission_middleware_missing()

        # Background threads: Gauge hydration, Precomputed Cache, System Metrics, Watchdog.
        # In Gunicorn preload mode, ready() runs in Master — background threads
        # die after fork(). post_worker_init calls start_background_threads() instead.
        # In dev server (manage.py runserver), start threads directly here.
        if self._should_start_background_threads():
            self._start_all_background_threads()

        # 632 D7 — required-secret validation is centralized in baldur.init()
        # (the _validate_critical_secrets step), invoked above at the
        # baldur.init() call, so the prod boot-abort gate fires on every
        # framework adapter. This adapter no longer runs its own Django-only
        # secret-validation step.

        # Register JWT blacklist hook for session invalidation
        self._register_jwt_blacklist_hook()

    # 416: _register_default_event_handlers and _register_shutdown_handlers
    # were relocated to baldur.bootstrap as framework-agnostic module
    # functions. apps.py.ready() now invokes them via baldur.init().

    @staticmethod
    def _connect_session_signals():
        """Connect Django session signal handlers."""
        try:
            from baldur.adapters.django.signal_hooks import (
                connect_session_signals,
            )

            connect_session_signals()
        except Exception as e:
            logger.warning(
                "baldur.connect_session_signals_failed",
                error=e,
            )

    @staticmethod
    def _autodiscover_celery_tasks():
        """
        Celery autodiscover: register baldur.celery_tasks module.

        Automatically registers Celery tasks that the host app previously
        had to import manually.  Silently skipped when Celery is not installed.
        """
        try:
            from celery import current_app

            current_app.autodiscover_tasks(["baldur.celery_tasks"])
            logger.info("baldur.celery_tasks_autodiscovered_baldur")
        except ImportError:
            logger.debug("baldur.celery_not_installed")
        except Exception as e:
            logger.warning(
                "baldur.autodiscover_celery_tasks_failed",
                error=e,
            )

    @staticmethod
    def _instrument_django_if_enabled():
        """Wire OTel Django request instrumentation when OTel is enabled.

        Delegates to ``baldur.observability.instrument_django`` (idempotent,
        internally gated by the observability profile via
        ``ObservabilitySettings.effective_otel_enabled`` +
        ``OTEL_DJANGO_INSTRUMENT_ENABLED``). The extra ``BALDUR_OTEL_AUTOSTART``
        hatch (default ``"1"``) lets the test suite opt out so a test that sets
        ``BALDUR_OBSERVABILITY_PROFILE=otel_collector`` does not monkey-patch
        ``settings.MIDDLEWARE`` globally — the same shared gate the
        framework-agnostic bootstrap instrumentors use.

        Called from ``ready()`` after ``baldur.init()`` (composite propagator
        live) and before ``WSGIHandler.load_middleware()``, because
        ``DjangoInstrumentor`` inserts its middleware at position 0 of
        ``settings.MIDDLEWARE`` and that only takes effect if the insert precedes
        the handler reading the list. ``instrument_django`` never raises (returns
        a bool), so it cannot break ``ready()``'s exception-propagation contract.
        """
        from baldur.bootstrap import _otel_autostart_enabled

        if not _otel_autostart_enabled():
            logger.debug("otel.django_autostart_disabled_env")
            return

        try:
            from baldur.observability import instrument_django

            instrument_django()
        except ImportError:
            logger.debug("otel.django_instrumentation_module_not_available")
        except Exception as e:
            logger.warning("baldur.instrument_django_failed", error=e)

    @staticmethod
    def _inject_http_metrics_middleware_if_enabled():
        """Auto-inject the sync HTTP RED metrics middleware on the zero-config path.

        Django emits the OSS HTTP-RED latency histogram
        (``baldur_http_request_duration_seconds``) only when
        ``HttpMetricsMiddleware`` is present in ``settings.MIDDLEWARE``. The
        getting-started Django path adds nothing but ``baldur.adapters.django`` to
        ``INSTALLED_APPS``, so without this inject the HTTP Latency panel stays
        permanently empty — while Flask and FastAPI emit RED zero-config (their
        single ``BaldurMiddleware`` records it). This prepends the sync middleware
        so Django reaches the same out-of-the-box behavior.

        Called from ``ready()`` immediately after ``_instrument_django_if_enabled()``
        — the same "after ``baldur.init()``, before ``WSGIHandler.load_middleware()``
        reads ``settings.MIDDLEWARE``" window the OTel insert relies on, so the
        prepend takes effect in dev-server, gunicorn ``--preload`` master, and
        no-preload worker alike.

        Gates (no new settings flag): skipped under ``BALDUR_TEST_MODE=true`` (the
        unit suite sets this session-wide, so the integrated path never mutates the
        global ``settings.MIDDLEWARE``) and when ``get_metrics_settings().enabled``
        is ``False`` (the observability off-switch doubles as the auto-inject
        off-switch). Idempotent: skipped when either RED middleware class is already
        listed — matched by dotted-path suffix, which covers the long
        (``...http_metrics.HttpMetricsMiddleware``) and short
        (``...middleware.HttpMetricsMiddleware``) aliases and the async
        ``AsyncHttpMetricsMiddleware`` an ASGI operator may list explicitly — so
        neither an explicit sync listing nor a hand-added async one double-records.

        Best-effort: the body is wrapped so a wiring failure logs and returns
        instead of breaking ``ready()``'s contract (mirrors
        ``_instrument_django_if_enabled``).
        """
        if os.environ.get("BALDUR_TEST_MODE") == "true":
            return

        try:
            from baldur.settings.metrics import get_metrics_settings

            if not get_metrics_settings().enabled:
                return

            from django.conf import settings

            # Build a NEW list — never mutate in place, because Django's
            # global_settings.MIDDLEWARE default is a shared list. list(...) also
            # tolerates a tuple-typed MIDDLEWARE ([path] + tuple would raise).
            existing = list(getattr(settings, "MIDDLEWARE", None) or [])

            # Idempotency: match either RED middleware class by dotted-path suffix
            # so the long/short alias and the async variant are all caught.
            red_suffixes = (
                ".HttpMetricsMiddleware",
                ".AsyncHttpMetricsMiddleware",
            )
            if any(
                entry.endswith(suffix) for entry in existing for suffix in red_suffixes
            ):
                return

            path = "baldur.api.django.middleware.http_metrics.HttpMetricsMiddleware"
            settings.MIDDLEWARE = [path, *existing]
            logger.debug("baldur.http_metrics_middleware_injected")
        except Exception as e:
            logger.warning(
                "baldur.http_metrics_middleware_inject_failed",
                error=e,
            )

    # 416: _validate_startup_config relocated to baldur.bootstrap.
    # apps.py.ready() now calls baldur.init() which invokes the
    # framework-agnostic implementation; this Django-bound method only
    # exists as the quarantine_callback target below.

    def _activate_quarantine_mode(self, error: Exception):
        """
        Activate Quarantine Mode (LEVEL_3) on fatal setting violation.

        Quarantine Mode:
        - EmergencyLevel.LEVEL_3 activated (Critical traffic only, 50%)
        - System starts but operates in isolated state
        - Manual intervention required (fix settings and restart)

        Args:
            error: FatalConfigError instance
        """
        try:
            from baldur_pro.services.emergency_mode import (
                EmergencyLevel,
                GracefulDegradationManager,
            )

            manager = GracefulDegradationManager()

            # Quarantine Mode (LEVEL_3) activation. Indefinite duration —
            # manual release required after fixing config and restarting.
            manager.activate_manual(
                level=EmergencyLevel.LEVEL_3,
                reason=f"Config Quarantine: {str(error)[:200]}",
                activated_by="system:config_validation",
                duration_minutes=None,
            )

            logger.critical("quarantine.system_started_quarantine_mode")

        except ImportError:
            logger.warning("baldur.quarantine_module_unavailable")
        except Exception as e:
            logger.exception(
                "baldur.activate_quarantine_mode_failed",
                error=e,
            )

    # =========================================================================
    # 317: Correlation Engine Analysis Loop (Category B — background thread)
    # =========================================================================

    def _start_correlation_engine_loop(self):
        """317: Start CorrelationEngine analysis loop (LeaderScheduler thread)."""
        try:
            from baldur.settings.correlation_engine import (
                get_correlation_engine_settings,
            )

            engine_settings = get_correlation_engine_settings()
            if not engine_settings.enabled:
                return

            with self._correlation_loop_lock:
                if self._correlation_loop_started:
                    return
                BaldurConfig._correlation_loop_started = True

            # 599 D12 — the engine lives in the private distribution; the
            # dormant hook registers a per-worker start callable. Empty
            # slot (OSS-only install) -> debug-log no-op, flag reset so a
            # later registration can still start it.
            from baldur.factory.registry import ProviderRegistry

            slot = ProviderRegistry.worker_background_starts
            if not slot.has_provider("correlation_engine_loop"):
                with BaldurConfig._correlation_loop_lock:
                    BaldurConfig._correlation_loop_started = False
                logger.debug("baldur.correlation_engine_module_not_available")
                return

            slot.get_provider("correlation_engine_loop")()
            logger.info("baldur.correlation_engine_loop_started")

        except Exception as e:
            with BaldurConfig._correlation_loop_lock:
                BaldurConfig._correlation_loop_started = False
            logger.warning(
                "baldur.start_correlation_engine_loop_failed",
                error=e,
            )

    # =========================================================================
    # JWT Blacklist Hook Registration
    # =========================================================================

    def _register_jwt_blacklist_hook(self):
        """
        Register JWT blacklist callback.

        Only registers the callback when rest_framework_simplejwt.token_blacklist
        is in INSTALLED_APPS.  On security violation (TOKEN_FORGED) detection,
        blacklists all OutstandingTokens for the user.
        """
        try:
            from django.apps import apps

            if not apps.is_installed("rest_framework_simplejwt.token_blacklist"):
                logger.debug("baldur.jwt_hook_dependency_unavailable")
                return

            from baldur.services.security.hooks import (
                register_session_invalidation_hook,
            )

            def blacklist_user_jwt(user_id: int) -> str:
                """Blacklist all OutstandingTokens for a user."""
                from rest_framework_simplejwt.token_blacklist.models import (
                    BlacklistedToken,
                    OutstandingToken,
                )

                # rest_framework_simplejwt has no django-stubs coverage, so
                # `.objects` is invisible to mypy on these concrete models.
                tokens = OutstandingToken.objects.filter(user_id=user_id)  # type: ignore[attr-defined]
                count = 0
                for token in tokens:
                    _, created = BlacklistedToken.objects.get_or_create(token=token)  # type: ignore[attr-defined]
                    if created:
                        count += 1
                return f"jwt_blacklisted({count})" if count > 0 else ""

            register_session_invalidation_hook(blacklist_user_jwt)
            logger.info("baldur.jwt_blacklist_hook_registered")

            # DONE(#217): OutstandingToken cleanup Celery Beat registered
            # Task: baldur/tasks/cleanup_tasks.py flush_expired_jwt_tokens
            # Schedule: get_cleanup_beat_schedule() + myproject/celery.py (daily 02:30)

        except ImportError as e:
            logger.debug(
                "baldur.jwt_hook_registration_skipped",
                error=e,
            )
        except Exception as e:
            logger.warning(
                "baldur.jwt_hook_registration_failed",
                error=e,
            )

    # =========================================================================
    # 317: Orphan Service Wiring — Pure-Memory Initialization (Category A)
    # =========================================================================

    def _initialize_orphan_services(self):
        """317: Initialize orphan services — ServiceDependencyGraph topological sort order."""
        try:
            from baldur.core.dependency_graph import ServiceDependencyGraph

            graph = ServiceDependencyGraph()

            graph.register_service("event_journal")
            graph.register_service("config_shadow", depends_on=["event_journal"])
            graph.register_service("event_bus")
            graph.register_service("correlation_engine", depends_on=["event_bus"])
            graph.register_service("saga")
            graph.register_service("config")
            # capacity_reservation init relocated to baldur.bootstrap's
            # framework-agnostic _start_capacity_reservation_if_enabled (it now
            # owns both initialize() and start()), so it is no longer wired here.
            # runbook initialization moved to baldur_pro.register_pro_services()
            # (599 D12 — feature relocated to the private distribution).

            init_order = graph.topological_sort_subset(
                services=[
                    "event_journal",
                    "correlation_engine",
                    "saga",
                    "config",
                ],
                direction="leaves_first",
            )

            initializers = {
                "event_journal": self._init_event_journal,
                "correlation_engine": self._init_correlation_engine,
                "saga": self._init_saga_autodiscover,
                "config": self._init_config_propagator,
            }

            for service_name in init_order:
                if service_name in initializers:
                    initializers[service_name]()

            logger.info(
                "baldur.orphan_services_initialized",
                init_order=init_order,
            )

        except Exception as e:
            logger.warning(
                "baldur.initialize_orphan_services_failed",
                error=e,
            )

    @staticmethod
    def _init_event_journal():
        """317: Initialize EventJournal — register EventBus subscription."""
        try:
            from baldur.settings.event_journal import EventJournalSettings

            ej_settings = EventJournalSettings()
            if not ej_settings.enabled:
                logger.debug("baldur.event_journal_disabled")
                return

            from baldur.services.event_journal import init_event_journal

            init_event_journal()
            logger.info("baldur.event_journal_initialized")
        except ImportError:
            logger.debug("baldur.event_journal_module_not_available")
        except Exception as e:
            logger.warning("baldur.init_event_journal_failed", error=e)

    @staticmethod
    def _init_correlation_engine():
        """317: Initialize CorrelationEngine pure memory (EventBus subscription, strategies)."""
        try:
            from baldur.settings.correlation_engine import (
                get_correlation_engine_settings,
            )

            engine_settings = get_correlation_engine_settings()
            if not engine_settings.enabled:
                logger.debug("baldur.correlation_engine_disabled")
                return

            # 599 D12 — resolve the init callable registered by the
            # dormant hook. Empty slot (OSS-only install) -> debug no-op.
            from baldur.factory.registry import ProviderRegistry

            slot = ProviderRegistry.worker_background_starts
            if not slot.has_provider("correlation_engine_init"):
                logger.debug("baldur.correlation_engine_module_not_available")
                return

            slot.get_provider("correlation_engine_init")()
            logger.info("baldur.correlation_engine_initialized")
        except Exception as e:
            logger.warning("baldur.init_correlation_engine_failed", error=e)

    @staticmethod
    def _init_saga_autodiscover():
        """317: Auto-register Saga definitions."""
        try:
            from celery import current_app

            current_app.autodiscover_tasks(["baldur_pro.services.saga"])
            logger.info("baldur.saga_tasks_autodiscovered")
        except ImportError:
            logger.debug("baldur.saga_autodiscover_skipped_no_celery")
        except Exception as e:
            logger.warning("baldur.saga_autodiscover_failed", error=e)

    @staticmethod
    def _init_config_propagator():
        """317: Initialize GlobalConfigPropagator."""
        try:
            from baldur.services.config.propagator import (
                get_global_config_propagator,
            )

            get_global_config_propagator()
            logger.info("baldur.config_propagator_initialized")
        except ImportError:
            logger.debug("baldur.config_propagator_module_not_available")
        except Exception as e:
            logger.warning("baldur.init_config_propagator_failed", error=e)

    # =========================================================================
    # 320: Celery Signal Registration Detection
    # =========================================================================

    @staticmethod
    def _warn_if_celery_signals_missing():
        """Warn if Celery signals are not registered."""
        from baldur.settings.auto_config import get_auto_config_settings

        auto_settings = get_auto_config_settings()
        if not auto_settings.celery_signal_warning:
            return

        try:
            from celery.signals import task_failure

            if not task_failure.receivers:
                logger.warning(
                    "baldur.celery_signals_not_registered",
                    hint="Call setup_baldur_signals(app=app) in your celery.py.",
                )
        except ImportError:
            pass

    @staticmethod
    def _warn_if_admission_middleware_missing():
        """Warn when PRO admission control is enabled but its middleware is absent.

        Django reads ``settings.MIDDLEWARE`` once at startup (it cannot be
        modified at runtime), so an operator who never adds
        ``AdmissionControlMiddleware`` silently gets no admission control with no
        signal. This fires only for the actual gap — admission enabled AND the
        per-tier Bulkhead registry present (PRO) AND the middleware path absent
        from ``settings.MIDDLEWARE``. An OSS-only Django user has no admission
        anyway (``check_admission`` is a clean no-op), so no false alarm.
        """
        try:
            from baldur.settings.admission_control import (
                get_admission_control_settings,
            )

            if not get_admission_control_settings().enabled:
                return

            from baldur.factory.registry import ProviderRegistry

            if ProviderRegistry.bulkhead_registry.safe_get() is None:
                # OSS — admission is a clean no-op, nothing to warn about.
                return

            from django.conf import settings

            middleware = list(getattr(settings, "MIDDLEWARE", None) or [])
            path = "baldur.api.django.admission_control.AdmissionControlMiddleware"
            if path not in middleware:
                logger.warning(
                    "baldur.admission_control_middleware_not_registered",
                    hint=(
                        f"Add '{path}' to settings.MIDDLEWARE to enable PRO "
                        "tier-based admission control."
                    ),
                )
        except ImportError:
            pass
        except Exception as e:
            logger.warning(
                "baldur.admission_middleware_check_failed",
                error=e,
            )

    # =========================================================================
    # Fork-Safety: Background Thread Lifecycle (Section 5.2)
    # =========================================================================

    @staticmethod
    def _should_start_background_threads() -> bool:
        """Determine if background threads should start in this process.

        Returns False for:
        - Gunicorn Master (threads die after fork)
        - Test mode (threads race with singleton resets, attempt
          real Redis/Prometheus connections)

        Returns True for dev server and Gunicorn workers.
        """
        if os.environ.get("BALDUR_TEST_MODE") == "true":
            return False

        from baldur.core.process_utils import is_gunicorn_master

        if is_gunicorn_master():
            logger.info(
                "baldur.skipping_background_threads_gunicorn_master",
            )
            return False

        return True

    def _start_all_background_threads(self):
        """Start all background threads (gauge hydration, cache, metrics, watchdog).

        450 Phase 4 — runtime propagation contract:

        Plain ``threading.Thread`` workers do **not** inherit ContextVar values
        from the parent (PEP 567). The framework relies on
        :data:`baldur.runtime._default_runtime` (process-global fallback inside
        :func:`baldur.runtime.get_runtime`) so worker threads spawned here see
        the same ``BaldurRuntime`` instance — and therefore the same singleton
        store and settings cache — as the AppConfig.ready() thread that
        invokes us. No per-thread ``copy_context()`` is required for runtime
        access.

        Per-callsite ``copy_context()`` is still the correct pattern when a
        worker needs to inherit *application* ContextVars (ActorContext, OTEL
        baggage, etc.); see :func:`baldur.bootstrap._wrap_with_context` for the
        canonical example used by the leader scheduler.
        """
        MetricHydrator.hydrate()
        self._start_correlation_engine_loop()
        # Every init()-started background worker — the OSS-5 daemon workers
        # (meta_watchdog, precomputed_cache, system_metrics_cache,
        # capacity_reservation, cell_topology) PLUS the OSS scaling loops
        # (rate_controller, hpa_exporter) and the PRO startup integrations
        # (bulkhead metrics updater, crisis-multiplier invalidation,
        # auto-tuning, circuit_mesh) — is started by
        # baldur.bootstrap.start_background_workers() (615 D1/D4), invoked by
        # init() and by the framework-agnostic gunicorn post_worker_init hook,
        # so Django / FastAPI / Flask / CLI all get the same wiring. They are
        # intentionally NOT started here; this method now carries only the
        # Django-adapter-intrinsic extras (gauge hydration + correlation loop).

    @classmethod
    def start_background_threads(cls):
        """Public entry point for Gunicorn post_worker_init hook.

        Resets duplicate-start guards so threads can be started fresh
        in each forked Worker, then starts all background threads.
        """
        cls._reset_all_background_state()

        try:
            from django.apps import apps

            app_config = apps.get_app_config("baldur")
            app_config._start_all_background_threads()
        except Exception as exc:
            logger.warning(
                "baldur.background_thread_startup_failed",
                error=exc,
            )

    @classmethod
    def stop_background_threads(cls):
        """Public entry point for Gunicorn worker_exit hook.

        Resets the duplicate-start guard flags for the Django-tracked
        background threads. The threads are ``daemon=True`` and terminate
        when the worker process exits, so there is nothing to join here;
        in-flight work is drained upstream by the ShutdownCoordinator wait
        in the ``worker_exit`` hook (``baldur.adapters.gunicorn.hooks``)
        that runs before this call.
        """
        cls._reset_all_background_state()

    @classmethod
    def _reset_all_background_state(cls):
        """Reset the Django-only duplicate-start guards for a fresh Worker.

        Only the Django-only extras (gauge hydration, correlation loop) keep
        per-worker guards here. The OSS-5 init()-started workers carry their own
        service-level ``_running``/``_active`` idempotency guard and are started
        via ``start_background_workers()``, so they need no Django-side reset.
        """
        MetricHydrator.reset_state()
        with cls._correlation_loop_lock:
            cls._correlation_loop_started = False

    # =========================================================================
    # Test Helpers
    # =========================================================================

    @classmethod
    def reset_hydration_state(cls):
        """Hydration state reset (for testing)."""
        MetricHydrator.reset_state()
