"""
Bulkhead.execute() base-default tests.

The ABC ships a non-abstract execute() so third-party subclasses stay
instantiable: acquire a permit (bounded by ``timeout``), run ``fn`` in the
calling thread, release on return. ``timeout`` bounds the admission wait
only — execution is never offloaded nor bounded (implementations that
offload override execute() to also bound execution time).
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from baldur.services.bulkhead.base import Bulkhead, BulkheadState, BulkheadType
from baldur.services.bulkhead.exceptions import BulkheadFullError
from baldur.services.bulkhead.semaphore import SemaphoreBulkhead


def _raise_boom() -> None:
    raise ValueError("boom")


class _MinimalThirdPartyBulkhead(Bulkhead):
    """Third-party-style subclass implementing only the abstract surface.

    Deliberately does NOT override execute() — instantiating and executing
    through it pins the non-abstract-default contract. Records the timeout
    forwarded into acquire() so delegation can be asserted.
    """

    def __init__(self) -> None:
        self._active = 0
        self.acquire_timeouts: list[float | None] = []

    @property
    def name(self) -> str:
        return "third_party"

    @contextmanager
    def acquire(self, timeout: float | None = None) -> Generator[None, None, None]:
        self.acquire_timeouts.append(timeout)
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1

    def try_acquire(self, timeout: float | None = None) -> bool:
        self._active += 1
        return True

    def release(self) -> None:
        self._active -= 1

    def get_state(self) -> BulkheadState:
        return BulkheadState(
            name=self.name,
            bulkhead_type=BulkheadType.SEMAPHORE,
            max_concurrent=1,
            active_count=self._active,
            waiting_count=0,
            rejected_count=0,
        )


class TestBulkheadExecuteDefaultBehavior:
    """Base execute() default implementation, driven via the semaphore primitive."""

    @pytest.mark.parametrize(
        "timeout", [None, 0.5], ids=["none_nonblocking", "float_bounded"]
    )
    def test_execute_returns_fn_result_when_permit_available(self, timeout):
        """execute returns the function result for both timeout modes."""
        bulkhead = SemaphoreBulkhead("exec_default", max_concurrent=1)

        assert bulkhead.execute(lambda x: x + 1, 41, timeout=timeout) == 42

    def test_execute_forwards_args_and_kwargs(self):
        """Positional and keyword arguments reach the wrapped function."""
        bulkhead = SemaphoreBulkhead("exec_args", max_concurrent=1)

        def combine(left: str, right: str, sep: str = "-") -> str:
            return f"{left}{sep}{right}"

        assert bulkhead.execute(combine, "l", "r", sep="+") == "l+r"

    def test_execute_full_compartment_raises_full_error(self):
        """A full compartment raises BulkheadFullError (immediate verdict at timeout=None)."""
        # Given — the only permit is occupied
        bulkhead = SemaphoreBulkhead("exec_full", max_concurrent=1)
        assert bulkhead.try_acquire() is True

        try:
            # When/Then — the admission attempt fails loud
            with pytest.raises(BulkheadFullError):
                bulkhead.execute(lambda: "never", timeout=None)
        finally:
            bulkhead.release()

    def test_execute_runs_fn_in_calling_thread_no_offload(self):
        """fn runs in the caller's thread — a hung fn would occupy the caller.

        This pins the admission-only semantics: no worker-pool offload, so
        ``timeout`` cannot bound execution time on the default path.
        """
        bulkhead = SemaphoreBulkhead("exec_thread", max_concurrent=1)
        seen: list[int] = []

        bulkhead.execute(lambda: seen.append(threading.get_ident()))

        assert seen == [threading.get_ident()]

    def test_execute_releases_permit_after_return(self):
        """The permit returns to the pool once fn completes."""
        bulkhead = SemaphoreBulkhead("exec_release", max_concurrent=1)

        bulkhead.execute(lambda: "ok")

        assert bulkhead.get_state().active_count == 0

    def test_execute_business_exception_propagates_and_releases(self):
        """fn's own exception propagates uncaught and the permit is released."""
        bulkhead = SemaphoreBulkhead("exec_raise", max_concurrent=1)

        with pytest.raises(ValueError, match="boom"):
            bulkhead.execute(_raise_boom)

        # The permit is actually reusable, not just counted as free.
        assert bulkhead.get_state().active_count == 0
        assert bulkhead.try_acquire() is True
        bulkhead.release()


class TestBulkheadExecuteDefaultContract:
    """Non-abstract execute() keeps third-party subclasses instantiable."""

    def test_execute_is_not_abstract(self):
        """execute is not part of the abstract surface."""
        assert "execute" not in Bulkhead.__abstractmethods__

    def test_third_party_subclass_without_execute_instantiates_and_executes(self):
        """A subclass implementing only the abstract methods runs the default."""
        bulkhead = _MinimalThirdPartyBulkhead()

        assert bulkhead.execute(lambda: "via-default") == "via-default"
        assert bulkhead.get_state().active_count == 0

    def test_default_execute_forwards_timeout_to_acquire(self):
        """The default routes admission through acquire(timeout=...)."""
        bulkhead = _MinimalThirdPartyBulkhead()

        bulkhead.execute(lambda: "ok", timeout=1.5)

        assert bulkhead.acquire_timeouts == [1.5]
