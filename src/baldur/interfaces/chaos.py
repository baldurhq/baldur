"""Chaos Service Interfaces (519 PR 2 / D-c1).

OSS-side Protocols for PRO chaos singletons (Scheduler, ReportGenerator,
SafetyGuard). PRO ships realized implementations behind
``baldur_pro.services.chaos``; OSS callers resolve via
``ProviderRegistry.<slot>.safe_get()`` and use the returned instance with
a None-guard.

Methods are Interface Segregation — only those OSS code currently calls
are declared.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["ChaosScheduler", "ReportGenerator", "SafetyGuard"]


@runtime_checkable
class ChaosScheduler(Protocol):
    """Protocol for the PRO chaos experiment scheduler."""

    def list_schedules(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def get_due_experiments(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def cleanup_zombie_experiment(self, *args: Any, **kwargs: Any) -> Any: ...

    def unregister_experiment_instance(self, *args: Any, **kwargs: Any) -> Any: ...

    # Returns a ``dict[schedule_id, RunningExperimentInfo]`` on PRO impl.
    def get_running_experiments(self) -> dict[str, Any]: ...

    def get_experiments_by_status(self, status: Any) -> list[Any]: ...

    def get_execution_history(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def expire_pending_approvals(self) -> int: ...

    # PRO-internal experiment lookup, exposed for the OSS chaos health probe.
    def _get_experiment_instance(self, experiment_id: str) -> Any | None: ...


@runtime_checkable
class ReportGenerator(Protocol):
    """Protocol for the PRO chaos report generator."""

    def generate_report(self, *args: Any, **kwargs: Any) -> Any: ...

    def generate_daily_report(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_report_by_date(self, *args: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class SafetyGuard(Protocol):
    """Protocol for the PRO chaos safety guard."""

    def check(self, *args: Any, **kwargs: Any) -> Any: ...
