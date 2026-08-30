"""Unit tests for the Celery worker bootstrap receivers.

The adapter's ``baldur.init()`` call lives in two receivers, and almost every
rule they encode looks arbitrary until the failure it prevents is named:

- ``worker_init`` decides where background threads may live from the pool the
  worker is about to build. Guessing "non-forking" for a pool whose shape is
  unknowable would start services in a process that then forks children with
  none — so an out-of-tree pool is deferred and said so.
- The pool classifier answers aliases from the string. An ``issubclass``
  against an imported prefork pool would pull asynpool and billiard into a
  gevent/eventlet interpreter celery itself never takes there.
- ``worker_process_init`` marks the process as serving *before* it initializes.
  Billiard's spawn path restores the parent's ``sys.argv`` into the child, so
  the argv predicate reads "worker main" there; with the marker set second,
  every starter in that child would skip and never be asked again.
- Fail-loud is ``SystemExit`` and only from ``worker_init``. ``Signal.send``
  catches ``Exception``, so a raising receiver cannot stop a boot — and a
  child-side abort on a condition that appeared after the parent booted clean
  is a kill-and-respawn loop rather than an abort.

Mock points are the two ``baldur.bootstrap`` module functions the receivers
import at call time, which is the seam the design nominates.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from celery.signals import worker_init, worker_process_init
from celery.utils.dispatch import Signal

import baldur.bootstrap as bootstrap_module
from baldur.adapters.celery import bootstrap_hooks
from baldur.adapters.celery.bootstrap_hooks import (
    _classify_pool,
    _on_worker_init,
    _on_worker_process_init,
    _PoolLane,
    connect_celery_bootstrap_receivers,
    disconnect_celery_bootstrap_receivers,
    is_celery_bootstrap_receivers_connected,
)
from baldur.core import process_utils
from baldur.core.exceptions import ConfigurationError

_SERVING_ENV_VAR = process_utils._CELERY_WORKER_SERVING_ENV_VAR


def _receiver_count(signal, dispatch_uid: str) -> int:
    """How many receivers ``signal`` holds under ``dispatch_uid``.

    Celery keys its receiver table on ``(dispatch_uid, sender_id)``, so this is
    what "deduplicated across the three arming sites" has to mean.
    """
    return sum(1 for key, _ in signal.receivers if key[0] == dispatch_uid)


@pytest.fixture
def clean_process_markers(monkeypatch):
    """Reset both celery process markers around a test.

    Both are process-global: the worker-main flag is a module global the
    receiver sets permanently, the serving marker an env var. A test that left
    either set would make every later test in the same worker look like a
    Celery worker process.
    """
    original_main = process_utils._celery_worker_main
    process_utils._celery_worker_main = False
    monkeypatch.delenv(_SERVING_ENV_VAR, raising=False)
    try:
        yield
    finally:
        process_utils._celery_worker_main = original_main


@pytest.fixture
def celery_app_stub():
    """Celery app double with the ``conf`` surface ``configure_baldur_celery`` writes.

    A real ``Celery()`` is not needed — the entry point under test only merges
    schedule/queue/route configuration and then arms the receivers, and building
    a real app would pull a broker configuration into a unit test. A namespace
    rather than a mock: the three attributes are read *and* written, and nothing
    here asserts on calls.
    """
    return SimpleNamespace(
        conf=SimpleNamespace(beat_schedule={}, task_queues=[], task_routes={})
    )


@pytest.fixture
def receivers_disconnected():
    """Guarantee a clean receiver table before and after a registration test."""
    disconnect_celery_bootstrap_receivers()
    try:
        yield
    finally:
        disconnect_celery_bootstrap_receivers()


class _OutOfTreePool:
    """A pool class defined outside ``celery.concurrency`` — shape unknowable."""


class TestPoolClassifierBehavior:
    """``_classify_pool`` — alias fast path, MRO inspection, conservative default.

    ``worker_init`` fires before celery resolves ``pool_cls``, so the classifier
    sees whatever the operator or app config supplied: an alias string, a dotted
    path, or a class object.
    """

    @pytest.mark.parametrize(
        ("alias", "expected"),
        [
            ("prefork", _PoolLane.FORK),
            ("processes", _PoolLane.FORK),
            ("  PreFork  ", _PoolLane.FORK),
            ("solo", _PoolLane.NON_FORK),
            ("threads", _PoolLane.NON_FORK),
            ("gevent", _PoolLane.NON_FORK),
            ("eventlet", _PoolLane.NON_FORK),
        ],
        ids=[
            "prefork",
            "processes_compat_alias",
            "case_and_whitespace_insensitive",
            "solo",
            "threads",
            "gevent",
            "eventlet",
        ],
    )
    def test_alias_string_resolves_to_its_lane(self, alias, expected):
        """Celery's own alias table decides; the string answers on its own."""
        assert _classify_pool(alias) is expected

    def test_alias_classification_does_not_import_the_prefork_pool(self, monkeypatch):
        """The import-free fast path is the point, not an optimization.

        Under ``-P gevent`` / ``-P eventlet`` the interpreter is monkey-patched
        and celery never imports the prefork module; importing it here (and
        through it asynpool and billiard) would drag that import set in behind
        celery's back.
        """
        # Given — the prefork module is provably not loaded in this process
        monkeypatch.delitem(sys.modules, "celery.concurrency.prefork", raising=False)

        # When — every alias the fast path claims to answer
        for alias in ("prefork", "processes", "solo", "threads", "gevent", "eventlet"):
            _classify_pool(alias)

        # Then
        assert "celery.concurrency.prefork" not in sys.modules

    def test_dotted_path_to_the_prefork_pool_is_the_fork_lane(self):
        """A dotted path resolves, then classifies by MRO module names."""
        assert _classify_pool("celery.concurrency.prefork:TaskPool") is _PoolLane.FORK

    def test_dotted_path_to_an_in_tree_non_forking_pool_is_the_non_fork_lane(self):
        """Resolved under ``celery.concurrency.*`` without prefork on the MRO."""
        assert _classify_pool("celery.concurrency.solo:TaskPool") is _PoolLane.NON_FORK

    def test_class_object_is_classified_without_a_dotted_path(self):
        """``pool_cls`` can already be a class when the app config set one."""
        from celery.concurrency.solo import TaskPool as SoloTaskPool

        assert _classify_pool(SoloTaskPool) is _PoolLane.NON_FORK

    def test_out_of_tree_pool_class_is_unknown(self):
        """``CELERY_CUSTOM_WORKER_POOL`` may or may not fork — refuse to guess.

        A forking custom pool classified "non-forking" would start services in
        the worker main and hand its children none, which is the failure the
        whole module exists to remove.
        """
        assert _classify_pool(_OutOfTreePool) is _PoolLane.UNKNOWN

    def test_unresolvable_pool_reference_is_unknown(self):
        """Resolution failure falls toward the conservative lane, not a lane guess."""
        assert _classify_pool("no.such.module:TaskPool") is _PoolLane.UNKNOWN

    def test_missing_pool_reference_is_unknown(self):
        """``sender.pool_cls`` absent (``None``) is not a non-forking pool."""
        assert _classify_pool(None) is _PoolLane.UNKNOWN


