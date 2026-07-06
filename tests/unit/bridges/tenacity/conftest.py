"""Shared fixtures for ``tests/unit/bridges/tenacity/`` (impl 451).

The autouse reset fixture is required because ``instrument_tenacity()``
mutates ``tenacity.Retrying.__init__`` globally — without per-test reset,
ordering effects under ``pytest -n 6`` would corrupt later tests in the
same xdist worker.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_tenacity_instrument():
    """Restore ``tenacity.Retrying.__init__`` after each test."""
    from baldur.bridges.tenacity.instrument import _reset_instrument_for_testing

    _reset_instrument_for_testing()
    yield
    _reset_instrument_for_testing()


@pytest.fixture
def make_retry_state():
    """Return a factory producing a stub ``RetryCallState``-like object.

    ``tenacity`` is installed in this dev environment, but constructing a
    real ``RetryCallState`` requires a Retrying instance and a wrapped fn.
    The bridge callbacks only consult ``attempt_number`` and ``outcome``,
    so a SimpleNamespace stub is sufficient and keeps tests deterministic.
    """
    from types import SimpleNamespace

    def _make(
        *,
        attempt_number: int = 1,
        failed: bool | None = None,
        exception: BaseException | None = None,
    ):
        if failed is None:
            outcome = None
        else:
            outcome = SimpleNamespace(
                failed=failed,
                exception=lambda: exception,
            )
        return SimpleNamespace(attempt_number=attempt_number, outcome=outcome)

    return _make
