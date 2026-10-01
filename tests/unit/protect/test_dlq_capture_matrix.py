"""Every failed call at a DLQ-capturing call site is parked under its name.

Target: the capture path from each DLQ-capturing entry point — ``protect`` /
``aprotect``, ``@protected`` on a sync and an async function, ``@dlq_protect``
on both, and the ``standard_pipeline`` preset — through the composer's failure
capture to ``DLQSink``.

Seam: ``baldur.services.retry_handler.sinks.store_to_dlq``. A cell whose call
reached the caller with a failure finds exactly one store call, filed under
the call site's name; a dropped failure leaves zero calls, a duplicate two.

The matrix — entry point x retry configuration x terminal
----------------------------------------------------------
Retry configurations:

- ``retry_enabled``: ``retry=True`` (settings-derived); on the preset, its own
  retry stage.
- ``retry_disabled``: the same under ``BALDUR_RETRY_ENABLED=false``.
- ``placeholder_domain``: a ``RetryPolicyConfig`` built without ``domain=``; on
  the preset, a ``retry_policy=`` built from one.
- ``tenacity``: a ``TenacityBridgePolicy``; the async entry points run it as
  the async bridge they convert it to.

Terminals:

- ``final_raise``: the call raises on every attempt.
- ``timeout_mid_retry``: ``timeout=`` cuts the call off while its retry stage
  runs it.
- ``result_exhausted``: ``retry_if_result`` runs out of attempts; the bridge
  reports it as ``MaxRetriesExceededError`` carrying its attempt count.

Cells that cannot exist:

- ``@dlq_protect`` pins ``retry=True``: no ``placeholder_domain`` or
  ``tenacity`` rows.
- ``standard_pipeline`` takes no ``timeout=``: no ``timeout_mid_retry`` cells.
- ``result_exhausted`` exists only on the ``tenacity`` rows.
- ``aprotect`` and async ``@protected`` take only the tenacity bridge as a
  pre-built stage, so the synthesized-rejection case — a caller-supplied
  stage that ends without an error object — is sync ``protect`` only.

Beyond the matrix: ``observe_only`` (the same cells store nothing and leave
the would-store decision), ``excluded`` (each facade-level exclusion rule and
its record), ``nested`` (an enclosing DLQ site around an inner one whose
breaker is open), a named retry domain kept, and the synthesized rejection.

UNIT_TEST_GUIDELINES.md:
- Behavior verification: the failure type is the sink's error -> failure-type
  mapping, computed from the error class; attempt counts come from the
  module's own retry settings.
- No ``time.sleep`` (§6.3): a call the bound cuts off blocks on an event the
  test releases, then the timeout executor is drained so the abandoned worker
  has finished before the store count is read. The sync retry stages wait
  through the module default sleeper, replaced by a no-op; the async stages
  wait the settings floor once.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import tenacity
from structlog.testing import capture_logs

from baldur import protect_facade
from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.bridges.tenacity.policy import TenacityBridgePolicy
from baldur.core.exceptions import TimeoutPolicyError
from baldur.core.execution_mode import clear_execution_mode_override
from baldur.decorators.dlq_protect import dlq_protect
from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
    PolicyOutcome,
    PolicyRejectedException,
    PolicyResult,
)
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE, DLQEntryResult
from baldur.protect_facade import aprotect, protect, protected
from baldur.resilience.policies.guards.error_budget import ErrorBudgetGuard
from baldur.resilience.policies.presets import standard_pipeline
from baldur.resilience.policies.timeout import TimeoutPolicy
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.retry_handler.models import (
    MaxRetriesExceededError,
    RetryPolicyConfig,
)
from baldur.services.retry_handler.policy import RetryPolicy
from baldur.settings.dlq import reset_dlq_settings
from baldur.settings.protect import reset_protect_settings
from baldur.settings.retry import reset_retry_settings
from tests.factories import dry_run_active

_STORE = "baldur.services.retry_handler.sinks.store_to_dlq"

# A bound far below the time the blocked call needs, and a block far above it:
# the call is always cut off, then released so its worker completes in-test.
_BOUND_SECONDS = 0.05
_RELEASE_WAIT_SECONDS = 5.0

#: Total attempts of a settings-derived retry stage in this module.
SETTINGS_ATTEMPTS = 2
#: Total attempts of an explicit config built without ``domain=``.
CONFIG_ATTEMPTS = 2
#: Attempts a tenacity bridge makes before it gives up.
BRIDGE_ATTEMPTS = 3


def _no_wait(_delay: float) -> None:
    """The sync retry stage's between-attempt wait, made instant."""


