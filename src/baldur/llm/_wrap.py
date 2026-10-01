"""The wrapped LLM client: every SDK call runs under Baldur, endpoint by endpoint.

``wrap(client, fallbacks=[...])`` returns an object that answers like the SDK
client it wraps. Attribute access walks the SDK's resource tree on the primary
client; a method reached through at least one resource
(``client.chat.completions.create``, ``client.messages.create``,
``client.models.generate_content``) is replaced by a call that tries each
endpoint in order, each under its own ``protect()`` — its own shared wait,
retry ladder and breaker, named after the endpoint's host and model.

The move rule after an endpoint fails:

- the provider rejected the request itself → the error is re-raised and nothing
  else is called;
- any other provider answer (a limit, an overload, an exhausted quota, a bad
  key, a failure), a transport error, an open breaker, a wait longer than the
  call may sleep → the next endpoint is tried;
- anything else (an SDK argument check, a bug in the caller) → re-raised.

When no endpoint answered, the call raises ``LLMUnavailableError`` chained to
the last endpoint's error.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import re
from collections.abc import Callable, Sequence
from typing import Any, TypeVar, overload
from urllib.parse import urlparse

import structlog

from baldur.core.exceptions import (
    CircuitBreakerError,
    LLMUnavailableError,
    RateLimitDeferredError,
    TimeoutPolicyError,
)
from baldur.services.retry_handler.provider_errors import (
    ProviderErrorCategory,
    classify_provider_error,
)

logger = structlog.get_logger()

__all__ = ["Endpoint", "wrap"]

ClientT = TypeVar("ClientT")

_UNSET: Any = object()

# Prefix of every derived endpoint name, and the length a derived name is held
# to: the domain-name cap the breaker, the shared wait and the metric labels
# all key on.
_IDENTITY_PREFIX = "llm"
_IDENTITY_MAX_LENGTH = 64
_IDENTITY_HASH_LENGTH = 8
_IDENTITY_SEGMENT_UNSAFE = re.compile(r"[^a-z0-9]")

# A Google Gen AI client exposes no base URL; its public ``vertexai`` flag names
# the service its calls go to.
_GOOGLE_GENAI_FAMILY = "google.genai"
_VERTEX_AI_HOST = "aiplatform.googleapis.com"
_GEMINI_API_HOST = "generativelanguage.googleapis.com"

# SDK module roots whose client classes the wrap recognizes by name; any other
# client is grouped by its own module root.
_SDK_FAMILY_ROOTS = frozenset({"openai", "anthropic"})

# Transport-layer error modules: a failure below the SDK (google-genai raises
# its connection errors raw) is a move like any connection error.
_TRANSPORT_MODULE_ROOTS = frozenset(
    {"httpx", "httpx2", "requests", "urllib3", "aiohttp"}
)

# Categories of a move the endpoint did not itself cause: the breaker state and
# the shared wait already record why, so the move is not logged again.
_CATEGORY_BREAKER_OPEN = "breaker_open"
_CATEGORY_RATE_LIMIT_DEFERRED = "rate_limit_deferred"
_CATEGORY_DEADLINE = "deadline"
_UNLOGGED_MOVE_CATEGORIES = frozenset(
    {_CATEGORY_BREAKER_OPEN, _CATEGORY_RATE_LIMIT_DEFERRED}
)

# A call carrying this keyword addresses a model, so it may move to another
# endpoint; any other call (files, batches, model listing) is account-scoped
# and runs on the primary alone.
_MODEL_KEYWORD = "model"
_TIMEOUT_KEYWORD = "timeout"

# Bound on the per-function "does it take timeout=" cache.
_SIGNATURE_CACHE_SIZE = 512


class Endpoint:
    """One place a wrapped call can be sent.

    Args:
        client: An SDK client — the same SDK as the wrap's other endpoints.
        model: The model this endpoint serves. A call sent here has its
            ``model=`` replaced with it, so a fallback can name the model its
            own provider calls the same job by.
        name: The name this endpoint's breaker, shared wait and metrics are
            kept under. Unset, it is derived from the client's host and the
            model: ``llm.<host>.<model>``.
    """

    __slots__ = ("client", "model", "name")

    def __init__(
        self,
        client: Any,
        *,
        model: str | None = None,
        name: str | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.name = name

    def __repr__(self) -> str:
        return (
            f"Endpoint({type(self.client).__name__}, model={self.model!r}, "
            f"name={self.name!r})"
        )


@overload
def wrap(
    client: Endpoint,
    *,
    fallbacks: Sequence[Any] = (),
    name: str | None = None,
    timeout: Any = _UNSET,
) -> Any: ...


@overload
def wrap(
    client: ClientT,
    *,
    fallbacks: Sequence[Any] = (),
    name: str | None = None,
    timeout: Any = _UNSET,
) -> ClientT: ...


def wrap(
    client: Any,
    *,
    fallbacks: Sequence[Any] = (),
    name: str | None = None,
    timeout: Any = _UNSET,
) -> Any:
    """Wrap an LLM SDK client so every call it makes runs under Baldur.

    The call sites do not change: ``wrapped.chat.completions.create(...)`` is
    the SDK call, sent to the first endpoint that answers. Each endpoint gets
    its own shared wait (a provider's rate limit or overload is waited out by
    every worker at once, for at least as long as the provider asked), its own
    retry ladder and its own circuit breaker. A call that waiting cannot save
    moves to the next endpoint; a request the provider rejected as invalid is
    neither retried nor moved; when no endpoint answers the call raises
    ``LLMUnavailableError``.

    Supported SDKs: the OpenAI Python SDK (which also reaches Azure OpenAI and
    every OpenAI-compatible server), the Anthropic SDK and the Google Gen AI
    SDK (``google-genai``). Each OpenAI or Anthropic client is copied with the
    SDK's own retries switched off (``with_options(max_retries=0)``), so
    Baldur's coordinated retry is the only retry loop; the client you pass in
    is not changed. Plain attributes and client-level methods
    (``wrapped.close()``) are the primary client's own.

    Only a call with a ``model=`` keyword moves between endpoints; other calls
    run on the primary alone, still waited, retried and broken.

    The wrapped object is not an instance of the SDK client's class: a library
    that type-checks its client argument needs the raw client.

    Args:
        client: The primary SDK client, or an :class:`Endpoint` around one.
        fallbacks: Further endpoints, tried in order — clients of the same SDK,
            each either bare or in an :class:`Endpoint` (to pin the model it
            serves, or name it).
        name: The primary endpoint's name — see :class:`Endpoint`.
        timeout: A request timeout applied to every OpenAI or Anthropic
            endpoint, as the SDK's own ``timeout`` option. Unset, the SDK's
            default stands.

    Returns:
        An object that answers like ``client``.

    Raises:
        ValueError: An endpoint uses a different SDK, or a sync client is mixed
            with an async one, or two endpoints would always share one name —
            sharing, the fallback would wait out the primary's wait and stand
            behind its breaker, and never answer where the primary did not.
            Give one of them ``Endpoint(name=...)`` or a different ``model``.
    """
    primary = client if isinstance(client, Endpoint) else Endpoint(client)
    if name is not None:
        if primary.name is not None:
            raise ValueError(
                "wrap(name=...) names the primary endpoint, which already has "
                f"name={primary.name!r}; pass one of the two."
            )
        primary = Endpoint(primary.client, model=primary.model, name=name)
    endpoints = [primary] + [
        item if isinstance(item, Endpoint) else Endpoint(item) for item in fallbacks
    ]
    return _WrappedClient(
        [_PreparedEndpoint(endpoint, timeout) for endpoint in endpoints],
        timeout,
    )


def _sdk_family(obj: Any) -> str:
    """The SDK an object belongs to: ``openai``, ``anthropic``, ``google.genai`` or its module root."""
    for cls in type(obj).__mro__:
        module = getattr(cls, "__module__", None)
        if not isinstance(module, str):
            continue
        if module == _GOOGLE_GENAI_FAMILY or module.startswith(
            _GOOGLE_GENAI_FAMILY + "."
        ):
            return _GOOGLE_GENAI_FAMILY
        root = module.split(".", 1)[0]
        if root in _SDK_FAMILY_ROOTS:
            return root
    return str(type(obj).__module__).split(".", 1)[0]


def _client_mode(client: Any) -> str | None:
    """``"sync"`` / ``"async"`` for a client whose class says so, else ``None``."""
    for cls in type(client).__mro__:
        if cls.__name__ == "AsyncAPIClient":
            return "async"
        if cls.__name__ == "SyncAPIClient":
            return "sync"
    return None


def _host_of(client: Any, family: str) -> str:
    """The host an endpoint's calls go to, as far as the client says."""
    base_url = getattr(client, "base_url", None)
    if base_url is not None:
        try:
            host = urlparse(str(base_url)).hostname
        except Exception:
            host = None
        if host:
            return host
    if family == _GOOGLE_GENAI_FAMILY:
        vertexai = getattr(client, "vertexai", None)
        if isinstance(vertexai, bool):
            return _VERTEX_AI_HOST if vertexai else _GEMINI_API_HOST
    return family


