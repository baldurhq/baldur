"""Execution-mode test helpers: the dry-run toggle and the kill switch.

Shared helpers for driving Baldur's observe-only signal through the **real**
resolver — the System Control dry-run toggle and kill switch read by
``get_execution_mode()`` — rather than the ``set_execution_mode()`` override,
which bypasses the toggle entirely (and which the kill switch outranks).

``dry_run_active()`` is the context manager the per-site observe-only tests use:
it flips the runtime dry-run toggle on (``enable_dry_run``) with a guaranteed
teardown that resets the System Control singleton AND clears any execution-mode
override. Centralising teardown here avoids the xdist-isolation flake that a
missed reset would cause across the multi-site test matrix.

The env-mode axis (``BALDUR_EXECUTION_MODE`` = shadow / evaluation) is a
*separate* posture and is deliberately NOT collapsed into this helper — drive it
with ``set_execution_mode(ExecutionMode.shadow())`` in the test itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def dry_run_active(actor: str = "test") -> Iterator[object]:
    """Activate the System Control runtime dry-run toggle for the block.

    Exercises the genuine D1 bridge: ``get_system_control().enable_dry_run()``
    flips the runtime flag, so ``get_execution_mode()`` resolves to observe-only
    via the ``runtime_toggle`` precedence rung (assuming the env posture would
    otherwise execute). The env cache is cleared first so a prior test that set
    ``BALDUR_EXECUTION_MODE`` cannot leave a stale ``shadow`` posture that would
    mask the toggle under test.

    Teardown is guaranteed: the System Control singleton is reset (clearing the
    dry-run flag) and any execution-mode override is cleared.

    Yields:
        The active ``SystemControlManager`` instance (so a test can read state).
    """
    from baldur.core.execution_mode import (
        _get_mode_from_env,
        clear_execution_mode_override,
    )
    from baldur.services.system_control import (
        get_system_control,
        reset_system_control,
    )

    # Make the env posture deterministic (default = active → should_execute) so
    # the toggle is the thing forcing observe-only and mode_source resolves to
    # "runtime_toggle".
    clear_execution_mode_override()
    _get_mode_from_env.cache_clear()

    manager = get_system_control()
    manager.enable_dry_run(actor=actor)
    try:
        yield manager
    finally:
        reset_system_control()
        clear_execution_mode_override()
        _get_mode_from_env.cache_clear()


@contextmanager
def kill_switch_active(actor: str = "test") -> Iterator[object]:
    """Pull the System Control kill switch for the block.

    Flips the real switch (``get_system_control().disable()``) on the process
    store, so ``get_execution_mode()`` resolves to observe-only through the
    ``kill_switch`` rung — above any programmatic override — exactly as it does
    when an operator pulls the brake. The env cache and any override are
    cleared first, like ``dry_run_active()``.

    Teardown is guaranteed: the System Control singleton is reset (re-enabling
    the switch) and any execution-mode override is cleared.

    Yields:
        The active ``SystemControlManager`` instance.
    """
    from baldur.core.execution_mode import (
        _get_mode_from_env,
        clear_execution_mode_override,
    )
    from baldur.services.system_control import (
        get_system_control,
        reset_system_control,
    )

    clear_execution_mode_override()
    _get_mode_from_env.cache_clear()

    manager = get_system_control()
    manager.disable(actor=actor, reason="kill switch under test")
    try:
        yield manager
    finally:
        reset_system_control()
        clear_execution_mode_override()
        _get_mode_from_env.cache_clear()


__all__ = ["dry_run_active", "kill_switch_active"]