@pytest.fixture(autouse=True)
def _isolated_capture_profile(monkeypatch) -> Iterator[None]:
    """Fresh protect caches, a two-attempt settings retry stage with the
    shortest settings backoff, and no observe-only override around every
    test."""
    monkeypatch.setenv("BALDUR_RETRY_MAX_ATTEMPTS", str(SETTINGS_ATTEMPTS))
    monkeypatch.setenv("BALDUR_RETRY_BASE_DELAY", "0.1")
    clear_execution_mode_override()
    reset_retry_settings()
    reset_protect_settings()
    with patch("baldur.services.retry_handler.policy._DEFAULT_SLEEPER", _no_wait):
        yield
    reset_protect_settings()
    reset_retry_settings()
    reset_dlq_settings()
    clear_execution_mode_override()


@pytest.fixture
def store() -> Iterator[MagicMock]:
    """The DLQ store the sink calls — the capture seam."""
    with patch(
        _STORE, autospec=True, return_value=DLQEntryResult.created("dlq-1")
    ) as mock_store:
        yield mock_store


def _switch_retry_off(monkeypatch) -> None:
    """``BALDUR_RETRY_ENABLED=false``, picked up by every stage built after."""
    monkeypatch.setenv("BALDUR_RETRY_ENABLED", "false")
    reset_retry_settings()
    reset_protect_settings()


# =============================================================================
# The matrix
# =============================================================================


@dataclass(frozen=True)
class Cell:
    """One entry point x retry configuration x terminal."""

    entry: str
    retry: str
    terminal: str

    @property
    def id(self) -> str:
        return f"{self.entry}-{self.retry}-{self.terminal}"

    @property
    def name(self) -> str:
        """The call site's name, unique per cell."""
        return f"svc.{self.entry}.{self.retry}.{self.terminal}"

    @property
    def is_async(self) -> bool:
        return self.entry in ("aprotect", "protected_async", "dlq_protect_async")


_FACADE_ENTRIES = ("protect", "aprotect", "protected_sync", "protected_async")
_DLQ_PROTECT_ENTRIES = ("dlq_protect_sync", "dlq_protect_async")


def _cells() -> list[Cell]:
    cells: list[Cell] = []
    for entry in _FACADE_ENTRIES:
        for retry in ("retry_enabled", "retry_disabled", "placeholder_domain"):
            for terminal in ("final_raise", "timeout_mid_retry"):
                cells.append(Cell(entry, retry, terminal))
        for terminal in ("final_raise", "timeout_mid_retry", "result_exhausted"):
            cells.append(Cell(entry, "tenacity", terminal))
    for entry in _DLQ_PROTECT_ENTRIES:
        for retry in ("retry_enabled", "retry_disabled"):
            for terminal in ("final_raise", "timeout_mid_retry"):
                cells.append(Cell(entry, retry, terminal))
    for retry in ("retry_enabled", "retry_disabled", "placeholder_domain"):
        cells.append(Cell("standard_pipeline", retry, "final_raise"))
    for terminal in ("final_raise", "result_exhausted"):
        cells.append(Cell("standard_pipeline", "tenacity", terminal))
    return cells


CELLS = _cells()
CELL_IDS = [cell.id for cell in CELLS]