def _identity_segment(text: str) -> str:
    return _IDENTITY_SEGMENT_UNSAFE.sub("_", text.lower())


def _derived_identity(host: str, model: str | None) -> str:
    """``llm.<host>.<model>``, lowercased, every other character ``_``, at most 64."""
    segments = [_IDENTITY_PREFIX, _identity_segment(host)]
    if model:
        segments.append(_identity_segment(model))
    identity = ".".join(segments)
    if len(identity) <= _IDENTITY_MAX_LENGTH:
        return identity
    digest = hashlib.sha1(identity.encode("utf-8"), usedforsecurity=False)
    keep = _IDENTITY_MAX_LENGTH - _IDENTITY_HASH_LENGTH - 1
    return f"{identity[:keep]}_{digest.hexdigest()[:_IDENTITY_HASH_LENGTH]}"


def _prepare_client(client: Any, timeout: Any) -> Any:
    """A copy of ``client`` with the SDK's own retries off, or the client itself."""
    with_options = getattr(client, "with_options", None)
    if not callable(with_options):
        return client
    options: dict[str, Any] = {"max_retries": 0}
    if timeout is not _UNSET:
        options["timeout"] = timeout
    return with_options(**options)


class _PreparedEndpoint:
    """An endpoint as the wrap calls it: prepared client, family, host."""

    __slots__ = ("client", "family", "host", "model", "name")

    def __init__(self, endpoint: Endpoint, timeout: Any) -> None:
        self.family = _sdk_family(endpoint.client)
        self.client = _prepare_client(endpoint.client, timeout)
        self.host = _host_of(self.client, self.family)
        self.model = endpoint.model
        self.name = endpoint.name

    def identity(self, call_model: Any) -> str:
        if self.name is not None:
            return self.name
        model = self.model if self.model is not None else call_model
        return _derived_identity(self.host, model if isinstance(model, str) else None)

    def always_shares_identity_with(self, other: _PreparedEndpoint) -> bool:
        if self.name is not None or other.name is not None:
            return self.name is not None and self.name == other.name
        return self.host == other.host and self.model == other.model


