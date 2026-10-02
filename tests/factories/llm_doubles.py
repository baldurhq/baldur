"""Test doubles for the LLM SDKs that ``baldur.llm`` and the provider classifier read.

Three kinds, for three needs:

- **SDK-free exceptions** whose ``__module__`` names an SDK. The classifier
  recognizes a provider by the module an exception's class is defined in, so
  these run its rules with no SDK installed. They carry the attributes the real
  SDK exceptions carry (``status_code`` / ``response.headers`` / ``code`` /
  ``type`` for OpenAI and Anthropic; ``code`` / ``status`` / ``details`` for the
  Google Gen AI SDK).
- **Real SDK exceptions**, built from the installed SDK's own classes. Each
  builder imports its SDK when called, so a caller guards it with
  ``pytest.importorskip``.
- **A fake SDK client tree** (``client.chat.completions.create``,
  ``client.models.list``) for the wrap. Its methods answer from a script and
  record every call; ``with_options`` returns a copy, as the real clients do,
  so the copy the wrap prepares and the client the caller holds can be told
  apart.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

__all__ = [
    "HANG",
    "FakeAnthropicError",
    "FakeApiCoreError",
    "FakeAsyncLLMClient",
    "FakeGenaiError",
    "FakeLLMClient",
    "FakeOpenAIError",
    "FakeOtherSdkClient",
    "LLMCall",
    "OpenAICompatibleStub",
    "StubRequest",
    "anthropic_status_error",
    "completion_body",
    "error_body",
    "genai_api_error",
    "gemini_error_body",
    "openai_connection_error",
    "openai_status_error",
    "openai_timeout_error",
    "raises",
]

# Module paths the fakes claim, as the real SDKs define them.
_OPENAI_ERRORS_MODULE = "openai._exceptions"
_ANTHROPIC_ERRORS_MODULE = "anthropic._exceptions"
_GENAI_ERRORS_MODULE = "google.genai.errors"
_API_CORE_ERRORS_MODULE = "google.api_core.exceptions"

_OPENAI_BASE_URL = "https://api.openai.com/v1/"
_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


# =============================================================================
# SDK-free exceptions
# =============================================================================


class _StatusErrorShape(Exception):
    """Shaped like an OpenAI / Anthropic ``APIStatusError``."""

    def __init__(
        self,
        status_code: int | None = None,
        *,
        message: str = "provider error",
        headers: dict[str, str] | None = None,
        code: str | None = None,
        error_type: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(headers=dict(headers or {}))
        self.body = body
        self.code = code
        self.type = error_type


def _sdk_api_error(module: str) -> type[_StatusErrorShape]:
    """A stand-in for an SDK's ``APIError``, the base of every failed API call it raises.

    The real status errors and connection errors both derive from it; the
    classifier reads a status-less exception as a connection failure only
    inside that family, so the stand-ins carry it too.
    """
    return type(
        "APIError",
        (_StatusErrorShape,),
        {"__module__": module, "__qualname__": "APIError"},
    )


class FakeOpenAIError(_sdk_api_error(_OPENAI_ERRORS_MODULE)):  # type: ignore[misc]
    """An exception the classifier reads as raised by the OpenAI SDK."""


FakeOpenAIError.__module__ = _OPENAI_ERRORS_MODULE


class FakeAnthropicError(_sdk_api_error(_ANTHROPIC_ERRORS_MODULE)):  # type: ignore[misc]
    """An exception the classifier reads as raised by the Anthropic SDK."""


FakeAnthropicError.__module__ = _ANTHROPIC_ERRORS_MODULE


class FakeGenaiError(Exception):
    """Shaped like ``google.genai.errors.APIError``: ``code``, ``status``, ``details``."""

    def __init__(
        self,
        code: int,
        details: Any = None,
        *,
        status: str = "",
        message: str = "provider error",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details
        self.message = message


FakeGenaiError.__module__ = _GENAI_ERRORS_MODULE


class FakeApiCoreError(Exception):
    """Shaped like a ``google.api_core`` exception, whose ``code`` is an ``HTTPStatus``.

    Every Google Cloud client library raises these, so the classifier must
    leave them alone.
    """

    def __init__(self, code: Any, message: str = "google cloud error") -> None:
        super().__init__(message)
        self.code = code


FakeApiCoreError.__module__ = _API_CORE_ERRORS_MODULE


def gemini_error_body(
    code: int,
    *,
    status: str = "RESOURCE_EXHAUSTED",
    retry_delay: str | None = None,
    quota_id: str | None = None,
) -> dict[str, Any]:
    """A Gemini error body, with a ``RetryInfo`` and a ``QuotaFailure`` when asked."""
    details: list[dict[str, Any]] = []
    if quota_id is not None:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaMetric": "requests", "quotaId": quota_id}],
            }
        )
    if retry_delay is not None:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": retry_delay,
            }
        )
    return {
        "error": {
            "code": code,
            "message": "provider error",
            "status": status,
            "details": details,
        }
    }


# =============================================================================
# Real SDK exceptions (import the SDK when called)
# =============================================================================


def _httpx2_response(status: int, url: str, headers: dict[str, str] | None) -> Any:
    import httpx2

    request = httpx2.Request("POST", url)
    return httpx2.Response(status, request=request, headers=headers or {})


def openai_status_error(
    status: int,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
    message: str = "provider error",
) -> Exception:
    """The OpenAI SDK's own exception for ``status``, as its client raises it."""
    import openai

    classes = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
        404: openai.NotFoundError,
        409: openai.ConflictError,
        422: openai.UnprocessableEntityError,
        429: openai.RateLimitError,
    }
    cls = classes.get(
        status, openai.InternalServerError if status >= 500 else openai.APIStatusError
    )
    response = _httpx2_response(status, _OPENAI_BASE_URL + "chat/completions", headers)
    return cls(message, response=response, body=body)