class TestWorkerInitReceiverBehavior:
    """``_on_worker_init`` — the universal main-process lane.

    Order is pinned inside the receiver: mark the worker main first (so
    ``init()`` sees signal-based truth rather than the argv fallback), reconcile
    the earlier posture while it still describes any earlier initialization,
    then act on the lane.
    """

    @pytest.fixture
    def receiver_seams(self, clean_process_markers):
        """Patch the three bootstrap functions the receiver imports at call time."""
        with (
            patch.object(bootstrap_module, "init", autospec=True) as init,
            patch.object(
                bootstrap_module, "start_background_workers", autospec=True
            ) as starters,
            patch.object(
                bootstrap_module,
                "reconcile_celery_deferral_posture",
                autospec=True,
            ) as reconcile,
            patch.object(bootstrap_hooks, "logger") as logger,
        ):
            yield SimpleNamespace(
                init=init,
                starters=starters,
                reconcile=reconcile,
                logger=logger,
            )

    def test_worker_main_flag_is_set_before_init_runs(self, receiver_seams):
        """Signal-based truth first, so ``init()`` never falls back to argv."""
        observed: list[bool] = []
        receiver_seams.init.side_effect = lambda: observed.append(
            process_utils.is_celery_worker_main()
        )

        _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))

        assert observed == [True]

    def test_forking_pool_initializes_without_starting_background_workers(
        self, receiver_seams
    ):
        """The fork source builds no starter state the fork would kill."""
        _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))

        receiver_seams.init.assert_called_once_with()
        receiver_seams.starters.assert_not_called()

    def test_forking_pool_does_not_mark_this_process_as_serving(self, receiver_seams):
        """The prefork parent never marks itself, so children inherit it unset."""
        _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))

        assert process_utils.is_celery_worker_serving() is False

    def test_forking_pool_logs_the_delegation_posture(self, receiver_seams):
        """Deferring here is the designed posture, and it is said out loud."""
        _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))

        events = [
            call.args[0]
            for call in receiver_seams.logger.info.call_args_list
            if call.args
        ]
        assert "celery.background_workers_delegated" in events
        receiver_seams.logger.warning.assert_not_called()

    def test_non_forking_pool_marks_serving_before_init(self, receiver_seams):
        """The marker has to be visible to the ``init()`` this receiver drives."""
        observed: list[bool] = []
        receiver_seams.init.side_effect = lambda: observed.append(
            process_utils.is_celery_worker_serving()
        )

        _on_worker_init(sender=SimpleNamespace(pool_cls="solo"))

        assert observed == [True]

    def test_non_forking_pool_starts_the_background_workers_explicitly(
        self, receiver_seams
    ):
        """Explicit, not implied by ``init()``.

        On the Django-fixup path ``init()`` already ran with the starters
        deferred and is a no-op here, so this call is what un-defers them.
        """
        _on_worker_init(sender=SimpleNamespace(pool_cls="solo"))

        receiver_seams.init.assert_called_once_with()
        receiver_seams.starters.assert_called_once_with()

    def test_unknown_pool_defers_the_starters_and_warns(self, receiver_seams):
        """A pool whose fork shape is unknowable gets today's behavior plus a WARNING.

        Emitted at classification time because a non-forking custom pool never
        sends ``worker_process_init`` — the deferral is permanent by design and
        nothing later would report it.
        """
        _on_worker_init(sender=SimpleNamespace(pool_cls=_OutOfTreePool))

        receiver_seams.starters.assert_not_called()
        assert process_utils.is_celery_worker_serving() is False
        warned = [
            call.args[0]
            for call in receiver_seams.logger.warning.call_args_list
            if call.args
        ]
        assert warned == ["celery.background_workers_not_started"]

    def test_absent_sender_is_treated_as_an_unknown_pool(self, receiver_seams):
        """No sender means no ``pool_cls``, which is not evidence of non-forking."""
        _on_worker_init()

        receiver_seams.starters.assert_not_called()
        assert (
            receiver_seams.logger.warning.call_args_list[0].args[0]
            == "celery.background_workers_not_started"
        )

    @pytest.mark.parametrize(
        ("pool_cls", "fork_lane"),
        [("prefork", True), ("solo", False), (_OutOfTreePool, False)],
        ids=["fork", "non_fork", "unknown"],
    )
    def test_posture_reconciliation_receives_the_classified_lane(
        self, receiver_seams, pool_cls, fork_lane
    ):
        """The reconciliation only reports the pre-fork-threads posture on a fork lane."""
        _on_worker_init(sender=SimpleNamespace(pool_cls=pool_cls))

        receiver_seams.reconcile.assert_called_once_with(fork_lane=fork_lane)

    def test_reconciliation_runs_before_init(self, receiver_seams):
        """It reads the state of any *earlier* initialization, so it must precede.

        The Django-fixup path initializes at app-module import; reconciling
        after this receiver's own ``init()`` would read that instead.
        """
        order: list[str] = []
        receiver_seams.reconcile.side_effect = lambda **_: order.append("reconcile")
        receiver_seams.init.side_effect = lambda: order.append("init")

        _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))

        assert order == ["reconcile", "init"]

    def test_configuration_error_becomes_system_exit(self, receiver_seams):
        """``Signal.send`` catches ``Exception``; only a ``BaseException`` aborts.

        Production misconfiguration must stop the worker rather than let it come
        up on in-process defaults — the silent-wrong outcome ``init()`` raises
        to prevent.
        """
        receiver_seams.init.side_effect = ConfigurationError("BALDUR_REDIS_URL unset")

        with pytest.raises(SystemExit) as exc_info:
            _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))

        assert "BALDUR_REDIS_URL unset" in str(exc_info.value)

    def test_configuration_error_aborts_before_the_starters_run(self, receiver_seams):
        """Nothing downstream of the failed ``init()`` executes."""
        receiver_seams.init.side_effect = ConfigurationError("bad config")

        with pytest.raises(SystemExit):
            _on_worker_init(sender=SimpleNamespace(pool_cls="solo"))

        receiver_seams.starters.assert_not_called()

    def test_unexpected_exception_propagates_unchanged(self, receiver_seams):
        """No blanket try/except: anything else is left to ``Signal.send``.

        The worker then boots on the documented pre-init semantics rather than
        not at all — a fail-open direction that a catch-all here would take away.
        """
        receiver_seams.init.side_effect = KeyError("something else entirely")

        with pytest.raises(KeyError):
            _on_worker_init(sender=SimpleNamespace(pool_cls="prefork"))