def _bridge(terminal: str) -> TenacityBridgePolicy[Any]:
    """A tenacity stage that gives up after ``BRIDGE_ATTEMPTS`` — on a raise,
    or, for ``result_exhausted``, on a ``None`` result."""
    if terminal == "result_exhausted":
        return TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(BRIDGE_ATTEMPTS),
            wait=tenacity.wait_none(),
            retry=tenacity.retry_if_result(lambda value: value is None),
        )
    return TenacityBridgePolicy(
        stop=tenacity.stop_after_attempt(BRIDGE_ATTEMPTS), wait=tenacity.wait_none()
    )


def _unnamed_config() -> RetryPolicyConfig:
    """An explicit retry config with no ``domain=`` and no backoff."""
    return RetryPolicyConfig(
        max_attempts=CONFIG_ATTEMPTS, backoff_base=0, backoff_max=0, jitter_percent=0
    )


def _facade_retry(cell: Cell) -> Any:
    if cell.retry in ("retry_enabled", "retry_disabled"):
        return True
    if cell.retry == "placeholder_domain":
        return _unnamed_config()
    return _bridge(cell.terminal)


class _Body:
    """The business call a cell runs, in its sync and async forms."""

    def __init__(self, terminal: str) -> None:
        self.terminal = terminal
        self.release = threading.Event()

    def sync(self) -> str | None:
        if self.terminal == "timeout_mid_retry":
            self.release.wait(timeout=_RELEASE_WAIT_SECONDS)
            return "late"
        if self.terminal == "result_exhausted":
            return None
        raise ConnectionError("upstream down")

    async def asynchronous(self) -> str | None:
        if self.terminal == "timeout_mid_retry":
            await asyncio.Event().wait()  # never set: only the bound ends it
            return "never"
        if self.terminal == "result_exhausted":
            return None
        raise ConnectionError("upstream down")


def _preset(cell: Cell):
    if cell.retry in ("retry_enabled", "retry_disabled"):
        return standard_pipeline(cell.name, max_retries=SETTINGS_ATTEMPTS)
    if cell.retry == "placeholder_domain":
        return standard_pipeline(
            cell.name, retry_policy=RetryPolicy(config=_unnamed_config())
        )
    return standard_pipeline(cell.name, retry_policy=_bridge(cell.terminal))


def _run(cell: Cell) -> bool:
    """Run one call of ``cell``; True when a failure reached the caller."""
    body = _Body(cell.terminal)
    timeout = _BOUND_SECONDS if cell.terminal == "timeout_mid_retry" else None
    try:
        if cell.entry == "standard_pipeline":
            return not _preset(cell).execute(body.sync).success
        if cell.entry == "protect":
            protect(
                cell.name,
                body.sync,
                retry=_facade_retry(cell),
                dlq=True,
                circuit_breaker=False,
                timeout=timeout,
            )
        elif cell.entry == "aprotect":
            asyncio.run(
                aprotect(
                    cell.name,
                    body.asynchronous,
                    retry=_facade_retry(cell),
                    dlq=True,
                    circuit_breaker=False,
                    timeout=timeout,
                )
            )
        elif cell.entry in ("protected_sync", "protected_async"):
            decorate = protected(
                cell.name,
                retry=_facade_retry(cell),
                dlq=True,
                circuit_breaker=False,
                timeout=timeout,
            )
            if cell.is_async:
                asyncio.run(decorate(body.asynchronous)())
            else:
                decorate(body.sync)()
        else:
            decorate = dlq_protect(cell.name, timeout=timeout)
            if cell.is_async:
                asyncio.run(decorate(body.asynchronous)())
            else:
                decorate(body.sync)()
    except Exception:  # the failure the caller receives
        return True
    finally:
        body.release.set()
        # Join an abandoned sync worker, so whatever it does after the bound
        # has happened before the caller's assertions read the store.
        TimeoutPolicy.shutdown_executor()
    return False


def _expected_failure_type(cell: Cell) -> str:
    error_class: type[Exception] = ConnectionError
    if cell.terminal == "timeout_mid_retry":
        error_class = TimeoutPolicyError
    elif cell.terminal == "result_exhausted":
        error_class = MaxRetriesExceededError
    return f"MAX_RETRIES_{error_class.__name__.upper()}"