def openai_connection_error() -> Exception:
    """The OpenAI SDK's connection error: no status at all."""
    import httpx2
    import openai

    request = httpx2.Request("POST", _OPENAI_BASE_URL + "chat/completions")
    return openai.APIConnectionError(request=request)


def openai_timeout_error() -> Exception:
    """The OpenAI SDK's request timeout: no status at all."""
    import httpx2
    import openai

    request = httpx2.Request("POST", _OPENAI_BASE_URL + "chat/completions")
    return openai.APITimeoutError(request=request)


def anthropic_status_error(
    status: int,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
    message: str = "provider error",
) -> Exception:
    """The Anthropic SDK's own exception for ``status``, as its client raises it."""
    import anthropic

    classes = {
        400: anthropic.BadRequestError,
        401: anthropic.AuthenticationError,
        403: anthropic.PermissionDeniedError,
        404: anthropic.NotFoundError,
        413: anthropic.RequestTooLargeError,
        429: anthropic.RateLimitError,
        529: anthropic.OverloadedError,
    }
    cls = classes.get(
        status,
        anthropic.InternalServerError if status >= 500 else anthropic.APIStatusError,
    )
    response = _httpx2_response(status, _ANTHROPIC_BASE_URL + "/v1/messages", headers)
    return cls(message, response=response, body=body)


def genai_api_error(code: int, body: dict[str, Any] | None = None) -> Exception:
    """The Google Gen AI SDK's own exception for ``code``, as its client raises it."""
    from google.genai import errors

    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(
        code, body or {"error": {"code": code, "message": "provider error"}}, None
    )


# =============================================================================
# Fake SDK client tree
# =============================================================================

# The SDK's own "argument not given" marker: an unpassed keyword is not
# recorded, so a test can tell "the wrap added timeout=" from "nobody did".
_NOT_GIVEN: Any = object()