class TestWorkerProcessInitReceiverBehavior:
    """``_on_worker_process_init`` — every process that runs tasks.

    Prefork children (``maxtasksperchild`` replacements included) and the solo
    pool's own main process. The two halves that look arbitrary are the pinned
    marker-then-init order and the ``ConfigurationError`` that logs instead of
    exiting.
    """

    @pytest.fixture
    def receiver_seams(self, clean_process_markers):
        """Patch the two bootstrap functions plus the Django extras hook."""
        with (
            patch.object(bootstrap_module, "init", autospec=True) as init,
            patch.object(
                bootstrap_module, "start_background_workers", autospec=True
            ) as starters,
            patch(
                "baldur.adapters.django.apps.BaldurConfig.start_background_threads",
                autospec=True,
            ) as django_threads,
            patch.object(bootstrap_hooks, "logger") as logger,
        ):
            yield SimpleNamespace(
                init=init,
                starters=starters,
                django_threads=django_threads,
                logger=logger,
            )

    def test_serving_marker_is_set_before_init_runs(self, receiver_seams):
        """Billiard's spawn path restores the parent's argv into the child.

        The argv predicate therefore reads "celery worker main" there. With the
        marker already set the composed fork-source predicate answers False
        regardless; set afterwards, every starter in this child would skip and
        never be asked again.
        """
        observed: list[bool] = []
        receiver_seams.init.side_effect = lambda: observed.append(
            process_utils.is_celery_worker_serving()
        )

        _on_worker_process_init()

        assert observed == [True]

    def test_fork_source_predicate_is_false_for_this_process_at_init_time(
        self, receiver_seams
    ):
        """The marker's whole purpose, asserted through the predicate itself."""
        observed: list[bool] = []
        receiver_seams.init.side_effect = lambda: observed.append(
            process_utils.is_fork_source_process()
        )
        process_utils.mark_celery_worker_main()  # inherited from the pool parent

        _on_worker_process_init()

        assert observed == [False]

    def test_initializes_then_starts_the_background_workers(self, receiver_seams):
        """``init()`` is an inherited-``_init_done`` no-op under fork; the starters are not."""
        _on_worker_process_init()

        receiver_seams.init.assert_called_once_with()
        receiver_seams.starters.assert_called_once_with()

    def test_new_serving_process_starts_the_django_intrinsic_threads(
        self, receiver_seams
    ):
        """A fresh child's Django correlation loop died at the fork.

        ``start_background_workers()`` deliberately does not own it, so the
        receiver makes the same call gunicorn's ``post_worker_init`` hook does.
        """
        _on_worker_process_init()

        receiver_seams.django_threads.assert_called_once()

    def test_already_serving_process_skips_the_django_intrinsic_threads(
        self, receiver_seams
    ):
        """The solo pool's main process is the one ``worker_init`` just handled.

        ``start_background_threads()`` resets its own duplicate-start guards,
        which is what a fresh child needs and exactly why it must not run in a
        process that was already serving.
        """
        process_utils.mark_celery_worker_serving()

        _on_worker_process_init()

        receiver_seams.django_threads.assert_not_called()

    def test_configuration_error_is_logged_without_exiting(self, receiver_seams):
        """A child-side abort would be a kill-and-respawn loop, not an abort.

        The main process already had its chance to fail loudly, at
        ``worker_init``, before any child existed.
        """
        receiver_seams.init.side_effect = ConfigurationError("disk full")

        _on_worker_process_init()

        assert receiver_seams.logger.exception.call_args.args[0] == (
            "celery.worker_process_init_error"
        )

    def test_configuration_error_stops_the_starters_in_this_child(self, receiver_seams):
        """Fail-open to pre-init semantics means returning, not continuing."""
        receiver_seams.init.side_effect = ConfigurationError("disk full")

        _on_worker_process_init()

        receiver_seams.starters.assert_not_called()
        receiver_seams.django_threads.assert_not_called()

    def test_unexpected_exception_propagates_unchanged(self, receiver_seams):
        """Only ``ConfigurationError`` is handled; the rest is ``Signal.send``'s."""
        receiver_seams.init.side_effect = KeyError("something else entirely")

        with pytest.raises(KeyError):
            _on_worker_process_init()

    def test_missing_django_adapter_does_not_break_the_child(self, receiver_seams):
        """A Celery-only deployment has no Django adapter to call into."""
        receiver_seams.django_threads.side_effect = ImportError("no django here")

        _on_worker_process_init()

        receiver_seams.starters.assert_called_once_with()


