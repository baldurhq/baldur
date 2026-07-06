"""Structural guards over the consolidated Celery Beat schedule.

Two direction-independent regressions, both of which would have caught the
original broken legacy replay entry (a signature-incompatible dispatch routed
to a phantom queue):

1. **Signature bind** — every ``get_baldur_beat_schedule()`` entry that carries
   ``kwargs``/``args`` must bind cleanly against its registered task's ``run``
   signature. The original bug dispatched ``replay_batch_by_domain`` with
   ``kwargs={"max_entries": 50}`` while the task required a positional
   ``domain`` and had no ``max_entries`` param — every fire raised ``TypeError``.
   Covers the whole schedule, not just the legacy lane.
2. **Queue-definedness** — every entry must route to a baldur auto-declared
   queue (``BALDUR_QUEUE_CONFIG``), the Celery ``default``, or a known
   operator-provisioned queue (``dlq_processing`` / ``baldur_recovery``). This
   subsumes the earlier phantom-``dlq`` regression: ``dlq`` is in none of those
   sets, so a re-introduced ``dlq`` route — the original broken-lane bug — fails
   here, alongside any other undeclared-queue typo.
3. **Traffic-aware routing** — the ``traffic-aware-replay`` entry routes to the
   canonical ``dlq_processing`` queue, so enabling it with a ``dlq_processing``
   worker actually drains the DLQ.

PRO/Dormant-lane tasks that fail to resolve (the private wheel is absent, e.g.
the published mirror) are skipped rather than failed — the entry is absent
PRO-absent anyway, and the OSS lanes carry the meaningful coverage.
"""

from __future__ import annotations

import importlib
import inspect

import pytest
from celery import Celery

from baldur.adapters.celery.beat_schedule import (
    _SCHEDULE_MODULES,
    BALDUR_QUEUE_CONFIG,
    get_baldur_beat_schedule,
)

# Queues baldur does NOT auto-declare (absent from BALDUR_QUEUE_CONFIG) but that a
# beat entry may legitimately target because the operator provisions the worker:
# ``dlq_processing`` is the operator DLQ worker's queue, and ``baldur_recovery`` is
# the operator recovery worker's queue (``scan-orphan-sagas`` routes to it, matching
# its task registration). Any OTHER off-config queue is an undeclared-queue typo.
_OPERATOR_QUEUES = frozenset({"dlq_processing", "baldur_recovery"})

# The static schedule is a plain dict literal — enumerating it at collection
# time imports every lane module (their getters live there), which registers
# the ``@shared_task`` functions on the shared registry.
_SCHEDULE = get_baldur_beat_schedule()

# Entries carrying ``kwargs``/``args`` (key presence, not truthiness — an empty
# ``kwargs={}`` binds trivially and stays covered).
_ARG_ENTRIES = [
    (name, tuple(entry.get("args", ())), dict(entry.get("kwargs", {})), entry["task"])
    for name, entry in _SCHEDULE.items()
    if "kwargs" in entry or "args" in entry
]

# Fresh app whose ``.tasks`` carries every ``@shared_task`` (they self-register
# on the shared registry when their module is imported — done above). Never
# connects to a broker. Avoids ``register_all_tasks_with_celery`` so a bare app
# is enough to resolve function tasks by their registered name.
# ``set_as_current=False`` is load-bearing: the default (True) would hijack the
# process/worker's current Celery app, so a co-located test's ``.delay()`` would
# resolve against this broker-less app and raise on connect.
_SHARED_APP = Celery("test_678_beat_schedule_guard", set_as_current=False)

# Class-based tasks (e.g. ``TrafficAwareReplayTask``) carry a custom ``name``
# that is NOT their import path and are not in the shared registry. Map
# name -> class by scanning each schedule module for Task-shaped classes; a
# PRO/Dormant module that fails to import is simply absent (entry then skipped).
_CLASS_TASKS_BY_NAME: dict[str, type] = {}
for _flag, _modpath, _getter, _msg in _SCHEDULE_MODULES:
    try:
        _mod = importlib.import_module(_modpath)
    except ImportError:
        continue
    for _obj in vars(_mod).values():
        _name = getattr(_obj, "name", None)
        if (
            isinstance(_obj, type)
            and isinstance(_name, str)
            and _name
            and hasattr(_obj, "run")
        ):
            _CLASS_TASKS_BY_NAME.setdefault(_name, _obj)


