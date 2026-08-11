"""752 D7 — the shared entry-point prelude keeps each return contract.

Every public protect entry point began the same way: configure logging, read
the enable flag, and — once protection is on — announce the runtime posture.
Two of the four had drifted out of that sequence, so the prelude now owns
it. What the prelude deliberately does NOT own is the disabled-path body:
``protect`` / ``aprotect`` return the raw value and let exceptions
propagate, while the ``*_with_meta`` pair returns a ``ProtectResult`` and
captures the failure instead.

Asserting one entry point is what let an earlier collapse of these four
bodies into a single shared one through, so all four are pinned here.
"""

from __future__ import annotations

import asyncio

import pytest

from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.protect_facade import (
    ProtectResult,
    aprotect,
    aprotect_with_meta,
    protect,
    protect_with_meta,
)

_SENTINEL = "the-raw-value"


class _Boom(RuntimeError):
    """Distinct type so a swallowed-and-rewrapped failure cannot pass."""


@pytest.fixture(autouse=True)
def protection_disabled(monkeypatch):
    from baldur.settings.protect import reset_protect_settings

    monkeypatch.setenv("BALDUR_PROTECT_ENABLED", "false")
    reset_protect_settings()
    yield
    reset_protect_settings()


def _ok() -> str:
    return _SENTINEL


def _boom() -> str:
    raise _Boom("disabled path still runs the callable")


async def _aok() -> str:
    return _SENTINEL


async def _aboom() -> str:
    raise _Boom("disabled path still runs the callable")


class TestProtectDisabledReturnContractBehavior:
    """Four entry points, two different contracts, no cross-contamination."""

    def test_the_flag_is_actually_off(self):
        """Guards every case below against passing on an enabled pipeline."""
        from baldur.settings.protect import get_protect_settings

        assert get_protect_settings().enabled is False

    @pytest.mark.parametrize(
        "call",
        [
            lambda: protect("d752", _ok),
            lambda: asyncio.run(aprotect("d752", _aok)),
        ],
        ids=["sync", "async"],
    )
    def test_the_raw_variants_return_the_callable_result_unwrapped(self, call):
        assert call() == _SENTINEL

    @pytest.mark.parametrize(
        "call",
        [
            lambda: protect("d752", _boom),
            lambda: asyncio.run(aprotect("d752", _aboom)),
        ],
        ids=["sync", "async"],
    )
    def test_the_raw_variants_propagate_the_exception(self, call):
        with pytest.raises(_Boom):
            call()

    @pytest.mark.parametrize(
        "call",
        [
            lambda: protect_with_meta("d752", _ok),
            lambda: asyncio.run(aprotect_with_meta("d752", _aok)),
        ],
        ids=["sync", "async"],
    )
    def test_the_meta_variants_wrap_a_success_in_a_result(self, call):
        result = call()

        assert isinstance(result, ProtectResult)
        assert result.success is True
        assert result.value == _SENTINEL
        assert result.attempts == 1
        assert result.error is None

    @pytest.mark.parametrize(
        "call",
        [
            lambda: protect_with_meta("d752", _boom),
            lambda: asyncio.run(aprotect_with_meta("d752", _aboom)),
        ],
        ids=["sync", "async"],
    )
    def test_the_meta_variants_capture_a_failure_instead_of_raising(self, call):
        result = call()

        assert isinstance(result, ProtectResult)
        assert result.success is False
        assert result.value is None
        assert isinstance(result.error, _Boom)
        assert result.outcome == PolicyOutcome.FAILURE

    @pytest.mark.parametrize(
        "call",
        [
            lambda: protect_with_meta("d752", _ok),
            lambda: asyncio.run(aprotect_with_meta("d752", _aok)),
        ],
        ids=["sync", "async"],
    )
    def test_the_meta_variants_time_the_disabled_call(self, call):
        """The result is a real measurement, not a zeroed placeholder."""
        result = call()

        assert result.duration_seconds >= 0.0