def _validate_endpoints(endpoints: list[_PreparedEndpoint]) -> None:
    primary = endpoints[0]
    primary_mode = _client_mode(primary.client)
    for index, endpoint in enumerate(endpoints[1:], start=1):
        if endpoint.family != primary.family:
            raise ValueError(
                f"fallback {index} is a {endpoint.family} client but the primary "
                f"is a {primary.family} client; every endpoint of one wrap must "
                "use the same SDK, because the call is the SDK's call."
            )
        mode = _client_mode(endpoint.client)
        if primary_mode and mode and mode != primary_mode:
            raise ValueError(
                f"fallback {index} is an {mode} client but the primary is a "
                f"{primary_mode} client; wrap sync and async clients separately."
            )
    for i, first in enumerate(endpoints):
        for j in range(i + 1, len(endpoints)):
            if first.always_shares_identity_with(endpoints[j]):
                raise ValueError(
                    f"endpoints {i} and {j} would share the name "
                    f"{first.identity(None)!r} on every call, so the second would "
                    "wait out the first's wait and stand behind its breaker; give "
                    "one of them Endpoint(name=...) or a different model."
                )


class _WrappedClient:
    """The object ``wrap`` returns: the primary client, with its calls protected."""

    __slots__ = ("_endpoints", "_timeout")

    def __init__(self, endpoints: list[_PreparedEndpoint], timeout: Any) -> None:
        _validate_endpoints(endpoints)
        object.__setattr__(self, "_endpoints", tuple(endpoints))
        object.__setattr__(self, "_timeout", timeout)

    @property
    def _primary(self) -> Any:
        return self._endpoints[0].client

    def __getattr__(self, attr: str) -> Any:
        target = getattr(self._primary, attr)
        if attr.startswith("_") or callable(target):
            return target
        if _sdk_family(target) != self._endpoints[0].family:
            return target
        return _Resource(self, (attr,))

    def __dir__(self) -> list[str]:
        return dir(self._primary)

    def __repr__(self) -> str:
        names = ", ".join(repr(endpoint.client) for endpoint in self._endpoints)
        return f"<baldur.llm.wrap of {names}>"