def _expected_attempts(cell: Cell) -> int:
    """The attempts the stored verdict states the call made."""
    if cell.terminal == "timeout_mid_retry" or cell.retry == "retry_disabled":
        return 1
    if cell.retry == "placeholder_domain":
        return CONFIG_ATTEMPTS
    if cell.retry == "tenacity":
        return BRIDGE_ATTEMPTS
    return SETTINGS_ATTEMPTS


class TestDlqCaptureMatrixBehavior:
    """One entry per failed call, under the call site's name, whatever the
    retry setting, the retry implementation, or the terminal."""

    @pytest.mark.parametrize("cell", CELLS, ids=CELL_IDS)
    def test_failed_call_is_parked_once_under_the_call_site_name(
        self, store, monkeypatch, cell
    ):
        # Given the cell's retry configuration
        if cell.retry == "retry_disabled":
            _switch_retry_off(monkeypatch)

        # When one call runs to its terminal
        failed = _run(cell)

        # Then the failure reached the caller and was parked exactly once
        assert failed is True
        assert store.call_count == 1
        kwargs = store.call_args.kwargs
        assert kwargs["domain"] == cell.name
        assert kwargs["failure_type"] == _expected_failure_type(cell)
        assert kwargs["metadata"]["max_attempts"] == _expected_attempts(cell)

    @pytest.mark.parametrize("cell", CELLS, ids=CELL_IDS)
    def test_observe_only_cell_stores_nothing_and_records_the_would_store_decision(
        self, store, monkeypatch, cell
    ):
        if cell.retry == "retry_disabled":
            _switch_retry_off(monkeypatch)

        with dry_run_active(), capture_logs() as logs:
            failed = _run(cell)

        assert failed is True
        store.assert_not_called()
        would_store = [
            e
            for e in logs
            if e.get("event") == "execution_mode.intervention_suppressed"
            and e.get("action") == "dlq_store"
        ]
        assert len(would_store) == 1
        assert would_store[0]["service_name"] == cell.name


# =============================================================================
# Beyond the matrix — named domain, synthesized rejection
# =============================================================================


class _ErrorlessFailureStage:
    """A caller-supplied retry stage that gives up without an error object;
    the composer synthesizes the rejection the caller receives."""

    @property
    def name(self) -> str:
        return "custom_retry"

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            value=None, outcome=PolicyOutcome.FAILURE, error=None, total_attempts=3
        )


def _raise_upstream() -> str:
    raise ConnectionError("upstream down")


async def _araise_upstream() -> str:
    raise ConnectionError("upstream down")


class TestDlqCaptureDomainAndRejectionBehavior:
    """The placeholder is replaced, a named domain is not, and a stage failure
    with no error object is still a parked call."""

    def test_named_retry_domain_is_kept_on_sync_protect(self, store):
        named = RetryPolicyConfig(
            max_attempts=CONFIG_ATTEMPTS,
            backoff_base=0,
            backoff_max=0,
            jitter_percent=0,
            domain="payments",
        )

        with pytest.raises(ConnectionError):
            protect(
                "svc.named",
                _raise_upstream,
                retry=named,
                dlq=True,
                circuit_breaker=False,
                timeout=None,
            )

        assert store.call_count == 1
        assert store.call_args.kwargs["domain"] == "payments"

    def test_named_retry_domain_is_kept_on_aprotect(self, store):
        named = RetryPolicyConfig(
            max_attempts=CONFIG_ATTEMPTS,
            backoff_base=0,
            backoff_max=0,
            jitter_percent=0,
            domain="payments",
        )

        with pytest.raises(ConnectionError):
            asyncio.run(
                aprotect(
                    "svc.anamed",
                    _araise_upstream,
                    retry=named,
                    dlq=True,
                    circuit_breaker=False,
                    timeout=None,
                )
            )

        assert store.call_count == 1
        assert store.call_args.kwargs["domain"] == "payments"

    def test_synthesized_rejection_from_an_errorless_stage_is_parked_once(self, store):
        with pytest.raises(PolicyRejectedException):
            protect(
                "svc.errorless",
                _raise_upstream,
                retry=_ErrorlessFailureStage(),
                dlq=True,
                circuit_breaker=False,
                timeout=None,
            )

        assert store.call_count == 1
        kwargs = store.call_args.kwargs
        assert kwargs["domain"] == "svc.errorless"
        expected_type = PolicyRejectedException.__name__.upper()
        assert kwargs["failure_type"] == f"MAX_RETRIES_{expected_type}"
        assert kwargs["metadata"]["max_attempts"] == 3