@dataclass
class LLMCall:
    """One call a fake client received."""

    method: str
    kwargs: dict[str, Any]
    max_retries: int
    client_timeout: Any


@dataclass
class _Script:
    """What the fake answers, call by call; the last answer repeats."""

    answers: list[Any]
    calls: list[LLMCall] = field(default_factory=list)

    def answer(self, call: LLMCall) -> Any:
        self.calls.append(call)
        outcome = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(call)
        return outcome


def raises(build: Callable[[], BaseException]) -> Callable[[LLMCall], Any]:
    """An answer that raises a new exception on every call, as an SDK does.

    A scripted exception *instance* is the same object on each call it answers,
    and a retry stage classifies one object once; a fresh one per call is what
    the provider's repeated refusals look like.
    """

    def answer(_call: LLMCall) -> Any:
        raise build()

    return answer


def _passed(**kwargs: Any) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value is not _NOT_GIVEN}


class _FakeClientBase:
    """Behavior shared by the sync and async fake clients."""

    def __init__(
        self,
        *,
        answers: Sequence[Any] = ("answered",),
        base_url: str = _OPENAI_BASE_URL,
        max_retries: int = 2,
        timeout: Any = 600.0,
        _script: _Script | None = None,
    ) -> None:
        self.base_url = base_url
        self.max_retries = max_retries
        self.timeout = timeout
        self._script = _script if _script is not None else _Script(list(answers))
        self.closed = False

    @property
    def calls(self) -> list[LLMCall]:
        """Every call this client or a ``with_options`` copy of it received."""
        return self._script.calls

    def with_options(self, **options: Any) -> Any:
        """A copy with the given client options; this client is not changed."""
        return type(self)(
            base_url=self.base_url,
            max_retries=options.get("max_retries", self.max_retries),
            timeout=options.get("timeout", self.timeout),
            _script=self._script,
        )

    def close(self) -> None:
        self.closed = True

    def _record(self, method: str, kwargs: dict[str, Any]) -> LLMCall:
        return LLMCall(
            method=method,
            kwargs=kwargs,
            max_retries=self.max_retries,
            client_timeout=self.timeout,
        )


# -- sync ----------------------------------------------------------------------


class _Completions:
    def __init__(self, client: FakeLLMClient) -> None:
        self._client = client

    def create(
        self,
        *,
        model: Any = _NOT_GIVEN,
        messages: Any = _NOT_GIVEN,
        timeout: Any = _NOT_GIVEN,
    ) -> Any:
        kwargs = _passed(model=model, messages=messages, timeout=timeout)
        return self._client._script.answer(
            self._client._record("chat.completions.create", kwargs)
        )


class _Chat:
    def __init__(self, client: FakeLLMClient) -> None:
        self.completions = _Completions(client)


class _Models:
    def __init__(self, client: FakeLLMClient) -> None:
        self._client = client

    def list(self, *, timeout: Any = _NOT_GIVEN) -> Any:
        return self._client._script.answer(
            self._client._record("models.list", _passed(timeout=timeout))
        )


class SyncAPIClient(_FakeClientBase):
    """Named like the OpenAI / Anthropic SDKs' sync base, which the wrap reads."""


class FakeLLMClient(SyncAPIClient):
    """A sync client of the OpenAI SDK's shape, answering from a script.

    ``answers`` is consumed one per call, the last one repeating: an exception
    instance is raised, a callable is called with the :class:`LLMCall`, any
    other value is returned.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.chat = _Chat(self)
        self.models = _Models(self)


# -- async ---------------------------------------------------------------------


def _sync_looking(method: Callable[..., Any]) -> Callable[..., Any]:
    """Hide a coroutine function behind a plain wrapper, as the SDKs' decorators do."""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return method(*args, **kwargs)

    return wrapper