def _resolve_run(task_name: str):
    """Resolve a beat task name to its ``run`` callable, or None if unresolvable.

    Two resolution paths, mirroring the two task shapes: ``@shared_task``
    functions via the shared registry, class-based tasks via the name->class
    scan. None -> the owning lane is absent (PRO/Dormant) and the entry is
    skipped, not failed.
    """
    task = _SHARED_APP.tasks.get(task_name)
    if task is not None:
        return task.run
    cls = _CLASS_TASKS_BY_NAME.get(task_name)
    if cls is not None:
        return cls.run
    return None


def _bindable_signature(run) -> inspect.Signature:
    """Return a ``run`` signature with a leading ``self`` dropped.

    ``@shared_task(bind=True)`` and unbound class ``run`` methods expose
    ``self`` first; dropping it normalizes both so the beat entry's
    ``args``/``kwargs`` bind against the caller-visible params.
    """
    sig = inspect.signature(run)
    params = list(sig.parameters.values())
    if params and params[0].name == "self":
        params = params[1:]
        sig = sig.replace(parameters=params)
    return sig


class TestBeatEntrySignatureBind:
    """Every kwargs/args-carrying entry binds against its task signature."""

    @pytest.mark.parametrize(
        ("name", "args", "kwargs", "task_name"),
        _ARG_ENTRIES,
        ids=[e[0] for e in _ARG_ENTRIES],
    )
    def test_entry_args_bind_registered_task(self, name, args, kwargs, task_name):
        run = _resolve_run(task_name)
        if run is None:
            pytest.skip(
                f"task '{task_name}' (entry '{name}') unresolvable — "
                "PRO/Dormant lane absent; skipped, not failed"
            )
        sig = _bindable_signature(run)
        try:
            sig.bind(*args, **kwargs)
        except TypeError as exc:
            pytest.fail(
                f"beat entry '{name}' -> task '{task_name}': args={args} "
                f"kwargs={kwargs} do not bind {sig}: {exc}"
            )


class TestBeatQueueDefinedness:
    """Every entry routes to a declared or known-operator queue.

    The general form of the queue-consistency guard: an entry's ``options.queue``
    must be a baldur auto-declared queue (``BALDUR_QUEUE_CONFIG``), the Celery
    ``default`` (no explicit queue), or one of the operator-provisioned queues.
    Subsumes the earlier phantom-``dlq`` regression — ``dlq`` is in none of those
    sets, so a re-introduced ``dlq`` route (the original broken-lane bug) fails
    here, as does any other undeclared-queue typo.
    """

    def test_every_entry_routes_to_a_known_queue(self):
        allowed = set(BALDUR_QUEUE_CONFIG) | {"default"} | _OPERATOR_QUEUES
        offenders = {
            name: queue
            for name, entry in _SCHEDULE.items()
            if (queue := entry.get("options", {}).get("queue")) and queue not in allowed
        }
        assert not offenders, (
            f"{len(offenders)} beat entr(y/ies) route to an undeclared queue "
            f"(not in BALDUR_QUEUE_CONFIG, not 'default', not an operator queue "
            f"{sorted(_OPERATOR_QUEUES)}): {offenders}. Add the queue to "
            f"_QUEUE_DEFINITIONS or fix the route."
        )


class TestTrafficAwareRouting:
    """The traffic-aware entry targets the canonical ``dlq_processing`` queue."""

    def test_traffic_aware_routes_to_dlq_processing(self):
        entry = _SCHEDULE.get("traffic-aware-replay")
        assert entry is not None, "traffic-aware-replay entry missing from schedule"
        assert entry.get("options", {}).get("queue") == "dlq_processing"
