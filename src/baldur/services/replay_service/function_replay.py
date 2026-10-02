"""
Function replay — re-run a parked job from the arguments stored with it.

``@protected(name, replay=True)`` (and ``@aprotected``) registers a
:class:`FunctionReplayHandler` for ``name``. When the job's breaker closes after
an outage, the recovery sweep replays the job's parked failures through it: the
decorated function, undecorated, called again with its stored arguments under
the same protection the decorator gives it — minus DLQ capture and fallback, so
a replay that fails again adds no second entry and a fallback can never mark a
job done that did not run.

What makes a job replayable is decided when it is decorated, not when it fails:
every parameter must be one Baldur stores exactly (``str``, ``int``, ``float``,
``bool`` or ``None``, or an ``Optional`` of one; an unannotated parameter is
checked when it replays), bound by name, and not redacted by DLQ masking.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import threading
import types
import typing
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.masking import mask_sensitive_fields
from baldur.core.exceptions import LLMUnavailableError
from baldur.services.replay_service.handlers import (
    ReplayHandler,
    get_replay_handler,
    has_replay_handler,
    register_replay_handler,
)
from baldur.services.replay_service.models import ReplayResult
from baldur.services.retry_handler.sinks import retry_exhausted_failure_type
from baldur.utils.domain_validation import FALLBACK_DOMAIN, resolve_stored_domain

if TYPE_CHECKING:
    from baldur.interfaces.repositories import FailedOperationData

logger = structlog.get_logger()

__all__ = ["FunctionReplayHandler", "arm_function_replay"]

# Parameter types whose values survive the DLQ's JSON round trip unchanged.
_EXACT_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))

# Parameter kinds a replay can rebuild: it calls the function with keywords.
_REBUILDABLE_KINDS = frozenset(
    {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
)

# Probe value for the masking check: masking replaces a sensitive field's value
# whatever it is, so any value distinguishes a masked name from a kept one.
_MASKING_PROBE = "value"

# Keywords of the decorator a replay runs with, besides the ones it fixes.
REPLAY_PROTECT_OPTIONS: tuple[str, ...] = (
    "retry",
    "circuit_breaker",
    "timeout",
    "idempotency_key",
    "idempotency_fail_open",
    "idempotency_ttl",
    "idempotency_execution_ttl",
)

# Thread name for an async replay run off a thread that already runs a loop.
_ASYNC_REPLAY_THREAD_NAME = "baldur-async-replay"

# Bound on the length of an error message carried into a replay result.
_ERROR_PREVIEW_CHARS = 200

# Decision an idempotency guard reports for a key whose record is completed
# (``IdempotencyDuplicateError.decision``, the gate decision's member name).
_SKIP = "SKIP"


class FunctionReplayHandler(ReplayHandler):
    """Replays a ``replay=True`` job by calling its function with the stored arguments.

    Registered by the decorator, one per protected name; not built by hand.
    Declares the job's no-endpoint failures (``LLMUnavailableError``) as replayed
    automatically when the job's breaker closes. Every other failure parked
    under the name — a rejected request, an error in the job body, a timeout
    whose work may still be running — is replayed only from the console.
    """

    def __init__(
        self,
        *,
        name: str,
        func: Callable[..., Any],
        signature: inspect.Signature,
        annotated_primitive: dict[str, bool],
        protect_options: dict[str, Any],
    ) -> None:
        self._name = name
        self._domain = resolve_stored_domain(name)
        self._func = func
        self._signature = signature
        self._annotated_primitive = annotated_primitive
        self._protect_options = dict(protect_options)
        self._is_async = asyncio.iscoroutinefunction(func)

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def auto_replay_failure_types(self) -> tuple[str, ...]:
        return (retry_exhausted_failure_type(LLMUnavailableError.__name__),)

    @property
    def function_identity(self) -> tuple[str, str]:
        """``(module, qualified name)`` — stable across a module reload."""
        return (
            str(getattr(self._func, "__module__", "")),
            str(getattr(self._func, "__qualname__", "")),
        )

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        arguments, reason = self._stored_arguments(failed_op)
        return arguments is not None, reason

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        """Re-run the job, reporting whether its body began.

        The result's ``data`` carries ``job_started`` (the job function's body
        began) and ``rejected_by_breaker`` (the job's own breaker refused the
        call before the body began), read off the job itself rather than
        inferred from what it raised. A replay whose body never began did not
        call the dependency, so the replay service gives its attempt back.

        A job whose own idempotency key already completed (an earlier run
        succeeded, and its worker died before the entry was completed) reports
        success without running again.
        """
        dlq_id = str(failed_op.id)
        arguments, reason = self._stored_arguments(failed_op)
        if arguments is None:
            return ReplayResult.failed(dlq_id, reason)
        start = _JobStart()
        try:
            self._run(arguments, start)
        except Exception as error:
            if not start.began and _is_completed_key(error):
                return ReplayResult.succeeded(
                    dlq_id,
                    message=f"{self._name} already done under its idempotency key",
                    data=_start_flags(began=False, refused=False),
                )
            return ReplayResult(
                success=False,
                dlq_id=dlq_id,
                error=f"{type(error).__name__}: {str(error)[:_ERROR_PREVIEW_CHARS]}",
                data=_start_flags(
                    began=start.began,
                    refused=not start.began and _is_open_circuit_refusal(error),
                ),
            )
        return ReplayResult.succeeded(
            dlq_id,
            message=f"re-ran {self._name}",
            data=_start_flags(began=True, refused=False),
        )

    def _stored_arguments(
        self, failed_op: FailedOperationData
    ) -> tuple[dict[str, Any] | None, str]:
        """The keyword arguments to re-run with, or ``(None, why not)``."""
        stored = failed_op.request_data
        if not isinstance(stored, dict):
            return None, "no stored arguments"
        arguments: dict[str, Any] = {}
        for param_name, param in self._signature.parameters.items():
            if param_name not in stored:
                if param.default is inspect.Parameter.empty:
                    return None, (
                        f"stored arguments lack required parameter {param_name!r}"
                    )
                continue
            value = stored[param_name]
            if param.annotation is inspect.Parameter.empty and not isinstance(
                value, _EXACT_TYPES
            ):
                return None, (
                    f"stored argument {param_name!r} is a {type(value).__name__}, "
                    "which a replay cannot pass back exactly"
                )
            arguments[param_name] = value
        return arguments, ""

    def _run(self, arguments: dict[str, Any], start: _JobStart) -> Any:
        from baldur.protect_facade import (
            _build_context_from_callsite,
            aprotect,
            protect,
        )

        context = _build_context_from_callsite(
            self._signature, (), arguments, None, self._annotated_primitive
        )
        options: dict[str, Any] = {
            **self._protect_options,
            "dlq": False,
            "fallback": None,
            "context": context,
        }
        func = self._func
        if not self._is_async:

            def call() -> Any:
                start.began = True
                return func(**arguments)

            return protect(self._name, call, **options)

        async def call_async() -> Any:
            start.began = True
            return await func(**arguments)

        return _run_coroutine(lambda: aprotect(self._name, call_async, **options))


class _JobStart:
    """Notes that the job function's body began (set by the call protect runs)."""

    __slots__ = ("began",)

    def __init__(self) -> None:
        self.began = False


