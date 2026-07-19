"""G70 — Beat schedule and task registration must stay in sync.

A schedule surface in this tree must either run, or visibly not exist. Two
failure modes have shipped before and this rule closes both:

1. A composed beat entry naming a task nobody registered. Beat accepts the
   entry, the worker never resolves the name, and the lane silently does
   nothing while advertising a cadence.
2. A schedule getter or module-level ``*_SCHEDULE`` constant that no
   composition references. It reads as a wired feature, ships settings and
   docstrings describing its cadence, and never runs.

Resolution half: configure a throwaway Celery app and require every injected
beat entry to resolve against ``app.tasks``. Lanes whose module is absent
inject nothing, so the assertion is vacuously satisfied for them — this is
what keeps the rule green where the private lanes are not installed, rather
than an environment check.

Orphan half: AST-sweep the source tree for schedule-shaped surfaces and
require each to be reachable from the composition table (or explicitly
allowlisted). Both allowlists carry a stated reason per entry — an
unexplained entry is indistinguishable from the wiring this rule catches.

Rule registry: ``ARCHITECTURE.md#g70-schedule-registration-sync``
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.architecture.conftest import (
    DEFAULT_SRC_ROOTS,
    format_violation,
    parse_ast,
    walk_src,
)

_RULE_ANCHOR = "#g70-schedule-registration-sync"

# Composed beat entries whose task is known not to resolve. These are the
# violations this rule was written to surface, kept as an explicit ratchet
# rather than a silent pass: the fix activates automatic canary promotion and
# rollback on installs where the lane has never run, which is a safety call
# that has to be made deliberately, not as a side effect of adding a gate.
# Shrink this set; never grow it. A new entry means a lane was composed
# without registering its task.
_UNRESOLVED_ALLOWLIST: frozenset[str] = frozenset(
    {
        "baldur.tasks.canary_watchdog.scan_zombie_rollouts",
        "baldur.tasks.canary_watchdog.auto_promote_eligible",
        "baldur.tasks.canary_watchdog.collect_canary_metrics",
        "baldur.tasks.postmortem_tasks.postmortem_auto_seal",
    }
)

# Schedule surfaces that are intentionally unreferenced by the composition
# table. An entry here is a claim that operators are expected to wire the lane
# themselves, so each needs a stated reason — an unexplained entry is
# indistinguishable from the dead wiring this rule exists to catch.
_ORPHAN_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Opt-in reporting lane: the daily digest is not composed by default
        # because it emails/pushes on a cadence the operator chooses. Its
        # docstring documents the `.update(get_daily_report_beat_schedule())`
        # wiring, and that surface is a supported operator contract.
        "get_daily_report_beat_schedule",
    }
)


def _composition_module() -> Path:
    """Locate the module holding the composition table and the legacy loader.

    Resolved through the imported package rather than ``PROJECT_ROOT`` so the
    rule works from both checkouts: the private repo consumes ``baldur`` as an
    editable install from a sibling clone and has no ``src/baldur`` of its own.
    """
    from baldur.adapters.celery import beat_schedule

    return Path(beat_schedule.__file__).resolve()


def _composition_source() -> str:
    """Read the module that decides which schedule surfaces get composed."""
    return _composition_module().read_text(encoding="utf-8")


def _iter_schedule_surfaces() -> list[tuple[Path, int, str]]:
    """Find every schedule-shaped surface: getters and module-level constants.

    A getter is ``def get_*_beat_schedule``; a constant is a module-level
    assignment named ``*_SCHEDULE`` bound to a dict literal. Both are the
    shapes a beat composition can consume.
    """
    surfaces: list[tuple[Path, int, str]] = []
    composition_path = _composition_module()
    for path in walk_src(DEFAULT_SRC_ROOTS):
        if path.resolve() == composition_path:
            continue
        tree = parse_ast(path)
        if tree is None:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("get_") and node.name.endswith(
                    "_beat_schedule"
                ):
                    surfaces.append((path, node.lineno, node.name))
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.endswith("_SCHEDULE"):
                        surfaces.append((path, node.lineno, target.id))
    return surfaces


class TestBeatEntriesResolveToRegisteredTasks:
    """G70 — every composed beat entry names a task the app registers."""

    def test_no_unresolvable_beat_entry(self):
        celery = pytest.importorskip("celery")

        from baldur.adapters.celery.beat_schedule import (
            _reset_celery_configured,
            configure_baldur_celery,
        )

        app = celery.Celery("g70_schedule_registration_sync")
        _reset_celery_configured()
        try:
            configure_baldur_celery(app)
        finally:
            _reset_celery_configured()

        registered = set(app.tasks.keys())
        violations = [
            format_violation(
                _RULE_ANCHOR,
                _composition_module(),
                None,
                f"beat entry {entry_name!r} schedules unregistered task "
                f"{entry['task']!r}",
            )
            for entry_name, entry in sorted(app.conf.beat_schedule.items())
            if entry.get("task") not in registered
            and entry.get("task") not in _UNRESOLVED_ALLOWLIST
        ]

        # Ratchet: an allowlisted entry that now resolves must leave the list,
        # so the set cannot quietly outlive the defect it documents.
        stale = sorted(
            task_name for task_name in _UNRESOLVED_ALLOWLIST if task_name in registered
        )
        assert not stale, (
            "G70: these tasks now register — drop them from "
            f"_UNRESOLVED_ALLOWLIST: {stale}"
        )

        assert not violations, (
            f"G70: composed beat entries name unregistered tasks "
            f"({len(violations)}). Either register the task (@shared_task on a "
            "module the lane imports) or drop the beat entry — a scheduled name "
            "the worker cannot resolve is a lane that advertises a cadence and "
            "never runs.\n" + "\n".join(violations)
        )


class TestNoOrphanScheduleSurface:
    """G70 — every schedule surface is reachable from the composition table."""

    def test_no_unreferenced_schedule_surface(self):
        composition = _composition_source()
        violations = [
            format_violation(
                _RULE_ANCHOR,
                path,
                line,
                f"{name} is referenced by no beat composition",
            )
            for path, line, name in _iter_schedule_surfaces()
            if name not in _ORPHAN_ALLOWLIST and name not in composition
        ]

        assert not violations, (
            f"G70: orphan schedule surfaces ({len(violations)}). Add the module "
            "to the composition table so the lane actually runs, delete the "
            "surface, or allowlist it with a reason. An unreferenced schedule "
            "getter advertises a cadence nothing honours.\n" + "\n".join(violations)
        )
