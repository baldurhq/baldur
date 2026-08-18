"""Scheduler Settings — in-process default scheduler knobs.

Governs the leader-elected scheduler that ``baldur.init()`` starts, and the
default jobs it registers.

Environment Variables:
    BALDUR_SCHEDULER_AUTOSTART=1
    BALDUR_SCHEDULER_DISABLED_JOBS=config_apply,sla_drift
"""

from __future__ import annotations

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()

_AUTOSTART_ENV_VAR = "BALDUR_SCHEDULER_AUTOSTART"

# Operator spellings for the two boolean outcomes. Supersets of the pre-settings
# raw-env read this field absorbs ({"0", "false", "no"} disabled the scheduler,
# everything else enabled it), so no value that disabled the scheduler before
# starts it now, and the widened spellings resolve the way an operator writing
# them evidently means.
_FALSE_LITERALS = frozenset({"0", "off", "f", "false", "n", "no"})
_TRUE_LITERALS = frozenset({"1", "on", "t", "true", "y", "yes"})


class SchedulerSettings(BaseSettings):
    """Default-scheduler autostart and per-job disable switches.

    Both knobs govern the **in-process** scheduler started by ``init()``. On a
    celery deployment most default jobs also run off a beat lane that these
    knobs do not reach — see ``disabled_jobs``.
    """

    model_config = make_settings_config("BALDUR_SCHEDULER_")

    autostart: bool = Field(
        default=True,
        description=(
            "Whether baldur.init() registers the default jobs and starts the "
            "leader-elected scheduler. All-or-nothing — to switch off a single "
            "job use disabled_jobs instead. An unparseable value is treated as "
            "True (the scheduler runs) and logs a WARNING naming the variable."
        ),
    )

    disabled_jobs: str = Field(
        default="",
        description=(
            "Comma-separated default-job names to skip at registration, e.g. "
            "'config_apply,sla_drift'. Unknown names log a WARNING and are "
            "otherwise ignored. Scope: the in-process scheduler only, except "
            "for config_apply whose celery beat lane honours the same list; "
            "the other default jobs' celery twins are controlled by "
            "configure_baldur_celery(include_*)."
        ),
    )

    @field_validator("autostart", mode="before")
    @classmethod
    def _coerce_autostart(cls, v: object) -> object:
        """Parse an operator-typed autostart value without ever raising.

        Guarded on ``isinstance(str)`` because ``validate_default=True`` runs
        this validator over the bool default too.

        Two behaviours the plain bool coercion does not give, both of which
        an off-switch needs. Surrounding whitespace is stripped first, so
        ``"0 "`` still disables the scheduler rather than failing validation
        and falling back to enabled — that fallback would start the scheduler
        despite an explicit off-switch. And an unparseable value coerces to
        True with a WARNING instead of raising: a raise would make the whole
        model unconstructable, discarding the operator's ``disabled_jobs``
        list along with the typo.
        """
        if not isinstance(v, str):
            return v

        stripped = v.strip()
        if not stripped:
            # Blank / whitespace-only reads as "not set" — enabled, as before.
            return True

        lowered = stripped.lower()
        if lowered in _FALSE_LITERALS:
            return False
        if lowered in _TRUE_LITERALS:
            return True

        logger.warning(
            "scheduler.autostart_value_unparseable",
            env_var=_AUTOSTART_ENV_VAR,
            value=v,
            fallback=True,
        )
        return True

    def get_disabled_job_names(self) -> tuple[str, ...]:
        """Return the operator's disabled job names, empty entries dropped.

        Empty items are dropped before anything else: the field's own default
        is ``""``, and a naive split would yield one empty name that the
        caller's unknown-name check reports on every boot of every deployment
        that never set the variable.
        """
        if not self.disabled_jobs:
            return ()
        return tuple(
            name.strip() for name in self.disabled_jobs.split(",") if name.strip()
        )


def get_scheduler_settings() -> SchedulerSettings:
    """Get cached SchedulerSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(SchedulerSettings)


def reset_scheduler_settings() -> None:
    """Reset cached settings — for test isolation only."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(SchedulerSettings)


__all__ = [
    "SchedulerSettings",
    "get_scheduler_settings",
    "reset_scheduler_settings",
]
