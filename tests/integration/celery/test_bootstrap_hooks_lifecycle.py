"""Mock-based integration tests for the Celery worker bootstrap lifecycle.

The receivers are not delegation. ``_on_worker_init`` composes the
``process_utils`` process markers, ``bootstrap.init()``,
``bootstrap.start_background_workers()`` and
``bootstrap.reconcile_celery_deferral_posture()`` over **shared process-global
state** — the serving marker, ``_init_done``, ``_background_starters_deferred``
— and the lane decision one receiver makes is read by a different receiver, in
a different process. That composition is what these tests exercise; the unit
tests pin the receivers' own branches against mocked bootstrap seams.

Two success criteria live only here:

- A Celery-only worker with ``BALDUR_REDIS_URL`` set resolves the redis registry
  default with no user-authored init call — the whole point of the change. The
  negative half is that the pre-init "you skipped init()" WARNING no longer
  fires on a wired worker.
- A production worker with no connection signal exits at ``worker_init``,
  before any fork, rather than booting on memory.

Test Categories:
    A. worker_init lifecycle:
        - redis registry default resolved through the receiver-driven init()
        - pre-init WARNING positive control + wired-worker negative
        - fork lane defers the starters; non-fork lane serves in place
        - production misconfig exits at worker_init, before any fork
    B. worker_process_init lifecycle:
        - the child un-defers the starters its parent deferred
        - an inherited init() is not re-run over shared connection pools

No infrastructure: ``BALDUR_REDIS_URL`` set without a live Redis is enough,
because ``init()`` performs no I/O probe by design. Real fork-child
thread liveness is not expressible here — ``os.fork`` is absent on the Windows
dev box — and rides the Linux scenario lane.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import DEFAULT, MagicMock, patch

import pytest

import baldur.bootstrap as bootstrap_module
import baldur.factory.registry as registry_module
from baldur.adapters.celery.bootstrap_hooks import (
    _on_worker_init,
    _on_worker_process_init,
)
from baldur.core import process_utils
from baldur.core.exceptions import ConfigurationError
from baldur.factory.registry import ProviderRegistry

_SERVING_ENV_VAR = process_utils._CELERY_WORKER_SERVING_ENV_VAR


@pytest.fixture(autouse=True)
def _isolated_worker_process(monkeypatch):
    """Start and end each test as a fresh, unmarked, uninitialized process."""
    original_main = process_utils._celery_worker_main
    process_utils._celery_worker_main = False
    monkeypatch.delenv(_SERVING_ENV_VAR, raising=False)

    bootstrap_module.reset_init_state()
    with ProviderRegistry.cache.snapshot():
        yield
    bootstrap_module.reset_init_state()
    process_utils._celery_worker_main = original_main


@pytest.fixture
def celery_only_production_env(monkeypatch, tmp_path):
    """A Celery-only production deployment configured through env vars alone.

    This is the deployment shape the change exists for: no Django adapter, no
    gunicorn hook, no user-authored receiver — only the environment and the
    worker's own signals.
    """
    monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
    monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
    monkeypatch.setenv("BALDUR_REDIS_URL", "redis://celery-broker:6379/0")
    # Production wiring also requires a SQL signal so the registry step's
    # Group B does not raise; no connection is opened by init().
    monkeypatch.setenv("BALDUR_SQL_DSN", "postgresql://stub/db")
    monkeypatch.setenv("BALDUR_WAL_DIR", str(tmp_path / "wal"))


class _StorageBackendStub:
    """Stand-in for the eagerly constructed storage backend.

    A hand-written double rather than a mock: the production boot gate reads
    four plain attributes off it, and a spec-carrying mock cannot expose the
    private instance attributes at all. What is under test here is the registry
    default the wiring step selects, not the backend's own construction.
    """

    def __init__(self, wal_dir: str) -> None:
        self._wal_initialized = True
        self._wal_on_fallback_dir = False
        self._wal_honors_configured_dir = True
        self.config = SimpleNamespace(wal_dir=wal_dir)


@pytest.fixture
def eager_backend_stub(tmp_path):
    """Install the backend double and hand back the install seam.

    The yielded mock is ``configure_storage_backend``, because the question a
    test asks about it is how many times the *install* ran — once per process
    that fully initializes, and not again in a child whose ``init()`` was
    inherited.
    """
    from baldur.adapters.resilient import backend as backend_module

    backend = _StorageBackendStub(str(tmp_path))
    configure_fn = MagicMock(backend_module.configure_storage_backend)

    with patch.multiple(
        "baldur.adapters.resilient.backend",
        ResilientStorageBackend=MagicMock(
            backend_module.ResilientStorageBackend, return_value=backend
        ),
        configure_storage_backend=configure_fn,
    ):
        yield configure_fn


@pytest.fixture
def quiet_init_side_effects():
    """Isolate the wiring under test from init()'s unrelated side effects.

    Same scaffold the framework-agnostic init() integration tests use: event
    handlers, shutdown handlers, the scheduler and the admin server are not what
    these tests assert on, and letting them run would put sockets and threads
    behind an assertion about a registry default.
    """
    with patch.multiple(
        bootstrap_module,
        autospec=True,
        _validate_startup_config=DEFAULT,
        _register_default_event_handlers=DEFAULT,
        _init_bridge_instrumentation=DEFAULT,
        _register_shutdown_handlers=DEFAULT,
        _run_pro_extensions=DEFAULT,
        _apply_audit_default_provider=DEFAULT,
        _start_audit_pipeline_if_enabled=DEFAULT,
        _start_dlq_outbox_if_enabled=DEFAULT,
        _record_env_snapshot=DEFAULT,
        _start_default_scheduler=DEFAULT,
        _register_sql_statistics_if_available=DEFAULT,
        _start_admin_server_if_enabled=DEFAULT,
    ) as stubs:
        stubs["_run_pro_extensions"].return_value = bootstrap_module.ExtensionResult()
        yield stubs


@pytest.fixture
def recorded_starters(monkeypatch):
    """Replace the production starter tuple with one recording stub.

    The starters themselves are covered by their own tests; here the question is
    only whether this process's posture let them run, and the real tuple would
    put a dozen daemon threads behind that assertion.
    """
    ran: list[bool] = []
    monkeypatch.setattr(
        bootstrap_module,
        "_BACKGROUND_WORKER_STARTERS",
        (lambda: ran.append(process_utils.is_fork_source_process()),),
    )
    return ran


class TestCeleryWorkerInitLifecycleIntegration:
    """``worker_init`` drives a real ``init()`` in the worker's main process."""

    def test_worker_init_resolves_the_redis_registry_default(
        self, celery_only_production_env, eager_backend_stub, quiet_init_side_effects
    ):
        """SC1 — ``BALDUR_REDIS_URL`` is read with no user-authored init call.

        Before this change a Celery-only worker ran on module-load defaults, so
        the registry stayed on memory and circuit-breaker and idempotency state
        diverged per worker — a correctness failure, not a scale one.
        """
        _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))

        assert ProviderRegistry.cache.get_default_name() == "redis"
        assert bootstrap_module._init_done is True

    def test_get_cache_before_any_init_reports_the_missing_init(
        self, celery_only_production_env
    ):
        """Positive control for the negative assertion below.

        With Redis explicitly configured, a pre-init registry access discards
        the operator's configuration, and the framework says so at WARNING.
        """
        with patch.object(registry_module, "logger") as logger:
            ProviderRegistry.get_cache("memory")

        warned = [call.args[0] for call in logger.warning.call_args_list if call.args]
        assert "baldur.init_not_called_get_cache" in warned

    def test_wired_worker_no_longer_reports_the_missing_init(
        self, celery_only_production_env, eager_backend_stub, quiet_init_side_effects
    ):
        """SC1 negative — the pre-init categorization stops occurring.

        Same access as the control above, on a worker the receiver initialized.
        """
        _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))

        with patch.object(registry_module, "logger") as logger:
            ProviderRegistry.get_cache("memory")

        warned = [call.args[0] for call in logger.warning.call_args_list if call.args]
        assert "baldur.init_not_called_get_cache" not in warned

    def test_forking_pool_leaves_the_starters_to_its_children(
        self,
        celery_only_production_env,
        eager_backend_stub,
        quiet_init_side_effects,
        recorded_starters,
    ):
        """The composed state: the receiver's flag makes ``init()`` defer.

        The receiver marks this process as a Celery worker main *before*
        ``init()`` runs, so the fork-source predicate the starters consult
        answers True and every start is suppressed here.
        """
        _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))

        assert bootstrap_module._background_starters_deferred is True
        assert recorded_starters == [True]
        assert process_utils.is_celery_worker_serving() is False

    def test_non_forking_pool_serves_and_starts_in_place(
        self,
        celery_only_production_env,
        eager_backend_stub,
        quiet_init_side_effects,
        recorded_starters,
    ):
        """The solo/threads/gevent lane: no children to delegate to.

        The serving marker set before ``init()`` is what flips the same
        predicate the other direction, in the same process.
        """
        _on_worker_init(sender=SimpleNamespace(pool_cls="solo"))

        assert process_utils.is_celery_worker_serving() is True
        assert bootstrap_module._background_starters_deferred is False
        # init()'s own pass plus the receiver's explicit un-deferring call.
        assert recorded_starters == [False, False]

    def test_production_worker_without_redis_url_exits_before_forking(
        self, monkeypatch, tmp_path, quiet_init_side_effects
    ):
        """SC5 — fail loud in the main process, not per child.

        ``Signal.send`` catches ``Exception``, so the raising ``init()`` alone
        cannot stop a boot; ``SystemExit`` is what propagates through the send.
        This lane fires before any fork, so one abort covers every worker
        process the pool would have created.
        """
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.setenv("BALDUR_WAL_DIR", str(tmp_path / "wal"))

        with pytest.raises(SystemExit) as exc_info:
            _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))

        assert "BALDUR_REDIS_URL" in str(exc_info.value)
        assert bootstrap_module._init_done is False

    def test_the_underlying_init_failure_is_a_configuration_error(
        self, monkeypatch, tmp_path, quiet_init_side_effects
    ):
        """Proximate cause: the ``SystemExit`` above is the conversion, not a
        coincidence — the same environment makes ``init()`` itself raise."""
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.setenv("BALDUR_WAL_DIR", str(tmp_path / "wal"))

        with pytest.raises(ConfigurationError, match="BALDUR_REDIS_URL"):
            bootstrap_module.init()