class _Resource:
    """A resource reached from the wrapped client; its methods are protected."""

    __slots__ = ("_path", "_wrapped")

    def __init__(self, wrapped: _WrappedClient, path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, attr: str) -> Any:
        wrapped = self._wrapped
        path = (*self._path, attr)
        target = _resolve(wrapped._primary, path)
        if attr.startswith("_"):
            return target
        if callable(target) and not isinstance(target, type):
            return _protected_method(wrapped, path, target)
        if _sdk_family(target) == wrapped._endpoints[0].family:
            return _Resource(wrapped, path)
        return target

    def __dir__(self) -> list[str]:
        return dir(_resolve(self._wrapped._primary, self._path))

    def __repr__(self) -> str:
        return f"<baldur.llm wrap of {'.'.join(self._path)}>"


def _resolve(root: Any, path: tuple[str, ...]) -> Any:
    target = root
    for attr in path:
        target = getattr(target, attr)
    return target


def _is_coroutine_method(method: Any) -> bool:
    # SDK methods are often wrapped by a sync-looking decorator around an
    # ``async def``; the coroutine-ness is on the function underneath.
    return inspect.iscoroutinefunction(method) or inspect.iscoroutinefunction(
        inspect.unwrap(method)
    )


def _protected_method(
    wrapped: _WrappedClient, path: tuple[str, ...], method: Any
) -> Callable[..., Any]:
    if _is_coroutine_method(method):

        @functools.wraps(method)
        async def acall(*args: Any, **kwargs: Any) -> Any:
            return await _acall(wrapped, path, args, kwargs)

        return acall

    @functools.wraps(method)
    def call(*args: Any, **kwargs: Any) -> Any:
        return _call(wrapped, path, args, kwargs)

    return call


@functools.lru_cache(maxsize=_SIGNATURE_CACHE_SIZE)
def _function_accepts_timeout(function: Any) -> bool:
    try:
        return _TIMEOUT_KEYWORD in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def _accepts_timeout(method: Any) -> bool:
    function = getattr(method, "__func__", method)
    try:
        return _function_accepts_timeout(function)
    except TypeError:
        # An unhashable callable: ask without the cache.
        return _function_accepts_timeout.__wrapped__(function)


def _remaining_seconds() -> float | None:
    """Time left on the request-scoped deadline, or ``None`` outside one."""
    try:
        from baldur.scaling.deadline_context import get_remaining_ms

        remaining_ms = get_remaining_ms()
    except Exception:
        return None
    return None if remaining_ms is None else remaining_ms / 1000.0


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _deadline_timeout(
    caller_timeout: Any, remaining: float, wrap_timeout: Any
) -> float:
    """The tightest of the time left, the caller's timeout and the wrap's."""
    bound = remaining
    for timeout in (caller_timeout, wrap_timeout):
        if _is_positive_number(timeout):
            bound = min(bound, float(timeout))
    return bound


def _is_transport_error(error: BaseException) -> bool:
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    for cls in type(error).__mro__:
        module = getattr(cls, "__module__", None)
        if (
            isinstance(module, str)
            and module.split(".", 1)[0] in _TRANSPORT_MODULE_ROOTS
        ):
            return True
    return False


def _move_category(error: BaseException) -> str | None:
    """Why the call may move past this endpoint, or ``None`` when it may not."""
    verdict = classify_provider_error(error)
    if verdict is not None:
        if verdict.category is ProviderErrorCategory.INVALID_REQUEST:
            return None
        return verdict.category.value
    if isinstance(error, CircuitBreakerError):
        return _CATEGORY_BREAKER_OPEN
    if isinstance(error, RateLimitDeferredError):
        return _CATEGORY_RATE_LIMIT_DEFERRED
    if isinstance(error, TimeoutPolicyError) or _is_transport_error(error):
        return ProviderErrorCategory.TRANSIENT.value
    return None