def _start_flags(*, began: bool, refused: bool) -> dict[str, bool]:
    """The ``data`` a replay result carries about how far the job got."""
    return {"job_started": began, "rejected_by_breaker": refused}


def _is_open_circuit_refusal(error: BaseException) -> bool:
    """The job's breaker refused the call (only meaningful before the body began)."""
    from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError

    return isinstance(error, CircuitBreakerOpenError)


def _is_completed_key(error: BaseException) -> bool:
    """The job's own idempotency key reads completed: an earlier run succeeded."""
    from baldur.core.exceptions import IdempotencyDuplicateError

    return isinstance(error, IdempotencyDuplicateError) and error.decision == _SKIP


def _run_coroutine(make_coroutine: Callable[[], Any]) -> Any:
    """Run a coroutine to completion from sync code, carrying the caller's context.

    ``asyncio.run`` copies the calling context into its task, so a deadline the
    caller set is seen by every await inside. A thread already running a loop
    cannot start another: the coroutine then runs on a fresh thread under a
    copy of this thread's context.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coroutine())

    context = contextvars.copy_context()
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = context.run(lambda: asyncio.run(make_coroutine()))
        except BaseException as error:  # handed back to the waiting caller
            outcome["error"] = error

    thread = threading.Thread(target=run, name=_ASYNC_REPLAY_THREAD_NAME, daemon=True)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _resolve_annotation(func: Callable[..., Any], annotation: Any) -> Any:
    """A parameter's annotation as an object, evaluating a postponed (string) one.

    Evaluated one parameter at a time, so an unresolvable annotation elsewhere
    (a return type defined later in the module) does not fail the check. An
    annotation that cannot be resolved comes back as its string, which no
    replayable type matches.
    """
    if not isinstance(annotation, str):
        return annotation

    def probe() -> None:
        return None

    probe.__annotations__ = {"value": annotation}
    try:
        hints = typing.get_type_hints(
            probe, globalns=getattr(func, "__globals__", None)
        )
    except Exception:
        return annotation
    return hints.get("value", annotation)


def _is_exact_annotation(annotation: Any) -> bool:
    if annotation is inspect.Parameter.empty:
        return True
    if annotation is None or annotation in _EXACT_TYPES:
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        members = typing.get_args(annotation)
        kept = [member for member in members if member is not type(None)]
        return len(members) == 2 and len(kept) == 1 and kept[0] in _EXACT_TYPES
    return False


def _is_masked_name(param_name: str) -> bool:
    masked = mask_sensitive_fields({param_name: _MASKING_PROBE})
    return masked.get(param_name) != _MASKING_PROBE


def _check_signature(
    name: str, func: Callable[..., Any], sig: inspect.Signature
) -> None:
    for param_name, param in sig.parameters.items():
        if param.kind not in _REBUILDABLE_KINDS:
            raise ValueError(
                f"replay=True on {name!r}: parameter {param_name!r} is "
                f"{param.kind.description}; a replay re-runs the function with "
                "keyword arguments, so every parameter must accept one."
            )
        annotation = _resolve_annotation(func, param.annotation)
        if not _is_exact_annotation(annotation):
            raise ValueError(
                f"replay=True on {name!r}: parameter {param_name!r} is annotated "
                f"{annotation!r}; a replayed job gets back only str, int, float, "
                "bool or None (or an Optional of one) exactly as it was called."
            )
        if _is_masked_name(param_name):
            raise ValueError(
                f"replay=True on {name!r}: parameter {param_name!r} is redacted "
                "when a failure is stored, so a replay would pass the redaction "
                "marker instead of the value; rename the parameter."
            )


def arm_function_replay(
    name: str,
    func: Callable[..., Any],
    *,
    signature: inspect.Signature,
    annotated_primitive: dict[str, bool],
    protect_options: dict[str, Any],
) -> FunctionReplayHandler:
    """Check that ``func`` can be replayed and register its handler under ``name``.

    Raises ``ValueError`` — at decoration, so an unreplayable job fails when its
    module is imported rather than when it fails — when the name has no domain
    identity of its own, a parameter cannot be stored exactly, or another
    function or a hand-written handler already replays the name. A handler for
    the same function (the same module and qualified name — a module reload) is
    replaced.
    """
    domain = resolve_stored_domain(name)
    if domain == FALLBACK_DOMAIN:
        raise ValueError(
            f"replay=True on {name!r}: the name cannot be stored as a DLQ domain "
            "of its own, so its parked jobs would share a bucket with every other "
            "such name; use lowercase letters, digits, '_' and '.'."
        )
    _check_signature(name, func, signature)
    handler = FunctionReplayHandler(
        name=name,
        func=func,
        signature=signature,
        annotated_primitive=annotated_primitive,
        protect_options=protect_options,
    )
    if has_replay_handler(domain):
        existing = get_replay_handler(domain)
        if not (
            isinstance(existing, FunctionReplayHandler)
            and existing.function_identity == handler.function_identity
        ):
            raise ValueError(
                f"replay=True on {name!r}: the name is already replayed by "
                f"{_describe(existing)}; a name replays one function."
            )
    register_replay_handler(handler)
    logger.debug(
        "replay_service.function_replay_registered",
        healing_domain=domain,
        function=".".join(handler.function_identity),
    )
    return handler


def _describe(handler: ReplayHandler) -> str:
    if isinstance(handler, FunctionReplayHandler):
        return ".".join(handler.function_identity)
    return f"the handler {type(handler).__name__}"
