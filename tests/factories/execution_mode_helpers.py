"""Execution-mode / dry-run test helpers.

Shared helpers for driving Baldur's observe-only (dry-run) signal through the
**real** D1 bridge — i.e. the System Control runtime toggle resolved by
``get_execution_mode()`` — rather than the ``set_execution_mode()`` override,
which bypasses the toggle entirely.

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


__all__ = ["dry_run_active"]
