"""
Hedging Policy — Tail Latency reduction via parallel competitive execution.

An independent Policy implementation that sends the same request to multiple
candidates concurrently and adopts the fastest response. It directly implements
the ResiliencePolicy Protocol without inheriting FallbackStrategy.

Reuses the existing HedgingExecutor/AsyncHedgingExecutor as-is, and expresses
Bulkhead control via per_candidate_policy/overall_policy injection.
EventBus subscription is separated into HedgingConfigUpdateHook so that
HedgingPolicy works even without an EventBus.

Composition:
- HedgingPolicy: synchronous Hedging (implements ResiliencePolicy Protocol)
- AsyncHedgingPolicy: asynchronous Hedging (implements AsyncResiliencePolicy Protocol)
- HedgingConfigUpdateHook: mediates EventBus CONFIG_UPDATED → HedgingPolicy config update

Backpressure logic (delay adjustment/disabling by load level) is Hedging-specific logic,
so it is kept inside the Policy. Only the way the load level is updated is separated into HedgingConfigUpdateHook.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

import structlog

from baldur.interfaces.resilience_policy import (
    AsyncResiliencePolicy,
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)

if TYPE_CHECKING:
    # Imported for return-type narrowing of the lazy-imported HedgingExecutor /
    # AsyncHedgingExecutor instances (declared `Any` at runtime via the
    # bottom-of-module try/except). The noqa is required because the runtime
    # `HedgingResult` reference exists only inside `cast(...)` calls, which the
    # ruff PostToolUse hook would otherwise treat as unused.
    from baldur_pro.services.hedging.result import HedgingResult  # noqa: F401

# baldur_pro.services.hedging.* imports are relocated to the bottom of this
# file to break the OSS<->PRO module-load cycle — see
# 503.

logger = structlog.get_logger()

T = TypeVar("T")

# BackpressureLevel order (low → high)
_LOAD_LEVEL_ORDER: dict[str, int] = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


# =============================================================================
# Factory helpers — produce zero-arg Callable[[], T] closures that survive
# HedgingCandidate.fn's structural type check. Closures defined with default
# args inline (e.g. `def f(x=val): ...`) appear as (x=...) -> T to mypy and
# do NOT satisfy Callable[[], T]; factories produce true zero-arg signatures.
# =============================================================================


def _make_primary_fn(
    func: Callable[..., T],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Callable[[], T]:
    """Bind a func + (args, kwargs) snapshot into a zero-arg callable."""

    def primary_fn() -> T:
        return func(*args, **kwargs)

    return primary_fn


def _make_policy_wrapped(
    original_fn: Callable[[], T],
    policy: ResiliencePolicy[T],
) -> Callable[[], T]:
    """Bind (fn, policy) into a zero-arg callable for sync hedging candidates."""

    def policy_wrapped() -> T:
        result = policy.execute(original_fn)
        if result.success:
            assert result.value is not None  # success outcome contract
            return result.value
        if result.outcome == PolicyOutcome.REJECTED:
            raise RuntimeError(f"Candidate rejected by policy: {result.outcome}")
        if result.outcome == PolicyOutcome.TIMEOUT:
            raise TimeoutError("Candidate timed out in policy")
        raise RuntimeError(f"Candidate policy failed: {result.outcome}")

    return policy_wrapped


def _make_async_primary_fn(
    func: Callable[..., Awaitable[T]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Callable[[], Awaitable[T]]:
    """Bind an async func + (args, kwargs) snapshot into a zero-arg callable."""

    async def primary_fn() -> T:
        return await func(*args, **kwargs)

    return primary_fn


def _make_async_policy_wrapped(
    original_fn: Callable[[], Awaitable[T]],
    policy: AsyncResiliencePolicy[T],
) -> Callable[[], Awaitable[T]]:
    """Bind (async_fn, async_policy) into a zero-arg awaitable for async hedging."""

    async def policy_wrapped() -> T:
        # AsyncResiliencePolicy.execute is declared `Callable[..., T]` even
        # though the impl awaits its result — Protocol-side drift outside
        # this file's scope. Cast at the boundary so the typed wrapper is clean.
        result = await policy.execute(cast(Callable[..., T], original_fn))
        if result.success:
            assert result.value is not None  # success outcome contract
            return result.value
        if result.outcome == PolicyOutcome.REJECTED:
            raise RuntimeError(f"Candidate rejected by policy: {result.outcome}")
        if result.outcome == PolicyOutcome.TIMEOUT:
            raise TimeoutError("Candidate timed out in policy")
        raise RuntimeError(f"Candidate policy failed: {result.outcome}")

    return policy_wrapped


# =============================================================================
# HedgingPolicy — synchronous Hedging Policy
# =============================================================================


class HedgingPolicy(ResiliencePolicy[T], Generic[T]):
    """
    Synchronous Hedging Policy — parallel competitive execution.

    Directly implements the ResiliencePolicy Protocol without inheriting FallbackStrategy.
    Internally reuses the existing HedgingExecutor.

    Bulkhead control is expressed via external injection of per_candidate_policy/overall_policy:
    - per_candidate_policy: Policy applied to each candidate (Bulkhead, Timeout, etc.)
    - overall_policy: Policy applied to the whole hedging (Bulkhead, Timeout, etc.)

    Backpressure logic (load-based delay adjustment/disabling) is Hedging-specific logic,
    so it is kept inside. The load level is updated externally via on_config_updated().

    Usage example::

        hedging = HedgingPolicy(
            candidates=[fetch_region_b, fetch_region_c],
            config=HedgingConfig(mode=HedgingMode.DELAYED, delay=0.1),
            overall_policy=BulkheadPolicy(
                bulkhead=SemaphoreBulkhead("api_bulkhead", max_concurrent=10),
            ),
        )
        result = hedging.execute(fetch_region_a)
    """

    def __init__(
        self,
        candidates: list[Callable[[], T]] | None = None,
        candidate_names: list[str] | None = None,
        config: HedgingConfig | None = None,
        default_value: T | None = None,
        per_candidate_policy: ResiliencePolicy[T] | None = None,
        overall_policy: ResiliencePolicy[T] | None = None,
        initial_load_level: str = "none",
    ):
        """
        Args:
            candidates: list of candidate functions.
            candidate_names: list of candidate names (optional).
            config: hedging configuration.
            default_value: default value when all candidates fail.
            per_candidate_policy: Policy applied to each candidate (Bulkhead, Timeout, etc.).
            overall_policy: Policy applied to the whole hedging (Bulkhead, Timeout, etc.).
            initial_load_level: initial load level ("none"|"low"|"medium"|"high"|"critical").
        """
        # Fail-closed: Hedging is a PRO-tier feature. The module-level imports at
        # the bottom of this file bind HedgingExecutor / HedgingConfig to None
        # when baldur_pro is absent; raise a clear error here so the object is
        # never constructed in a half-PRO state (the executor build below would
        # otherwise crash with a bare TypeError).
        if HedgingExecutor is None or HedgingConfig is None:
            raise RuntimeError("HedgingPolicy requires baldur_pro")

        from baldur.settings.hedging import get_hedging_settings

        self._globally_enabled = get_hedging_settings().enabled
        self._candidates = candidates or []
        self._candidate_names = candidate_names or []
        self._config = config or HedgingConfig.from_settings()
        self._default_value = default_value
        self._per_candidate_policy = per_candidate_policy
        self._overall_policy = overall_policy
        self._executor = HedgingExecutor(self._config)
        self._current_load_level: str = initial_load_level

    @property
    def name(self) -> str:
        """Policy identifier."""
        return "hedging"

    def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Run hedging — implements the ResiliencePolicy Protocol.

        Execution order:
        1. Backpressure check → run single when disabled
        2. If overall_policy exists, wrap the whole (prevents Double Wrapping)
        3. Build the candidate list (func is the Primary)
        4. If per_candidate_policy exists, wrap each candidate
        5. Run in parallel via the Executor
        6. Convert to PolicyResult

        Args:
            func: Primary execution function.
            *args: function positional arguments.
            context: execution context (propagated to Guard/Hook/Sink).
            **kwargs: function keyword arguments.

        Returns:
            PolicyResult[T]: the unified result. Does not raise.
        """
        if not self._globally_enabled:
            return self._execute_single(func, *args, **kwargs)

        if self._should_disable_hedging():
            return self._execute_single(func, *args, **kwargs)

        if self._overall_policy is not None:
            return self._execute_with_overall_policy(func, *args, **kwargs)

        return self._execute_hedging(func, *args, **kwargs)

    def on_config_updated(self, event: dict[str, Any]) -> None:
        """
        Event handler that updates the config from the outside (HedgingConfigUpdateHook).

        A single-field assignment is a single STORE_ATTR bytecode operation under
        the CPython GIL, so no tearing occurs.
        However, composite consistency when mode and delay change simultaneously is not guaranteed.

        Args:
            event: config-change event (includes key, value fields).
        """
        config_key = event.get("key", "")
        config_value = event.get("value")

        if config_key == "hedging.mode" and config_value:
            try:
                self._config.mode = HedgingMode(config_value)
                logger.info(
                    "hedging_policy.mode_changed",
                    config_value=config_value,
                )
            except ValueError:
                logger.warning(
                    "hedging_policy.invalid_mode",
                    config_value=config_value,
                )
        elif config_key == "hedging.delay" and config_value is not None:
            self._config.delay = float(config_value)
            logger.info(
                "hedging_policy.delay_changed",
                config_value=config_value,
            )
        elif config_key == "backpressure.level" and config_value:
            self._current_load_level = config_value.lower()
            logger.info(
                "hedging_policy.load_level_updated",
                current_load_level=self._current_load_level,
            )

    # -------------------------------------------------------------------------
    # Internal execution methods
    # -------------------------------------------------------------------------

    def _execute_with_overall_policy(
        self,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Apply overall_policy — prevent Double Wrapping.

        hedging_as_single() returns only the raw value (T), and
        the hedging metadata is captured by the closure and merged into the final result.
        This prevents overall_policy.execute() from double-wrapping into
        PolicyResult(value=PolicyResult(...)).
        """
        hedging_metadata: dict[str, Any] = {}

        def hedging_as_single() -> T:
            inner = self._execute_hedging(func, *args, **kwargs)
            hedging_metadata.update(inner.metadata)
            if inner.success:
                assert inner.value is not None  # success outcome contract
                return inner.value
            raise inner.error or HedgingError("All candidates failed")

        assert self._overall_policy is not None  # checked in execute()
        try:
            result = self._overall_policy.execute(hedging_as_single)
        except HedgingError as e:
            if self._default_value is not None:
                return PolicyResult(
                    value=self._default_value,
                    outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                    error=e,
                    executed_policies=["hedging"],
                    metadata={"hedging_all_failed": True},
                )
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.FAILURE,
                error=e,
                executed_policies=["hedging"],
            )

        # overall_policy REJECTED/TIMEOUT → return as-is (hedging not run)
        if not result.success:
            result.executed_policies.append("hedging")
            return result

        # SUCCESS → merge hedging metadata
        result.metadata.update(hedging_metadata)
        result.executed_policies.append("hedging")
        return result

    def _execute_hedging(
        self,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        The actual hedging execution logic.

        Applies the effective delay according to Backpressure,
        builds the candidate list, and runs it in parallel via the Executor.
        """
        original_delay = self._config.delay
        try:
            self._config.delay = self._get_effective_delay()

            candidates = self._build_candidates(func, *args, **kwargs)

            if len(candidates) == 1:
                return self._execute_single(func, *args, **kwargs)

            if self._per_candidate_policy is not None:
                candidates = self._wrap_candidates_with_policy(candidates)

            result: HedgingResult[T] = self._executor.execute(candidates)

            return PolicyResult(
                value=cast(T, result.value),
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=["hedging"],
                metadata={
                    "hedged": result.hedged,
                    "winner": result.source,
                    "latency_ms": result.latency_ms,
                    "hedging_benefit_ms": result.hedging_benefit_ms,
                },
            )
        except HedgingError as e:
            if self._default_value is not None:
                return PolicyResult(
                    value=self._default_value,
                    outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                    error=e,
                    executed_policies=["hedging"],
                    metadata={"hedging_all_failed": True},
                )
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.FAILURE,
                error=e,
                executed_policies=["hedging"],
            )
        finally:
            self._config.delay = original_delay

    def _wrap_candidates_with_policy(
        self,
        candidates: list[HedgingCandidate[T]],
    ) -> list[HedgingCandidate[T]]:
        """
        Wrap each candidate with per_candidate_policy.

        PolicyResult → Raw value conversion (prevents Double Wrapping):
        - result.success → return result.value (SUCCESS + SUCCESS_WITH_FALLBACK)
        - REJECTED → raise RuntimeError → the Executor treats it as a candidate failure
        - TIMEOUT → raise TimeoutError
        - other → raise RuntimeError
        """
        assert self._per_candidate_policy is not None  # checked in execute()
        wrapped: list[HedgingCandidate[T]] = []
        for candidate in candidates:
            wrapped.append(
                HedgingCandidate(
                    name=candidate.name,
                    fn=_make_policy_wrapped(candidate.fn, self._per_candidate_policy),
                    priority=candidate.priority,
                    metadata=candidate.metadata,
                )
            )
        return wrapped

    def _build_candidates(
        self,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> list[HedgingCandidate[T]]:
        """
        Build the candidate list.

        Combines the func of execute(func, *args, **kwargs) as the Primary and
        the constructor's candidates as Secondary.
        Wraps func + args into a no-arg callable to match
        the HedgingCandidate.fn signature.
        """
        candidates: list[HedgingCandidate[T]] = []

        candidates.append(
            HedgingCandidate(
                name=self._get_name(0, "primary"),
                fn=_make_primary_fn(func, args, kwargs),
                priority=0,
            )
        )

        for i, fn in enumerate(self._candidates):
            candidates.append(
                HedgingCandidate(
                    name=self._get_name(i + 1, f"candidate_{i + 1}"),
                    fn=fn,
                    priority=i + 1,
                )
            )

        return candidates[: self._config.max_candidates]

    def _execute_single(
        self,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """Run a single function (no hedging)."""
        try:
            result = func(*args, **kwargs)
            return PolicyResult(
                value=result,
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=["hedging"],
                metadata={"hedged": False},
            )
        except Exception as e:
            if self._default_value is not None:
                return PolicyResult(
                    value=self._default_value,
                    outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                    error=e,
                    executed_policies=["hedging"],
                    metadata={"hedged": False, "single_failed": True},
                )
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.FAILURE,
                error=e,
                executed_policies=["hedging"],
            )

    def _get_name(self, index: int, default: str) -> str:
        """Return the candidate name."""
        if index < len(self._candidate_names):
            return self._candidate_names[index]
        return default

    def _should_disable_hedging(self) -> bool:
        """
        Return True if the current load level is at or above disable_on_load_level.

        Under high load, hedging is disabled to reduce resource usage.
        """
        current_order = _LOAD_LEVEL_ORDER.get(self._current_load_level, 0)
        disable_order = _LOAD_LEVEL_ORDER.get(self._config.disable_on_load_level, 3)
        return current_order >= disable_order

    def _get_effective_delay(self) -> float:
        """
        Return the actual delay according to the current load level.

        NONE/LOW: base delay
        MEDIUM: delay * delay_multiplier_on_medium
        HIGH: delay * delay_multiplier_on_high
        """
        if self._current_load_level == "medium":
            return self._config.delay * self._config.delay_multiplier_on_medium
        if self._current_load_level == "high":
            return self._config.delay * self._config.delay_multiplier_on_high
        return self._config.delay


# =============================================================================
# AsyncHedgingPolicy — asynchronous Hedging Policy
# =============================================================================


class AsyncHedgingPolicy(Generic[T]):
    """
    Asynchronous Hedging Policy — implements the AsyncResiliencePolicy Protocol.

    Same structure as the synchronous HedgingPolicy but supports async execution.
    Internally reuses the existing AsyncHedgingExecutor.

    Consumer Responsibility:
    The functions passed in candidates must be async def.
    To mix in synchronous functions, the consumer wraps them with asyncio.to_thread() before injecting.

    Usage example::

        hedging = AsyncHedgingPolicy(
            candidates=[async_fetch_b, async_fetch_c],
            config=HedgingConfig(mode=HedgingMode.DELAYED, delay=0.1),
        )
        result = await hedging.execute(async_fetch_a)

    Raises:
        TypeError: when per_candidate_policy/overall_policy does not
                   conform to the AsyncResiliencePolicy Protocol.
    """

    def __init__(
        self,
        candidates: list[Callable[[], Awaitable[T]]] | None = None,
        candidate_names: list[str] | None = None,
        config: HedgingConfig | None = None,
        default_value: T | None = None,
        per_candidate_policy: AsyncResiliencePolicy[T] | None = None,
        overall_policy: AsyncResiliencePolicy[T] | None = None,
        initial_load_level: str = "none",
    ):
        """
        Args:
            candidates: list of candidate coroutine functions.
            candidate_names: list of candidate names (optional).
            config: hedging configuration.
            default_value: default value when all candidates fail.
            per_candidate_policy: async Policy applied to each candidate.
            overall_policy: async Policy applied to the whole hedging.
            initial_load_level: initial load level ("none"|"low"|"medium"|"high"|"critical").

        Raises:
            TypeError: when per_candidate_policy/overall_policy does not
                       conform to the AsyncResiliencePolicy Protocol.
        """
        if per_candidate_policy is not None and not isinstance(
            per_candidate_policy, AsyncResiliencePolicy
        ):
            raise TypeError(
                f"per_candidate_policy must implement AsyncResiliencePolicy, "
                f"got {type(per_candidate_policy).__name__}. "
                f"Using a synchronous Policy in an async environment "
                f"raises TypeError at 'await policy.execute()'."
            )
        if overall_policy is not None and not isinstance(
            overall_policy, AsyncResiliencePolicy
        ):
            raise TypeError(
                f"overall_policy must implement AsyncResiliencePolicy, "
                f"got {type(overall_policy).__name__}"
            )

        # Fail-closed: see HedgingPolicy.__init__ — AsyncHedgingExecutor /
        # HedgingConfig are None when baldur_pro is absent.
        if AsyncHedgingExecutor is None or HedgingConfig is None:
            raise RuntimeError("AsyncHedgingPolicy requires baldur_pro")

        from baldur.settings.hedging import get_hedging_settings

        self._globally_enabled = get_hedging_settings().enabled
        self._candidates = candidates or []
        self._candidate_names = candidate_names or []
        self._config = config or HedgingConfig.from_settings()
        self._default_value = default_value
        self._per_candidate_policy = per_candidate_policy
        self._overall_policy = overall_policy
        self._executor = AsyncHedgingExecutor(self._config)
        self._current_load_level: str = initial_load_level

    @property
    def name(self) -> str:
        """Policy identifier."""
        return "hedging"

    async def execute(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Run async hedging — implements the AsyncResiliencePolicy Protocol.

        Same flow as the synchronous HedgingPolicy.execute():
        1. Backpressure check
        2. Apply overall_policy (prevents Double Wrapping)
        3. Run hedging (reuses AsyncHedgingExecutor)

        Args:
            func: Primary async execution function.
            *args: function positional arguments.
            context: execution context.
            **kwargs: function keyword arguments.

        Returns:
            PolicyResult[T]: the unified result. Does not raise.
        """
        if not self._globally_enabled:
            return await self._execute_single(func, *args, **kwargs)

        if self._should_disable_hedging():
            return await self._execute_single(func, *args, **kwargs)

        if self._overall_policy is not None:
            return await self._execute_with_overall_policy(func, *args, **kwargs)

        return await self._execute_hedging(func, *args, **kwargs)

    def on_config_updated(self, event: dict[str, Any]) -> None:
        """
        Event handler that updates the config from the outside (HedgingConfigUpdateHook).

        Same logic as the synchronous HedgingPolicy.on_config_updated().

        Args:
            event: config-change event (includes key, value fields).
        """
        config_key = event.get("key", "")
        config_value = event.get("value")

        if config_key == "hedging.mode" and config_value:
            try:
                self._config.mode = HedgingMode(config_value)
                logger.info(
                    "async_hedging_policy.mode_changed",
                    config_value=config_value,
                )
            except ValueError:
                logger.warning(
                    "async_hedging_policy.invalid_mode",
                    config_value=config_value,
                )
        elif config_key == "hedging.delay" and config_value is not None:
            self._config.delay = float(config_value)
            logger.info(
                "async_hedging_policy.delay_changed",
                config_value=config_value,
            )
        elif config_key == "backpressure.level" and config_value:
            self._current_load_level = config_value.lower()
            logger.info(
                "async_hedging_policy.load_level_updated",
                current_load_level=self._current_load_level,
            )

    # -------------------------------------------------------------------------
    # Internal execution methods
    # -------------------------------------------------------------------------

    async def _execute_with_overall_policy(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Apply overall_policy — prevent Double Wrapping (async version).

        hedging_as_single() returns only the raw value (T), and
        the hedging metadata is captured by the closure and merged into the final result.
        """
        hedging_metadata: dict[str, Any] = {}

        async def hedging_as_single() -> T:
            inner = await self._execute_hedging(func, *args, **kwargs)
            hedging_metadata.update(inner.metadata)
            if inner.success:
                assert inner.value is not None  # success outcome contract
                return inner.value
            raise inner.error or HedgingError("All candidates failed")

        assert self._overall_policy is not None  # checked in execute()
        try:
            # AsyncResiliencePolicy.execute Protocol declares `Callable[..., T]`
            # despite awaiting the result internally — cast at the boundary.
            result = await self._overall_policy.execute(
                cast(Callable[..., T], hedging_as_single)
            )
        except HedgingError as e:
            if self._default_value is not None:
                return PolicyResult(
                    value=self._default_value,
                    outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                    error=e,
                    executed_policies=["hedging"],
                    metadata={"hedging_all_failed": True},
                )
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.FAILURE,
                error=e,
                executed_policies=["hedging"],
            )

        if not result.success:
            result.executed_policies.append("hedging")
            return result

        result.metadata.update(hedging_metadata)
        result.executed_policies.append("hedging")
        return result

    async def _execute_hedging(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """The actual async hedging execution logic."""
        original_delay = self._config.delay
        try:
            self._config.delay = self._get_effective_delay()

            candidates = self._build_candidates(func, *args, **kwargs)

            if len(candidates) == 1:
                return await self._execute_single(func, *args, **kwargs)

            if self._per_candidate_policy is not None:
                candidates = self._wrap_candidates_with_policy(candidates)

            result: HedgingResult[T] = await self._executor.execute(candidates)

            return PolicyResult(
                value=cast(T, result.value),
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=["hedging"],
                metadata={
                    "hedged": result.hedged,
                    "winner": result.source,
                    "latency_ms": result.latency_ms,
                    "hedging_benefit_ms": result.hedging_benefit_ms,
                },
            )
        except HedgingError as e:
            if self._default_value is not None:
                return PolicyResult(
                    value=self._default_value,
                    outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                    error=e,
                    executed_policies=["hedging"],
                    metadata={"hedging_all_failed": True},
                )
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.FAILURE,
                error=e,
                executed_policies=["hedging"],
            )
        finally:
            self._config.delay = original_delay

    def _wrap_candidates_with_policy(
        self,
        candidates: list[HedgingCandidate[Awaitable[T]]],
    ) -> list[HedgingCandidate[Awaitable[T]]]:
        """
        Wrap each candidate with per_candidate_policy (async version).

        PolicyResult → Raw value conversion (prevents Double Wrapping).
        Awaits the async Policy's execute() to unwrap the result.
        """
        assert self._per_candidate_policy is not None  # checked in execute()
        wrapped: list[HedgingCandidate[Awaitable[T]]] = []
        for candidate in candidates:
            wrapped.append(
                HedgingCandidate(
                    name=candidate.name,
                    fn=_make_async_policy_wrapped(
                        candidate.fn, self._per_candidate_policy
                    ),
                    priority=candidate.priority,
                    metadata=candidate.metadata,
                )
            )
        return wrapped

    def _build_candidates(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> list[HedgingCandidate[Awaitable[T]]]:
        """
        Build the candidate list (async version).

        Wraps func + args into a no-arg async callable.
        """
        candidates: list[HedgingCandidate[Awaitable[T]]] = []

        candidates.append(
            HedgingCandidate(
                name=self._get_name(0, "primary"),
                fn=_make_async_primary_fn(func, args, kwargs),
                priority=0,
            )
        )

        for i, fn in enumerate(self._candidates):
            candidates.append(
                HedgingCandidate(
                    name=self._get_name(i + 1, f"candidate_{i + 1}"),
                    fn=fn,
                    priority=i + 1,
                )
            )

        return candidates[: self._config.max_candidates]

    async def _execute_single(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """Run a single function (no hedging, async)."""
        try:
            result = await func(*args, **kwargs)
            return PolicyResult(
                value=result,
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=["hedging"],
                metadata={"hedged": False},
            )
        except Exception as e:
            if self._default_value is not None:
                return PolicyResult(
                    value=self._default_value,
                    outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK,
                    error=e,
                    executed_policies=["hedging"],
                    metadata={"hedged": False, "single_failed": True},
                )
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.FAILURE,
                error=e,
                executed_policies=["hedging"],
            )

    def _get_name(self, index: int, default: str) -> str:
        """Return the candidate name."""
        if index < len(self._candidate_names):
            return self._candidate_names[index]
        return default

    def _should_disable_hedging(self) -> bool:
        """Return True if the current load level is at or above disable_on_load_level."""
        current_order = _LOAD_LEVEL_ORDER.get(self._current_load_level, 0)
        disable_order = _LOAD_LEVEL_ORDER.get(self._config.disable_on_load_level, 3)
        return current_order >= disable_order

    def _get_effective_delay(self) -> float:
        """Return the actual delay according to the current load level."""
        if self._current_load_level == "medium":
            return self._config.delay * self._config.delay_multiplier_on_medium
        if self._current_load_level == "high":
            return self._config.delay * self._config.delay_multiplier_on_high
        return self._config.delay


# =============================================================================
# HedgingConfigUpdateHook — mediates EventBus → HedgingPolicy config update
# =============================================================================


class HedgingConfigUpdateHook:
    """
    Hook that forwards EventBus CONFIG_UPDATED events to HedgingPolicy/AsyncHedgingPolicy.

    Mediates so that HedgingPolicy does not depend directly on the EventBus.
    Fail-Open principle: a Hook failure does not affect HedgingPolicy behavior.

    Usage example::

        hook = HedgingConfigUpdateHook()
        hook.register(hedging_policy)
        hook.start()  # start EventBus subscription
    """

    def __init__(self) -> None:
        self._policies: list[HedgingPolicy | AsyncHedgingPolicy] = []
        self._subscribed = False

    def register(self, policy: HedgingPolicy | AsyncHedgingPolicy) -> None:
        """Register a Policy to receive config updates."""
        self._policies.append(policy)

    def start(self) -> None:
        """
        Start the EventBus CONFIG_UPDATED subscription.

        Does nothing in environments without an EventBus (Fail-Open).
        """
        if self._subscribed:
            return
        try:
            from baldur.services.event_bus import EventType, get_event_bus
            from baldur.services.event_bus.bus.event_types import EventPriority

            bus = get_event_bus()
            bus.subscribe(
                EventType.CONFIG_UPDATED, self._dispatch, EventPriority.NORMAL
            )
            self._subscribed = True
            logger.debug("hedging_config_update_hook.subscribed")
        except ImportError:
            logger.debug("hedging_config_update_hook.eventbus_available")
        except Exception as e:
            logger.warning(
                "hedging_config_update_hook.subscribe_failed",
                error=e,
            )

    def stop(self) -> None:
        """Unsubscribe EventBus handler."""
        if not self._subscribed:
            return
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.unsubscribe(EventType.CONFIG_UPDATED, self._dispatch)
            self._subscribed = False
        except ImportError:
            pass
        except Exception:
            pass

    def _dispatch(self, event: Any) -> None:
        """Forward the event to all registered Policies (Fail-Open)."""
        event_data = event.data if hasattr(event, "data") else event
        for policy in self._policies:
            try:
                policy.on_config_updated(event_data)
            except Exception:
                pass  # Fail-Open: a Hook failure does not stop Policy behavior


# =============================================================================
# OSS<->PRO cycle break — see 503.
# These imports MUST stay below all class definitions in this file: they pull
# in baldur_pro.services.hedging.__init__, which re-exports HedgingPolicy /
# AsyncHedgingPolicy / HedgingConfigUpdateHook from this module. Placing them
# at module top causes cold-start ImportError on the partially-loaded module
# (AsyncHedgingPolicy not yet bound). Do not move above class definitions.
# The singleton-factory import below shares this section because the factory
# call is the only thing that consumes it, and grouping keeps a single block
# of deferred imports per ruff-isort.
# =============================================================================

from baldur.utils.singleton import CLEANUP_STOP, make_singleton_factory  # noqa: E402

# pro-guard-exempt: PRO-shim file (503 circular-import cycle-break). Hedging is a
# PRO feature; it lives in OSS only to break the import cycle. The names bound to
# None below are used unconditionally throughout this module's methods, but
# HedgingPolicy/AsyncHedgingPolicy.__init__ raise RuntimeError when they are None,
# so every downstream use is unreachable PRO-absent — a property the static G51
# gate cannot prove (it needs whole-program reachability). See 659 D8.
try:
    from baldur_pro.services.hedging.async_executor import (  # noqa: E402
        AsyncHedgingExecutor,
    )
except ImportError:
    AsyncHedgingExecutor = None  # type: ignore[assignment,misc]
try:
    from baldur_pro.services.hedging.config import (  # noqa: E402
        HedgingCandidate,
        HedgingConfig,
        HedgingMode,
    )
except ImportError:
    HedgingCandidate = None  # type: ignore[assignment,misc]
    HedgingConfig = None  # type: ignore[assignment,misc]
    HedgingMode = None  # type: ignore[assignment,misc]
try:
    from baldur_pro.services.hedging.exceptions import HedgingError  # noqa: E402
except ImportError:
    HedgingError = None  # type: ignore[assignment,misc]
try:
    from baldur_pro.services.hedging.executor import HedgingExecutor  # noqa: E402
except ImportError:
    HedgingExecutor = None  # type: ignore[assignment,misc]

(
    get_hedging_config_update_hook,
    configure_hedging_config_update_hook,
    reset_hedging_config_update_hook,
) = make_singleton_factory(
    "hedging_config_update_hook",
    HedgingConfigUpdateHook,
    cleanup_fn=CLEANUP_STOP,
)