class _AsyncCompletions:
    def __init__(self, client: FakeAsyncLLMClient) -> None:
        self._client = client

    @_sync_looking
    async def create(
        self,
        *,
        model: Any = _NOT_GIVEN,
        messages: Any = _NOT_GIVEN,
        timeout: Any = _NOT_GIVEN,
    ) -> Any:
        kwargs = _passed(model=model, messages=messages, timeout=timeout)
        return self._client._script.answer(
            self._client._record("chat.completions.create", kwargs)
        )


class _AsyncChat:
    def __init__(self, client: FakeAsyncLLMClient) -> None:
        self.completions = _AsyncCompletions(client)


class AsyncAPIClient(_FakeClientBase):
    """Named like the OpenAI / Anthropic SDKs' async base, which the wrap reads."""


class FakeAsyncLLMClient(AsyncAPIClient):
    """The async twin of :class:`FakeLLMClient`.

    Its ``create`` is an ``async def`` hidden behind a sync-looking decorator,
    the shape the real SDKs give their coroutine methods.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.chat = _AsyncChat(self)


# -- another SDK ---------------------------------------------------------------


class _Messages:
    def __init__(self, client: FakeOtherSdkClient) -> None:
        self._client = client

    def create(self, *, model: Any = _NOT_GIVEN) -> Any:
        return self._client._script.answer(
            self._client._record("messages.create", _passed(model=model))
        )


class FakeOtherSdkClient(SyncAPIClient):
    """A sync client of the Anthropic SDK's shape — a different SDK family."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("base_url", _ANTHROPIC_BASE_URL)
        super().__init__(**kwargs)
        self.messages = _Messages(self)


# The SDK every fake class belongs to, by the module it claims. Resources need it
# too: the wrap protects a method only when the object it hangs off belongs to
# the primary client's SDK.
for _cls in (
    SyncAPIClient,
    AsyncAPIClient,
    FakeLLMClient,
    FakeAsyncLLMClient,
    _Chat,
    _Completions,
    _Models,
    _AsyncChat,
    _AsyncCompletions,
):
    _cls.__module__ = "openai._client"
for _cls in (FakeOtherSdkClient, _Messages):
    _cls.__module__ = "anthropic._client"
del _cls


# =============================================================================
# A local OpenAI-compatible server, for the real OpenAI SDK
# =============================================================================


def completion_body(content: str) -> dict[str, Any]:
    """A chat-completions answer whose message is ``content``."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def error_body(message: str, error_type: str) -> dict[str, Any]:
    """An OpenAI-format error body."""
    return {
        "error": {"message": message, "type": error_type, "param": None, "code": None}
    }


# What the stub answers one request with: (status, JSON body, headers), or
# ``HANG`` to hold the request open until the stub is closed.
HANG: Any = object()


@dataclass
class StubRequest:
    """One request the stub received: when (``time.time()``) and its last message."""

    at: float
    content: str


class OpenAICompatibleStub:
    """A local HTTP server speaking the OpenAI chat-completions API.

    ``answer(content)`` decides each response from the request's last message
    and may be replaced while the server runs. Every request is recorded with
    its wall-clock arrival, so a test in another process can be compared with
    it. Use as a context manager; closing releases any held request.
    """

    def __init__(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.answer: Callable[[str], Any] = lambda content: (
            200,
            completion_body(f"answer to {content}"),
            {},
        )
        self.requests: list[StubRequest] = []
        self._lock = threading.Lock()
        self._release = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 — the stdlib's handler name
                import json
                import time

                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    content = str(payload["messages"][-1]["content"])
                except (ValueError, KeyError, IndexError, TypeError):
                    content = ""
                with stub._lock:
                    stub.requests.append(StubRequest(at=time.time(), content=content))
                decision = stub.answer(content)
                if decision is HANG:
                    stub._release.wait(timeout=60.0)
                    return
                status, body, headers = decision
                data = json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    return

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def contents(self) -> list[str]:
        """The last message of every request received, in arrival order."""
        with self._lock:
            return [request.content for request in self.requests]

    def __enter__(self) -> OpenAICompatibleStub:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)