class TestCeleryWorkerProcessInitLifecycleIntegration:
    """``worker_process_init`` reads the posture a different receiver decided."""

    def test_pool_child_undefers_the_starters_its_parent_deferred(
        self,
        celery_only_production_env,
        eager_backend_stub,
        quiet_init_side_effects,
        recorded_starters,
    ):
        """The end-to-end claim: nothing is lost across the two receivers.

        In production these run in two processes; in one process the same
        globals carry the decision, which is what makes the composition
        assertable at all. The parent deferred; the child marks itself serving,
        finds ``init()`` already done, and starts them.
        """
        # Given — a prefork worker main that deferred by design
        _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))
        assert bootstrap_module._background_starters_deferred is True
        recorded_starters.clear()

        # When — the pool child's own lane runs
        _on_worker_process_init()

        # Then
        assert process_utils.is_celery_worker_serving() is True
        assert bootstrap_module._background_starters_deferred is False
        assert recorded_starters == [False]

    def test_pool_child_does_not_re_initialize_an_inherited_init(
        self,
        celery_only_production_env,
        eager_backend_stub,
        quiet_init_side_effects,
    ):
        """``init()`` is idempotent, and the child's call is the no-op arm.

        Recovery in the child is ``start_background_workers()``, never a reset —
        the reset drains the WAL and closes storage pools whose sockets the fork
        shares with the parent.
        """
        _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))
        eager_backend_stub.assert_called_once()

        _on_worker_process_init()

        assert bootstrap_module._init_done is True
        # The storage backend is installed once per process that fully
        # initializes. A second install here would mean the child re-ran init()
        # over connection pools the fork shares with its parent.
        eager_backend_stub.assert_called_once()
        assert ProviderRegistry.cache.get_default_name() == "redis"
