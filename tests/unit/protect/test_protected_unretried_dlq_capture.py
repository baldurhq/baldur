"""``@protected(name, dlq=True)`` without a retry stage parks its final failure.

Target: ``baldur.protect_facade`` — both composer builders arm the composer for
failure capture whenever ``dlq`` is on — through ``PolicyComposer`` /
``AsyncPolicyComposer`` to ``DLQSink``.

Seam: ``baldur.services.retry_handler.sinks.store_to_dlq``. Without the arming
the sink drops the terminal and the store is never called, so each positive
case here fails for the production reason the arming exists; each negative
case pins a profile the arming must leave alone (a served fallback, a retry
stage that decides for itself, ``dlq=False``, observe-only mode).

UNIT_TEST_GUIDELINES.md:
- Behavior verification. The entry's ``MAX_RETRIES_<ERROR_TYPE>`` name is the
  sink's error -> failure-type mapping (§2.1), asserted as written.
- No ``time.sleep`` (§6.3): a call the bound cuts off blocks on an event the
  test releases once the call has returned, so the executor thread finishes
  inside the test.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from baldur import protect_facade
from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.core.exceptions import TimeoutPolicyError
from baldur.core.execution_mode import clear_execution_mode_override
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE, DLQEntryResult
from baldur.protect_facade import aprotect_with_meta, protect_with_meta, protected
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from baldur.settings.protect import reset_protect_settings
from tests.factories import dry_run_active

_STORE = "baldur.services.retry_handler.sinks.store_to_dlq"

# A bound far below the time the blocked call needs, and a block far above it:
# the call is always cut off, then released so its thread completes in-test.
_BOUND_SECONDS = 0.05
_RELEASE_WAIT_SECONDS = 5.0


@pytest.fixture(autouse=True)
def _reset_protect_state() -> Iterator[None]:
    """Fresh protect settings and caches (breakers, composers, the timeout
    executor) plus a cleared execution-mode override around every test."""
    clear_execution_mode_override()
    reset_protect_settings()
    yield
    reset_protect_settings()
    clear_execution_mode_override()


@pytest.fixture
def store() -> Iterator[MagicMock]:
    """The DLQ store the sink calls — the capture seam."""
    with patch(
        _STORE, autospec=True, return_value=DLQEntryResult.created("dlq-1")
    ) as mock_store:
        yield mock_store


@pytest.fixture
def breaker_opening_after_one_failure() -> str:
    """An in-memory breaker for ``svc.outage`` that opens on its first failure,
    shared by the sync and async paths through the facade's per-name cache
    (dropped again by the autouse cache reset)."""
    name = "svc.outage"
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
    return name


def _failure_types(store: MagicMock) -> list[str]:
    return [call.kwargs["failure_type"] for call in store.call_args_list]


def _retry_config(domain: str, *, enable_dlq: bool = True) -> RetryPolicyConfig:
    """Two attempts, no backoff — a retry stage that decides without waiting."""
    return RetryPolicyConfig(
        max_attempts=2,
        backoff_base=0,
        backoff_max=0,
        jitter_percent=0,
        enable_dlq=enable_dlq,
        domain=domain,
    )


def _raise_upstream() -> str:
    raise RuntimeError("upstream 500")


async def _araise_upstream() -> str:
    raise RuntimeError("upstream 500")


class TestProtectedUnretriedDlqCaptureBehavior:
    """The README profile — ``dlq=True`` and no ``retry=`` — stores the call
    that failed or was cut off, under the protect name, with its arguments."""

    # --- (a) the wrapped call raises ----------------------------------------

    def test_sync_raise_parks_one_entry_under_the_protect_name(self, store):
        @protected("svc.summarize", dlq=True, circuit_breaker=False, timeout=None)
        def summarize(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        with pytest.raises(RuntimeError, match="upstream 500"):
            summarize("doc-7")

        store.assert_called_once()
        kwargs = store.call_args.kwargs
        assert kwargs["domain"] == "svc.summarize"
        assert kwargs["failure_type"] == "MAX_RETRIES_RUNTIMEERROR"
        assert kwargs["request_data"] == {"doc_id": "doc-7"}
        assert kwargs["metadata"]["max_attempts"] == 1
        assert kwargs["metadata"]["retry_history"] == []

    def test_async_raise_parks_one_entry_under_the_protect_name(self, store):
        @protected("svc.asummarize", dlq=True, circuit_breaker=False, timeout=None)
        async def asummarize(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        with pytest.raises(RuntimeError, match="upstream 500"):
            asyncio.run(asummarize("doc-7"))

        store.assert_called_once()
        kwargs = store.call_args.kwargs
        assert kwargs["domain"] == "svc.asummarize"
        assert kwargs["failure_type"] == "MAX_RETRIES_RUNTIMEERROR"
        assert kwargs["request_data"] == {"doc_id": "doc-7"}
        assert kwargs["metadata"]["max_attempts"] == 1
        assert kwargs["metadata"]["retry_history"] == []

    def test_sync_raise_with_a_non_integer_user_id_is_still_parked(self, store):
        """The DLQ ``user_id`` column is an integer; a ``usr_...`` id leaves it
        empty and the entry keeps the id in its request data (the sink shares
        this extraction with the async path)."""

        @protected("svc.charge", dlq=True, circuit_breaker=False, timeout=None)
        def charge(order_id: str, user_id: str) -> str:
            raise RuntimeError("upstream 500")

        with pytest.raises(RuntimeError):
            charge("o-1", "usr_7f3a")

        store.assert_called_once()
        kwargs = store.call_args.kwargs
        assert kwargs["domain"] == "svc.charge"
        assert kwargs["user_id"] is None
        assert kwargs["request_data"] == {"order_id": "o-1", "user_id": "usr_7f3a"}

    # --- (b) the wall-clock bound cuts the call off -------------------------

    def test_sync_call_cut_off_by_the_bound_is_parked(self, store):
        release = threading.Event()

        @protected("svc.slow", dlq=True, circuit_breaker=False, timeout=_BOUND_SECONDS)
        def slow(doc_id: str) -> str:
            release.wait(timeout=_RELEASE_WAIT_SECONDS)
            return "late"

        try:
            with pytest.raises(TimeoutPolicyError):
                slow("doc-9")
        finally:
            release.set()

        store.assert_called_once()
        kwargs = store.call_args.kwargs
        assert kwargs["domain"] == "svc.slow"
        assert kwargs["failure_type"] == "MAX_RETRIES_TIMEOUTPOLICYERROR"
        assert kwargs["request_data"] == {"doc_id": "doc-9"}

    def test_async_call_cut_off_by_the_bound_is_parked(self, store):
        @protected("svc.aslow", dlq=True, circuit_breaker=False, timeout=_BOUND_SECONDS)
        async def aslow(doc_id: str) -> str:
            await asyncio.Event().wait()  # never set: only the bound ends it
            return "never"

        with pytest.raises(TimeoutPolicyError):
            asyncio.run(aslow("doc-9"))

        store.assert_called_once()
        kwargs = store.call_args.kwargs
        assert kwargs["domain"] == "svc.aslow"
        assert kwargs["failure_type"] == "MAX_RETRIES_TIMEOUTPOLICYERROR"
        assert kwargs["request_data"] == {"doc_id": "doc-9"}

    # --- (c) a served fallback answered the caller --------------------------

    def test_sync_served_fallback_parks_nothing(self, store):
        @protected(
            "svc.degrade",
            dlq=True,
            circuit_breaker=False,
            timeout=None,
            fallback=lambda: "degraded",
        )
        def call(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        assert call("doc-1") == "degraded"
        store.assert_not_called()

    def test_async_served_fallback_parks_nothing(self, store):
        # The async path awaits its fallback, so it takes an ``async def``.
        async def degraded() -> str:
            return "degraded"

        @protected(
            "svc.adegrade",
            dlq=True,
            circuit_breaker=False,
            timeout=None,
            fallback=degraded,
        )
        async def call(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        assert asyncio.run(call("doc-1")) == "degraded"
        store.assert_not_called()

    # --- (d) a retry stage decides for itself -------------------------------

    def test_sync_retry_verdict_false_parks_nothing(self, store):
        """A pre-built retry stage that declines keeps its verdict end to end,
        so the call is not parked. The facade's own no-arming condition is
        pinned by the retry-present timeout tests below, where no stage
        verdict exists to decide the outcome first."""

        @protected(
            "svc.declines",
            dlq=True,
            retry=RetryPolicy(config=_retry_config("svc.declines", enable_dlq=False)),
            circuit_breaker=False,
            timeout=None,
        )
        def call(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        with pytest.raises(RuntimeError):
            call("doc-1")

        store.assert_not_called()

    @pytest.mark.parametrize(
        "prebuilt", [False, True], ids=["retry_config", "prebuilt_retry_policy"]
    )
    def test_sync_retry_stage_cut_off_by_the_bound_parks_one_entry(
        self, store, prebuilt
    ):
        """With a retry stage the bound cuts the retry sequence off before it
        can reach a verdict; the armed composer supplies one, filed under the
        call site's name — whichever form of ``retry=`` composed the stage."""
        config = _retry_config("svc.retried_slow")
        release = threading.Event()

        @protected(
            "svc.retried_slow",
            dlq=True,
            retry=RetryPolicy(config=config) if prebuilt else config,
            circuit_breaker=False,
            timeout=_BOUND_SECONDS,
        )
        def call(doc_id: str) -> str:
            release.wait(timeout=_RELEASE_WAIT_SECONDS)
            return "late"

        try:
            with pytest.raises(TimeoutPolicyError):
                call("doc-1")
        finally:
            release.set()

        assert store.call_count == 1
        assert store.call_args.kwargs["domain"] == "svc.retried_slow"
        assert _failure_types(store) == ["MAX_RETRIES_TIMEOUTPOLICYERROR"]

    def test_async_retry_stage_cut_off_by_the_bound_parks_one_entry(self, store):
        """``aprotect`` accepts no pre-built ``RetryPolicy``, so the async half
        pins the retry-present timeout through a config."""

        @protected(
            "svc.aretried_slow",
            dlq=True,
            retry=_retry_config("svc.aretried_slow"),
            circuit_breaker=False,
            timeout=_BOUND_SECONDS,
        )
        async def call(doc_id: str) -> str:
            await asyncio.Event().wait()  # never set: only the bound ends it
            return "never"

        with pytest.raises(TimeoutPolicyError):
            asyncio.run(call("doc-1"))

        assert store.call_count == 1
        assert store.call_args.kwargs["domain"] == "svc.aretried_slow"
        assert _failure_types(store) == ["MAX_RETRIES_TIMEOUTPOLICYERROR"]

    # --- (e) the breaker opens mid-outage -----------------------------------

    def test_sync_open_breaker_parks_the_rejected_call_once_as_open_circuit(
        self, store, breaker_opening_after_one_failure
    ):
        # Given the README profile — breaker on by default — over a breaker
        # that opens on its first failure
        name = breaker_opening_after_one_failure

        @protected(name, dlq=True, timeout=None)
        def call(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        # When one call fails and the next is rejected by the open breaker
        with pytest.raises(RuntimeError):
            call("doc-1")
        with pytest.raises(CircuitBreakerOpenError):
            call("doc-2")

        # Then each call is parked exactly once, the rejected one as open-circuit
        assert _failure_types(store) == [
            "MAX_RETRIES_RUNTIMEERROR",
            OPEN_CIRCUIT_FAILURE_TYPE,
        ]
        assert store.call_args_list[1].kwargs["request_data"] == {"doc_id": "doc-2"}

    def test_async_open_breaker_parks_the_rejected_call_once_as_open_circuit(
        self, store, breaker_opening_after_one_failure
    ):
        name = breaker_opening_after_one_failure

        @protected(name, dlq=True, timeout=None)
        async def call(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        with pytest.raises(RuntimeError):
            asyncio.run(call("doc-1"))
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(call("doc-2"))

        assert _failure_types(store) == [
            "MAX_RETRIES_RUNTIMEERROR",
            OPEN_CIRCUIT_FAILURE_TYPE,
        ]
        assert store.call_args_list[1].kwargs["request_data"] == {"doc_id": "doc-2"}

    # --- (f) dlq=False ------------------------------------------------------

    def test_sync_dlq_false_parks_nothing_and_writes_no_verdict(self, store):
        """The arming is ``dlq``-gated: a composer with no sink carries no
        verdict for ``protect_with_meta`` to report."""
        result = protect_with_meta(
            "svc.no_dlq",
            _raise_upstream,
            dlq=False,
            circuit_breaker=False,
            timeout=None,
        )

        assert result.success is False
        assert "should_dlq" not in result.metadata
        store.assert_not_called()

    def test_async_dlq_false_parks_nothing_and_writes_no_verdict(self, store):
        result = asyncio.run(
            aprotect_with_meta(
                "svc.ano_dlq",
                _araise_upstream,
                dlq=False,
                circuit_breaker=False,
                timeout=None,
            )
        )

        assert result.success is False
        assert "should_dlq" not in result.metadata
        store.assert_not_called()

    # --- (g) observe-only mode ----------------------------------------------

    def test_sync_observe_only_suppresses_the_store_at_the_sink(self, store):
        """The arming still marks the call for storage under its name; the
        sink's observe-only guard is what withholds the store."""
        with dry_run_active():
            result = protect_with_meta(
                "svc.dry",
                _raise_upstream,
                dlq=True,
                circuit_breaker=False,
                timeout=None,
            )

        assert result.success is False
        assert result.metadata["should_dlq"] is True
        assert result.metadata["domain"] == "svc.dry"
        store.assert_not_called()

    def test_async_observe_only_suppresses_the_store_at_the_sink(self, store):
        with dry_run_active():
            result = asyncio.run(
                aprotect_with_meta(
                    "svc.adry",
                    _araise_upstream,
                    dlq=True,
                    circuit_breaker=False,
                    timeout=None,
                )
            )

        assert result.success is False
        assert result.metadata["should_dlq"] is True
        assert result.metadata["domain"] == "svc.adry"
        store.assert_not_called()

    # --- (h) dlq switched on by the settings default ------------------------

    def test_sync_default_dlq_setting_arms_a_call_without_a_dlq_argument(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("BALDUR_PROTECT_DEFAULT_DLQ", "true")
        reset_protect_settings()

        @protected("svc.by_default", circuit_breaker=False, timeout=None)
        def call(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        with pytest.raises(RuntimeError):
            call("doc-1")

        store.assert_called_once()
        kwargs = store.call_args.kwargs
        assert kwargs["domain"] == "svc.by_default"
        assert kwargs["failure_type"] == "MAX_RETRIES_RUNTIMEERROR"

    def test_async_default_dlq_setting_arms_a_call_without_a_dlq_argument(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("BALDUR_PROTECT_DEFAULT_DLQ", "true")
        reset_protect_settings()

        @protected("svc.aby_default", circuit_breaker=False, timeout=None)
        async def call(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        with pytest.raises(RuntimeError):
            asyncio.run(call("doc-1"))

        store.assert_called_once()
        kwargs = store.call_args.kwargs
        assert kwargs["domain"] == "svc.aby_default"
        assert kwargs["failure_type"] == "MAX_RETRIES_RUNTIMEERROR"
