"""
Rate Limit Coordinator - Core Coordinator

Central coordinator for distributed rate limit management.
Prevents Self-DDoS by coordinating retry behavior across workers: when one
worker is told to back off by a 429, the others honor the same cooldown
instead of each running its own backoff ladder against the same provider.

Key Features:
    - Shared cooldown on 429 responses
    - Exponential backoff with jitter
    - Distributed state via pluggable storage

Coverage:
    Baldur's own *synchronous* retry stage consults this coordinator by
    default, provided the call carries a domain identity — the coordination key
    is ``rate_limit_key`` or, failing that, ``domain``. A call that never named
    a domain is not coordinated, because the placeholder is shared by every
    such caller and one cooldown record cannot stand for unrelated downstreams.
    Two levers turn the default off: ``rate_limit_aware`` on the retry config
    and ``BALDUR_RATE_LIMIT_BACKOFF_COORDINATION_ENABLED``. Passing a
    coordinator explicitly overrides both.

    Detection is exception-borne: the retry stage classifies a 429 from the
    exception a call raises. A client that reports the 429 as a returned value
    instead installs no cooldown, even when a result predicate retries on it.

    Asynchronous surfaces do not participate in the default. Async callers who
    want outbound 429 coordination opt in through the tenacity bridge with a
    ``rate_limit_key``; a bring-your-own retry engine owns its own coordination.

    The cooldown is shared per key, so two call sites hitting the same provider
    under different domains do not share one — the key is the unit of
    coordination, and there is no provider-level auto-grouping.

Storage:
    Redis when configured (shared across processes and hosts), otherwise an
    in-process store (per-process cooldown only).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import structlog

from baldur.adapters.rate_limit import get_rate_limit_storage
from baldur.core.backoff import ExponentialBackoff
from baldur.core.rate_limiting import CooldownGate
from baldur.interfaces.rate_limit_storage import (
    RateLimitState,
    RateLimitStorageInterface,
)
from baldur.utils.retry_after import parse_retry_after

from .announcer import CooldownAnnouncer
from .helpers import (
    _default_get_retry_after,
    _default_is_429,
    _emit_rate_limit_event,
    _record_rate_limit_429,
    _record_rate_limit_cooldown,
    _record_rate_limit_deferral,
    _record_rate_limit_wait,
)
from .models import (
    RateLimitCoordinatorConfig,
    RateLimitDeferredError,
    RateLimitResult,
)

logger = structlog.get_logger()

T = TypeVar("T")

# Minimum cooldown floor (seconds) applied after backoff+jitter so a 429 always
# yields a non-trivial wait even when jitter drives the computed delay toward zero.
_MIN_COOLDOWN_SECONDS: float = 0.1


class RateLimitCoordinator:
    """
    Coordinates rate limiting across distributed workers.

    Prevents Self-DDoS by:
    1. Detecting 429 responses
    2. Setting global cooldown (shared across all workers)
    3. Making all workers wait before retrying
    4. Using exponential backoff with jitter

    Most callers never construct this directly. Baldur's synchronous retry
    stage resolves the shared coordinator itself, so naming a domain is all it
    takes::

        # Coordinated by default — "payment_api" is the coordination key.
        # The 429 must surface as an *exception* for the retry stage to see
        # it: a client that returns the response object instead (``requests``
        # without ``raise_for_status``) neither retries nor coordinates.
        @retry(domain="payment_api")
        def call_external_api():
            response = requests.post(...)
            response.raise_for_status()
            return response

        protect("payment_api", retry=True)(call_external_api)

    Opting out, in order of narrowness::

        # Per policy / per domain.
        RetryPolicyConfig(domain="payment_api", rate_limit_aware=False)

        # Whole deployment.
        BALDUR_RATE_LIMIT_BACKOFF_COORDINATION_ENABLED=false

    Driving it directly — for a call that Baldur's retry stage does not wrap,
    or to override the resolved instance (explicit injection wins over both
    opt-outs above)::

        coordinator = RateLimitCoordinator.get_instance()

        coordinator.wait_if_needed("payment_api")   # before the request
        coordinator.on_rate_limited(                # after a 429
            key="payment_api",
            retry_after=response.headers.get("Retry-After"),
        )
        coordinator.on_success("payment_api")       # after a success

    With decorator:
        @coordinator.rate_limit_aware("payment_api")
        def call_external_api():
            return requests.post(...)
    """

    _instance: RateLimitCoordinator | None = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        storage: RateLimitStorageInterface | None = None,
        config: RateLimitCoordinatorConfig | None = None,
        announcer: CooldownAnnouncer | None = None,
    ) -> None:
        """
        Initialize rate limit coordinator.

        Args:
            storage: Rate limit storage backend (auto-detected if None)
            config: Rate limit configuration
            announcer: Cooldown-end announcer (built over ``storage`` if None).
                Injectable so a test can drive the verification pass
                synchronously instead of through the announcer's own thread.
        """
        self._storage = storage or get_rate_limit_storage()
        self._config = config or RateLimitCoordinatorConfig.from_settings()
        self._local_lock = threading.Lock()

        # EventBus debouncing gate (prevents duplicate event emission per key;
        # the shared gate also bounds the map that previously grew per key).
        self._debounce_gate = CooldownGate()

        # Canary state tracking (scout mode for the first request after cooldown)
        self._canary_in_progress: dict[str, bool] = {}
        self._canary_lock = threading.Lock()

        # Cooldown-end announcement. The announcer holds one record per key —
        # the effective expiry this process last learned — and verifies it
        # against the shared store before announcing, so a peer's extension this
        # process never observed cannot be announced away. Its thread starts on
        # the first tracked key, so a process that takes no 429 runs none.
        self._announcer = announcer or CooldownAnnouncer(storage=self._storage)

    @classmethod
    def get_instance(cls) -> RateLimitCoordinator:
        """Get singleton instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation.

        Stops the announcer thread before clearing the instance, so a later
        ``get_instance()`` does not leave two live emitters for one key.
        """
        with cls._instance_lock:
            instance = cls._instance
            if instance is not None:
                instance._announcer.stop()
            cls._instance = None

    @property
    def storage_type(self) -> str:
        """Get the type of storage backend being used."""
        return self._storage.storage_type.value

    # =========================================================================
    # EventBus Debouncing Methods
    # =========================================================================

    def _should_emit_event(self, key: str) -> bool:
        """
        Check debouncing - prevent duplicate events within the window per key.

        A failed emit is not rolled back: the reserved window is kept consumed,
        matching the previous check-then-record behavior (acceptable-by-design
        for a debounce).

        Args:
            key: Rate limit key

        Returns:
            Whether the event should be emitted
        """
        emitted, _token = self._debounce_gate.try_reserve(
            key, self._config.debounce_window_seconds
        )
        if not emitted:
            logger.debug(
                "rate_limit_coordinator.debounced_event_last_emit",
                rate_limit_key=key,
            )
        return emitted

    # =========================================================================
    # Canary Request Methods
    # =========================================================================

    def _check_canary_mode(self, key: str, state: RateLimitState) -> bool:
        """
        Check canary mode - whether this is the first request after cooldown.

        Args:
            key: Rate limit key
            state: Current rate limit state

        Returns:
            Whether this is a canary request
        """
        if state.consecutive_429s == 0:
            return False

        with self._canary_lock:
            if key not in self._canary_in_progress:
                self._canary_in_progress[key] = True
                logger.info(
                    "rate_limit_coordinator.canary_request_mode",
                    rate_limit_key=key,
                )
                return True

        return False

    def _clear_canary_state(self, key: str) -> None:
        """Clear canary state."""
        with self._canary_lock:
            if key in self._canary_in_progress:
                del self._canary_in_progress[key]
                logger.debug(
                    "rate_limit_coordinator.canary_state_cleared",
                    rate_limit_key=key,
                )

    def get_state(self, key: str) -> RateLimitState:
        """Get current rate limit state for a key."""
        return self._storage.get_state(key)

    def wait_if_needed(
        self, key: str, max_wait: float | None = None
    ) -> RateLimitResult:
        """
        Wait if currently in cooldown period, bounded by the caller's budget.

        Call this BEFORE making an external request.
        The first request right after cooldown is marked is_canary=True.

        Serve-or-defer: when the remaining cooldown fits within the bound the
        full remaining time is slept (``waited=True``). When it does not, the
        call returns immediately with ``deferred=True`` and **sleeps nothing** —
        any slice shorter than the remaining cooldown cannot make the request
        legal before the cooldown expires, so it would be wasted budget. The
        caller decides what to do next (requeue, fail, drop); the shared
        cooldown is left untouched either way.

        Args:
            key: Rate limit key (e.g., "payment_api", "external_service")
            max_wait: Maximum seconds this call may sleep. ``None`` uses the
                configured ``max_delay`` as the default bound; ``float("inf")``
                opts into an unbounded wait.

        Returns:
            RateLimitResult with wait information, canary mode flag, and — on a
            deferral — ``not_before`` (the cooldown's expiry timestamp).
        """
        # The announcer's own entry points only run on a 429 or an operator
        # clear, and a cooldown is exactly the window in which this process makes
        # neither call — so a thread that died (or a fork child that inherited
        # records and no thread) is revived from the request path instead.
        self._announcer.ensure_running()

        state = self._storage.get_state(key)

        if state.is_in_cooldown:
            wait_time = state.remaining_cooldown
            bound = self._config.max_delay if max_wait is None else max_wait

            if wait_time > bound:
                logger.warning(
                    "rate_limit_coordinator.wait_deferred",
                    wait_time=wait_time,
                    max_wait=bound,
                    key=key,
                    state=state.consecutive_429s,
                )
                _record_rate_limit_deferral(key=key)
                return RateLimitResult(
                    waited=False,
                    wait_time=0.0,
                    was_rate_limited=True,
                    consecutive_429s=state.consecutive_429s,
                    is_canary=False,
                    deferred=True,
                    not_before=state.cooldown_until,
                )

            logger.info(
                "rate_limit_coordinator.waiting",
                wait_time=wait_time,
                key=key,
                state=state.consecutive_429s,
            )

            _record_rate_limit_wait(key=key, wait_seconds=wait_time)

            time.sleep(wait_time)

            return RateLimitResult(
                waited=True,
                wait_time=wait_time,
                was_rate_limited=True,
                consecutive_429s=state.consecutive_429s,
                is_canary=False,
            )

        # Right after cooldown ends - check canary mode
        is_canary = self._check_canary_mode(key, state)

        return RateLimitResult(
            waited=False,
            wait_time=0.0,
            was_rate_limited=state.consecutive_429s > 0,
            consecutive_429s=state.consecutive_429s,
            is_canary=is_canary,
        )

    def on_rate_limited(
        self,
        key: str,
        retry_after: float | str | None = None,
        status_code: int = 429,
    ) -> float:
        """
        Handle a rate limit (429) response.

        Call this when you receive a 429 response.
        Sets a global cooldown for all workers and emits events.

        The stored cooldown is **monotonic**: it moves only later. A worker whose
        429 carried no header computes a short ladder delay, and letting that
        replace a peer's honored ``Retry-After`` would resume the whole fleet
        before the provider's stated earliest time. ``clear()`` remains the way
        to shorten one deliberately.

        Args:
            key: Rate limit key
            retry_after: Retry-After header value — seconds, or the raw header in
                either RFC 9110 form. An unparseable, negative or already-past
                value is treated as absent and the backoff ladder applies.
            status_code: HTTP status code (for logging)

        Returns:
            The cooldown now in force for the key, in seconds. This is the
            **effective** wait, which exceeds the delay this call computed
            whenever a peer's longer cooldown is still running.
        """
        # Count the 429 before any storage call: both of the calls below can
        # raise on a degraded backend and every caller wraps this handler
        # fail-open, so any later placement makes a storm invisible during
        # exactly the outage an operator needs the count for.
        _record_rate_limit_429(key=key, status_code=status_code)

        # Parsed at the boundary rather than at each caller: the documented
        # direct-drive form passes the raw header, and an uncoerced string
        # reaches the numeric comparison below and raises there — where every
        # caller's fail-open wrap drops the cooldown entirely.
        retry_after_seconds = parse_retry_after(retry_after)

        consecutive = self._storage.increment_consecutive_429s(key)

        delay, honored, clamped = self._compute_cooldown(
            key, consecutive, retry_after_seconds
        )

        # Monotonic merge: the store decides which cooldown wins, and everything
        # downstream reports the winner rather than this call's proposal.
        #
        # The announcer's in-flight bracket spans the write, not just the record
        # update: between the store accepting this cooldown and the record
        # carrying it, a verifying read would still see the pre-429 expiry it was
        # armed for and announce into the cooldown this call is installing. A
        # store that raises releases the marker without recording anything.
        candidate_until = time.time() + delay
        recorded_until: float | None = None
        self._announcer.begin(key)
        try:
            recorded_until = self._storage.extend_cooldown(key, candidate_until)
        finally:
            self._announcer.track(key, recorded_until)
        cooldown_until = recorded_until
        in_force = max(0.0, cooldown_until - time.time())

        # Recorded after the store, because the in-force value does not exist
        # until the merge returns. A store that raises therefore records neither
        # this histogram nor the consecutive gauge, while the 429 counter above
        # still climbs. That divergence is the degradation signal for a store
        # that raises — the database backend, or a bring-your-own adapter. The
        # shipped Redis adapter no longer raises when its server dies mid-run:
        # it serves a per-worker cooldown and reports the window on its own
        # fallback gauge, so this histogram keeps recording throughout.
        _record_rate_limit_cooldown(
            key=key,
            cooldown_seconds=in_force,
            consecutive_429s=consecutive,
        )

        # EventBus integration (debouncing applied — metrics above are not
        # debounced: a flattened counter is indistinguishable from a storm
        # abating)
        if self._should_emit_event(key):
            _emit_rate_limit_event(
                "RATE_LIMIT_429",
                {
                    "key": key,
                    "status_code": status_code,
                    "retry_after_header": retry_after,
                    "calculated_delay": delay,
                    "consecutive_429s": consecutive,
                    "cooldown_until": cooldown_until,
                    "retry_after_honored": honored,
                    "retry_after_clamped": clamped,
                },
                priority_name="HIGH",
            )

        # 317: Broadcast the 429 event cluster-wide via the Kafka distributed channel
        self._broadcast_to_cluster(key, consecutive, cooldown_until, delay)

        logger.warning(
            "rate_limit_coordinator.rate_limited",
            key=key,
            status_code=status_code,
            consecutive=consecutive,
            delay=delay,
            cooldown_in_force=in_force,
        )

        return in_force

    def _compute_cooldown(
        self,
        key: str,
        consecutive: int,
        retry_after: float | None,
    ) -> tuple[float, bool, bool]:
        """Compute the cooldown a 429 installs, honoring a provider Retry-After.

        The headerless ladder (exponential backoff with jitter, hard-capped at
        ``max_delay``) is always Baldur's own escalation guard. A provider header
        acts as a **floor** on top of it — never undercut by jitter, never
        amplified by the ladder multiplier — bounded above by
        ``retry_after_ceiling``.

        Args:
            key: Rate limit key (logging only)
            consecutive: Consecutive-429 count for this key (1-indexed)
            retry_after: Retry-After header value in seconds, when present

        Returns:
            ``(delay, retry_after_honored, retry_after_clamped)`` — the two flags
            mark a header honored beyond ``max_delay`` and a header cut down by
            the ceiling, respectively.
        """
        # Exponential backoff with jitter, composed from the canonical strategy
        # (jitter_factor == jitter_percent / 100 keeps the symmetric-uniform semantics).
        # Seeded from default_retry_after unconditionally: seeding it from the
        # header would escalate the provider's own stated wait on every repeat.
        backoff = ExponentialBackoff(
            base_delay=self._config.default_retry_after,
            multiplier=self._config.backoff_multiplier,
            max_delay=self._config.max_delay,
            jitter=True,
            jitter_factor=self._config.jitter_percent / 100.0,
        )
        ladder = backoff.calculate(consecutive)

        if retry_after is None or retry_after <= 0:
            return max(_MIN_COOLDOWN_SECONDS, ladder), False, False

        ceiling = self._config.retry_after_ceiling
        clamped = retry_after > ceiling
        # The header is a floor, so no jitter is applied to it: the stored value
        # is a single shared global and every worker wakes on the same
        # cooldown_until — dispersal buys nothing and could undercut the header.
        delay = max(_MIN_COOLDOWN_SECONDS, min(ceiling, max(retry_after, ladder)))
        honored = delay > self._config.max_delay

        if clamped:
            logger.warning(
                "rate_limit_coordinator.retry_after_clamped",
                key=key,
                retry_after=retry_after,
                ceiling=ceiling,
                delay=delay,
            )
        elif honored:
            logger.info(
                "rate_limit_coordinator.retry_after_honored",
                key=key,
                retry_after=retry_after,
                max_delay=self._config.max_delay,
                delay=delay,
            )

        return delay, honored, clamped

    def _broadcast_to_cluster(
        self,
        key: str,
        consecutive_429s: int,
        cooldown_until: float,
        calculated_delay: float,
    ) -> None:
        """317: Async broadcast of the 429 event via the Kafka channel (Fail-Open)."""
        try:
            from baldur.services.rate_limit.distributed_channel import (
                get_distributed_rate_limit_channel,
            )

            channel = get_distributed_rate_limit_channel()
            channel.broadcast_rate_limit_429(
                key=key,
                consecutive_429s=consecutive_429s,
                cooldown_until=cooldown_until,
                calculated_delay=calculated_delay,
            )
        except Exception as e:
            logger.debug(
                "rate_limit_coordinator.broadcast_skipped",
                error=e,
            )

    def on_success(self, key: str) -> None:
        """
        Handle a successful response.

        Call this after a successful request to clear canary state
        and reset consecutive 429 counter.

        Args:
            key: Rate limit key
        """
        # Clear canary state
        self._clear_canary_state(key)

        state = self._storage.get_state(key)

        if state.consecutive_429s > 0:
            # Gradual reduction instead of immediate reset
            # Prevents immediate flood after recovery
            self._storage.reset_consecutive_429s(key)

            logger.debug(
                "rate_limit_coordinator.success_reset_consecutive_counter",
                key=key,
            )

    def clear(self, key: str) -> None:
        """Clear all rate limit state for a key.

        A key this process holds a cooldown record for is re-verified at once, so
        the operator escape produces its all-clear instead of leaving the
        consumer waiting for the expiry that was just cleared. A key this process
        never observed announces nothing — releasing there would be an all-clear
        for a cooldown that never cooled here.
        """
        self._storage.clear(key)
        self._announcer.reverify(key)
        logger.info(
            "rate_limit_coordinator.cleared_state",
            key=key,
        )

    def rate_limit_aware(
        self,
        key: str,
        is_429: Callable[[Any], bool] | None = None,
        get_retry_after: Callable[[Any], float | None] | None = None,
        max_wait: float | None = None,
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """
        Decorator to make a function rate-limit aware.

        Args:
            key: Rate limit key
            is_429: Function to detect if response is 429 (default: check status_code)
            get_retry_after: Function to extract Retry-After from response. The
                default reads the response's ``Retry-After`` header in both
                RFC 9110 forms (delta-seconds and HTTP-date) and returns ``None``
                for anything unusable. A replacement owns that coercion itself —
                a bare ``float(header)`` raises on the HTTP-date form, and the
                extractor's exceptions propagate to your caller.
            max_wait: Maximum seconds the wrapper may sleep on an active cooldown.
                ``None`` uses the configured ``max_delay``. When the remaining
                cooldown exceeds the bound the wrapper raises
                ``RateLimitDeferredError`` instead of calling the function — a
                decorator cannot return "nothing", so raising is its only correct
                refusal.

        Returns:
            Decorated function

        Raises:
            RateLimitDeferredError: Cooldown outlasts ``max_wait``; the wrapped
                function was not called and is safe to retry at ``not_before``.

        Example:
            @coordinator.rate_limit_aware("payment_api")
            def call_payment_api():
                return requests.post(...)

            @coordinator.rate_limit_aware(
                "external_api",
                is_429=lambda r: r.status_code == 429,
            )
            def call_external_api():
                return requests.get(...)
        """

        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            def wrapper(*args: Any, **kwargs: Any) -> T:
                # Wait if in cooldown. Fail-open on a coordinator fault (proceed
                # without waiting) — but a *deferral* is a deliberate refusal, so
                # it is decided from the returned result, outside the wrap.
                try:
                    wait_result: RateLimitResult | None = self.wait_if_needed(
                        key, max_wait=max_wait
                    )
                except Exception as coordinator_error:
                    logger.warning(
                        "rate_limit_coordinator.decorator_wait_failed",
                        key=key,
                        error=str(coordinator_error),
                    )
                    wait_result = None

                if wait_result is not None and wait_result.deferred:
                    raise RateLimitDeferredError(
                        key=key,
                        not_before=wait_result.not_before,
                    )

                result = func(*args, **kwargs)

                # Check if rate limited. Both notifications are fail-open: the
                # wrapped call has already committed its side effect, so a
                # coordinator fault must not surface as if the call never ran.
                _is_429 = is_429 or _default_is_429
                _get_retry_after = get_retry_after or _default_get_retry_after

                # The user-supplied predicates stay OUTSIDE the wrap: their
                # exceptions are the caller's own and must keep propagating.
                rate_limited = _is_429(result)
                retry_after = _get_retry_after(result) if rate_limited else None

                try:
                    if rate_limited:
                        self.on_rate_limited(key, retry_after)
                    else:
                        self.on_success(key)
                except Exception as coordinator_error:
                    logger.warning(
                        "rate_limit_coordinator.decorator_notify_failed",
                        key=key,
                        error=str(coordinator_error),
                    )

                return result

            return wrapper

        return decorator


# Convenience function
def get_rate_limit_coordinator() -> RateLimitCoordinator:
    """Get the global rate limit coordinator instance."""
    return RateLimitCoordinator.get_instance()