# =============================================================================
# Excluded — each facade-level exclusion rule leaves its record
# =============================================================================


def _records(logs: list, event: str) -> list:
    return [e for e in logs if e.get("event") == event]


def _breaker_opening_on_first_failure(name: str) -> None:
    """Install an in-memory breaker for ``name`` that opens on its first
    failure, shared by the sync and async facade through its per-name cache
    (dropped again by the autouse cache reset)."""
    cb_service = CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True,
            failure_threshold=1,
            minimum_calls=1,
            failure_rate_threshold=0,
            recovery_timeout=60,
        ),
        repository=InMemoryCircuitBreakerStateRepository(),
    )
    protect_facade._cb_policy_cache[name] = CircuitBreakerPolicy(
        service_name=name, cb_service=cb_service, hooks=[]
    )


class TestDlqCaptureExcludedBehavior:
    """A call a stated rule excludes is not parked and leaves the record the
    DLQ guide names beside the rule, at that rule's level."""

    def test_excluded_fallback_answered_call_logs_fallback_applied(self, store):
        with capture_logs() as logs:
            value = protect(
                "svc.degraded",
                _raise_upstream,
                fallback=lambda: "degraded",
                retry=True,
                dlq=True,
                circuit_breaker=False,
                timeout=None,
            )

        assert value == "degraded"
        store.assert_not_called()
        [record] = _records(logs, "policy_chain.fallback_applied")
        assert record["log_level"] == "warning"

    def test_excluded_idempotency_refusal_logs_duplicate_blocked(self, store):
        from baldur.core.exceptions import IdempotencyDuplicateError

        calls: list[str] = []

        @protected(
            "svc.charge_once",
            dlq=True,
            circuit_breaker=False,
            timeout=None,
            idempotency_key=lambda ctx: "charge-o-1",
        )
        def charge(order_id: str) -> str:
            calls.append(order_id)
            return "charged"

        charge("o-1")
        with capture_logs() as logs, pytest.raises(IdempotencyDuplicateError):
            charge("o-1")

        assert calls == ["o-1"]
        store.assert_not_called()
        [record] = _records(logs, "idempotency.duplicate_blocked")
        assert record["log_level"] == "warning"

    def test_excluded_preset_guard_refusal_logs_execution_rejected(self, store):
        """The preset's error-budget guard refuses only with the PRO gate
        installed, so its decision is stubbed here; what is pinned is the
        preset's record of the refusal and that nothing is parked."""
        refusal = GuardResult(allowed=False, reason="Error budget exhausted")
        pipeline = standard_pipeline("svc.budget_spent", max_retries=1)

        with (
            patch.object(
                ErrorBudgetGuard, "check", autospec=True, return_value=refusal
            ),
            capture_logs() as logs,
        ):
            result = pipeline.execute(_raise_upstream)

        assert result.outcome == PolicyOutcome.REJECTED
        store.assert_not_called()
        [record] = _records(logs, "policy_pipeline.execution_rejected")
        assert record["log_level"] == "warning"
        assert record["reason"] == "Error budget exhausted"

    def test_excluded_stage_declined_call_logs_capture_skipped(self, store):
        declining = RetryPolicy(
            config=RetryPolicyConfig(
                max_attempts=CONFIG_ATTEMPTS, domain="svc.declines", enable_dlq=False
            )
        )

        with capture_logs() as logs, pytest.raises(ConnectionError):
            protect(
                "svc.declines",
                _raise_upstream,
                retry=declining,
                dlq=True,
                circuit_breaker=False,
                timeout=None,
            )

        store.assert_not_called()
        [record] = _records(logs, "dlq_sink.capture_skipped")
        assert record["log_level"] == "debug"
        assert record["reason"] == "stage_declined"

    def test_excluded_open_circuit_rejection_with_capture_off_logs_capture_skipped(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("BALDUR_DLQ_OPEN_CIRCUIT_CAPTURE_ENABLED", "false")
        reset_dlq_settings()
        _breaker_opening_on_first_failure("svc.capture_off")
        with pytest.raises(ConnectionError):
            protect("svc.capture_off", _raise_upstream, dlq=True, timeout=None)
        store.reset_mock()

        with capture_logs() as logs, pytest.raises(CircuitBreakerOpenError):
            protect("svc.capture_off", _raise_upstream, dlq=True, timeout=None)

        store.assert_not_called()
        [record] = _records(logs, "dlq_sink.capture_skipped")
        assert record["log_level"] == "debug"
        assert record["reason"] == "open_circuit_capture_disabled"


# =============================================================================
# Nested — an enclosing DLQ site around an inner one whose breaker is open
# =============================================================================

_INNER = "svc.inner"
_OUTER = "svc.outer"


def _open_inner_breaker(store: MagicMock) -> None:
    """Open the inner site's breaker with one failed call, then forget the
    entry that call parked."""
    _breaker_opening_on_first_failure(_INNER)
    with pytest.raises(ConnectionError):
        protect(_INNER, _raise_upstream, dlq=True, timeout=None)
    store.reset_mock()


def _nested_sync() -> None:
    protect(
        _OUTER,
        lambda: protect(_INNER, _raise_upstream, dlq=True, timeout=None),
        retry=True,
        dlq=True,
        timeout=None,
    )


def _nested_async() -> None:
    async def inner_call() -> str:
        return await aprotect(_INNER, _araise_upstream, dlq=True, timeout=None)

    asyncio.run(aprotect(_OUTER, inner_call, retry=True, dlq=True, timeout=None))


_NESTED_FORMS = pytest.mark.parametrize(
    "nested_call", [_nested_sync, _nested_async], ids=["sync", "async"]
)


class TestDlqCaptureNestedSiteBehavior:
    """The inner site parks the rejection and marks it once the store took
    custody; the enclosing site skips a marked rejection and parks one the
    inner store kept nothing of."""

    @pytest.mark.parametrize(
        "inner_store_result",
        [
            DLQEntryResult.created("dlq-inner"),
            DLQEntryResult.fallback("backend down", "disk_persistent_buffer://dlq"),
        ],
        ids=["stored", "local_fallback"],
    )
    @_NESTED_FORMS
    def test_nested_rejection_in_custody_is_parked_once(
        self, store, nested_call, inner_store_result
    ):
        # Given the inner site's breaker is open and its store takes custody
        _open_inner_breaker(store)
        store.return_value = inner_store_result

        # When the enclosing site's call reaches the open inner breaker
        with pytest.raises(CircuitBreakerOpenError):
            nested_call()

        # Then the rejection is parked once, by the inner site
        assert store.call_count == 1
        assert store.call_args.kwargs["domain"] == _INNER
        assert store.call_args.kwargs["failure_type"] == OPEN_CIRCUIT_FAILURE_TYPE

    @_NESTED_FORMS
    def test_nested_rejection_the_inner_store_kept_nothing_of_is_parked_again(
        self, store, nested_call
    ):
        _open_inner_breaker(store)
        store.side_effect = RuntimeError("DLQ down")

        with pytest.raises(CircuitBreakerOpenError):
            nested_call()

        assert store.call_count == 2
        assert [c.kwargs["failure_type"] for c in store.call_args_list] == [
            OPEN_CIRCUIT_FAILURE_TYPE,
            OPEN_CIRCUIT_FAILURE_TYPE,
        ]