class _CallPlan:
    """One wrapped call's walk over its endpoints."""

    __slots__ = (
        "args",
        "attempts",
        "endpoints",
        "kwargs",
        "last_error",
        "path",
        "wrapped",
    )

    def __init__(
        self,
        wrapped: _WrappedClient,
        path: tuple[str, ...],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self.wrapped = wrapped
        self.path = path
        self.args = args
        self.kwargs = kwargs
        endpoints = wrapped._endpoints
        self.endpoints = endpoints if _MODEL_KEYWORD in kwargs else endpoints[:1]
        self.attempts: list[tuple[str, str]] = []
        self.last_error: BaseException | None = None

    def identity(self, index: int) -> str:
        return self.endpoints[index].identity(self.kwargs.get(_MODEL_KEYWORD))

    def prepare(self, index: int) -> tuple[Any, dict[str, Any]] | None:
        """The method and keywords for endpoint ``index``, or None past the deadline."""
        endpoint = self.endpoints[index]
        remaining = _remaining_seconds()
        if remaining is not None and remaining <= 0:
            self.attempts.append((self.identity(index), _CATEGORY_DEADLINE))
            return None
        method = _resolve(endpoint.client, self.path)
        kwargs = dict(self.kwargs)
        if endpoint.model is not None and _MODEL_KEYWORD in kwargs:
            kwargs[_MODEL_KEYWORD] = endpoint.model
        if remaining is not None and _accepts_timeout(method):
            kwargs[_TIMEOUT_KEYWORD] = _deadline_timeout(
                kwargs.get(_TIMEOUT_KEYWORD), remaining, self.wrapped._timeout
            )
        return method, kwargs

    def record_failure(self, index: int, error: Exception) -> bool:
        """Note why endpoint ``index`` failed; False when the error must propagate."""
        category = _move_category(error)
        if category is None:
            return False
        identity = self.identity(index)
        self.attempts.append((identity, category))
        self.last_error = error
        if (
            index + 1 < len(self.endpoints)
            and category not in _UNLOGGED_MOVE_CATEGORIES
        ):
            verdict = classify_provider_error(error)
            logger.warning(
                "llm.endpoint_call_failed",
                endpoint=identity,
                next_endpoint=self.identity(index + 1),
                category=category,
                status=verdict.status if verdict is not None else None,
                error_type=type(error).__name__,
            )
        return True

    def record_answer(self, index: int, mode: str) -> None:
        if index == 0:
            return
        try:
            from baldur.metrics.recorders.protect import get_protect_recorder

            recorder = get_protect_recorder()
            if recorder is not None:
                recorder.record_fallback(self.identity(0), mode=mode)
        except Exception as error:
            logger.debug("llm.fallback_record_failed", error=str(error))

    def unavailable(self) -> LLMUnavailableError:
        tried = ", ".join(
            f"{identity} ({category})" for identity, category in self.attempts
        )
        return LLMUnavailableError(
            f"No LLM endpoint answered: {tried or 'none tried'}",
            attempts=tuple(self.attempts),
        )


def _call(
    wrapped: _WrappedClient,
    path: tuple[str, ...],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    from baldur.protect_facade import protect

    plan = _CallPlan(wrapped, path, args, kwargs)
    for index in range(len(plan.endpoints)):
        prepared = plan.prepare(index)
        if prepared is None:
            break
        method, call_kwargs = prepared
        try:
            result = protect(
                plan.identity(index),
                functools.partial(method, *args, **call_kwargs),
                retry=True,
                circuit_breaker=True,
                dlq=False,
                fallback=None,
                timeout=None,
            )
        except Exception as error:
            if not plan.record_failure(index, error):
                raise
            continue
        plan.record_answer(index, "sync")
        return result
    raise plan.unavailable() from plan.last_error


async def _acall(
    wrapped: _WrappedClient,
    path: tuple[str, ...],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    from baldur.protect_facade import aprotect

    plan = _CallPlan(wrapped, path, args, kwargs)
    for index in range(len(plan.endpoints)):
        prepared = plan.prepare(index)
        if prepared is None:
            break
        method, call_kwargs = prepared

        # A real coroutine function, so every async stage recognizes it as one:
        # an SDK's own method often hides its ``async def`` behind a decorator.
        async def attempt(
            method: Any = method, call_kwargs: dict[str, Any] = call_kwargs
        ) -> Any:
            return await method(*args, **call_kwargs)

        try:
            result = await aprotect(
                plan.identity(index),
                attempt,
                retry=True,
                circuit_breaker=True,
                dlq=False,
                fallback=None,
                timeout=None,
            )
        except Exception as error:
            if not plan.record_failure(index, error):
                raise
            continue
        plan.record_answer(index, "async")
        return result
    raise plan.unavailable() from plan.last_error