class TestBootstrapReceiverRegistrationContract:
    """Three arming sites, one registration per signal.

    ``setup_baldur_signals()``, ``configure_baldur_celery()`` and ``init()``
    itself all connect the same receivers — the last one for Django+Celery
    deployments that call neither adapter entry point. The ``dispatch_uid`` is
    what collapses them: celery keys its receiver table on
    ``(dispatch_uid, sender_id)``.
    """

    def test_connect_registers_one_receiver_on_each_signal(
        self, receivers_disconnected
    ):
        """Both lanes are armed by the one call."""
        connect_celery_bootstrap_receivers()

        assert (
            _receiver_count(worker_init, bootstrap_hooks._WORKER_INIT_DISPATCH_UID) == 1
        )
        assert (
            _receiver_count(
                worker_process_init,
                bootstrap_hooks._WORKER_PROCESS_INIT_DISPATCH_UID,
            )
            == 1
        )

    def test_repeated_connects_leave_exactly_one_receiver_per_signal(
        self, receivers_disconnected
    ):
        """Arming three times is what production does; it must register once."""
        connect_celery_bootstrap_receivers()
        connect_celery_bootstrap_receivers()
        connect_celery_bootstrap_receivers()

        assert (
            _receiver_count(worker_init, bootstrap_hooks._WORKER_INIT_DISPATCH_UID) == 1
        )
        assert (
            _receiver_count(
                worker_process_init,
                bootstrap_hooks._WORKER_PROCESS_INIT_DISPATCH_UID,
            )
            == 1
        )

    def test_is_connected_reports_false_until_both_signals_are_armed(
        self, receivers_disconnected
    ):
        """The predicate ``init()``'s deferral warning reads for its remedy line."""
        assert is_celery_bootstrap_receivers_connected() is False

        connect_celery_bootstrap_receivers()

        assert is_celery_bootstrap_receivers_connected() is True

    def test_disconnect_removes_both_receivers(self, receivers_disconnected):
        """Test isolation and adapter teardown depend on both lanes clearing."""
        connect_celery_bootstrap_receivers()

        disconnect_celery_bootstrap_receivers()

        assert is_celery_bootstrap_receivers_connected() is False
        assert (
            _receiver_count(worker_init, bootstrap_hooks._WORKER_INIT_DISPATCH_UID) == 0
        )
        assert (
            _receiver_count(
                worker_process_init,
                bootstrap_hooks._WORKER_PROCESS_INIT_DISPATCH_UID,
            )
            == 0
        )

    def test_setup_baldur_signals_arms_the_bootstrap_receivers(
        self, receivers_disconnected
    ):
        """Closes the sweep-found gap: nothing asserted what this entry point connects."""
        from baldur.adapters.celery.signal_hooks import (
            disconnect_baldur_signals,
            setup_baldur_signals,
        )

        try:
            setup_baldur_signals()

            assert is_celery_bootstrap_receivers_connected() is True
        finally:
            disconnect_baldur_signals()

    def test_disconnect_baldur_signals_removes_the_bootstrap_receivers(
        self, receivers_disconnected
    ):
        """The adapter teardown owns everything the setup connected."""
        from baldur.adapters.celery.signal_hooks import (
            disconnect_baldur_signals,
            setup_baldur_signals,
        )

        setup_baldur_signals()
        disconnect_baldur_signals()

        assert is_celery_bootstrap_receivers_connected() is False

    def test_configure_baldur_celery_arms_the_bootstrap_receivers(
        self, receivers_disconnected, celery_app_stub
    ):
        """An app that reaches Baldur only through the beat wiring still initializes."""
        from baldur.adapters.celery.beat_schedule import (
            _reset_celery_configured,
            configure_baldur_celery,
        )

        try:
            with patch(
                "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
                autospec=True,
            ):
                configure_baldur_celery(celery_app_stub)

            assert is_celery_bootstrap_receivers_connected() is True
        finally:
            _reset_celery_configured()

    def test_celery_configured_reset_disconnects_the_bootstrap_receivers(
        self, receivers_disconnected, celery_app_stub
    ):
        """The receivers are connected inside the guarded body, so the reset owns them.

        Leaving them registered would let a later test's signal reach a receiver
        this reset was supposed to have removed.
        """
        from baldur.adapters.celery.beat_schedule import (
            _reset_celery_configured,
            configure_baldur_celery,
        )

        with patch(
            "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
            autospec=True,
        ):
            configure_baldur_celery(celery_app_stub)

        _reset_celery_configured()

        assert is_celery_bootstrap_receivers_connected() is False

    def test_all_three_arming_sites_together_leave_one_receiver_per_signal(
        self, receivers_disconnected, celery_app_stub, clean_process_markers
    ):
        """The dedup claim, exercised against the sites production actually uses."""
        from baldur.adapters.celery.beat_schedule import (
            _reset_celery_configured,
            configure_baldur_celery,
        )
        from baldur.adapters.celery.signal_hooks import (
            disconnect_baldur_signals,
            setup_baldur_signals,
        )

        try:
            # Given / When — all three sites arm, in one process
            setup_baldur_signals()
            with patch(
                "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery",
                autospec=True,
            ):
                configure_baldur_celery(celery_app_stub)
            with patch(
                "baldur.core.process_utils.is_celery_worker_process",
                return_value=True,
            ):
                bootstrap_module._arm_celery_bootstrap_receivers()

            # Then
            assert (
                _receiver_count(worker_init, bootstrap_hooks._WORKER_INIT_DISPATCH_UID)
                == 1
            )
            assert (
                _receiver_count(
                    worker_process_init,
                    bootstrap_hooks._WORKER_PROCESS_INIT_DISPATCH_UID,
                )
                == 1
            )
        finally:
            _reset_celery_configured()
            disconnect_baldur_signals()


class TestCelerySignalDispatchContract:
    """Pin the celery dispatch behavior the fail-loud channel rests on.

    ``Signal.send`` swallowing ``Exception`` is why the receiver converts
    ``ConfigurationError`` to ``SystemExit`` at all. It is upstream behavior
    that can drift across celery upgrades, so it is asserted here rather than
    assumed.
    """

    def test_receiver_exception_does_not_stop_the_send(self):
        """Swallowed and logged — a receiver cannot fail a worker boot by raising."""
        signal = Signal(name="baldur_test_signal", providing_args=[])
        reached: list[str] = []

        def _raising(**kwargs):
            reached.append("raised")
            raise RuntimeError("receiver blew up")

        signal.connect(_raising, dispatch_uid="baldur.test.exception")
        try:
            responses = signal.send(sender=None)
        finally:
            signal.disconnect(_raising, dispatch_uid="baldur.test.exception")

        assert reached == ["raised"]
        assert len(responses) == 1
        assert isinstance(responses[0][1], RuntimeError)

    def test_receiver_system_exit_propagates_through_the_send(self):
        """``SystemExit`` is a ``BaseException``: it passes the handler.

        This is the only startup-blocking channel a receiver has.
        """
        signal = Signal(name="baldur_test_signal_exit", providing_args=[])

        def _exiting(**kwargs):
            raise SystemExit("abort the boot")

        signal.connect(_exiting, dispatch_uid="baldur.test.system_exit")
        try:
            with pytest.raises(SystemExit) as exc_info:
                signal.send(sender=None)
        finally:
            signal.disconnect(_exiting, dispatch_uid="baldur.test.system_exit")

        assert "abort the boot" in str(exc_info.value)
