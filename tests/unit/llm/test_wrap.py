"""``baldur.llm.wrap``: one line around an LLM client, endpoint by endpoint.

Target: ``baldur.llm`` (``wrap``, ``Endpoint``) — the wrapped client's resource
walk, its endpoint identities, the move rule after an endpoint fails, the
terminal ``LLMUnavailableError``, and the request-scoped deadline it hands the
SDK as a timeout.

Every endpoint call runs under a real ``protect()`` — real retry stage, real
breaker, a real coordinator over an in-memory store — so a move is decided by
what the stages actually raised. The SDK is a fake client tree
(``tests.factories.llm_doubles``) that answers from a script and records every
call; the identity checks against the Google Gen AI SDK use the real client.

UNIT_TEST_GUIDELINES.md:
- Behavior: identities are an input → output mapping, asserted as written
  (§2.1); counts are read from the endpoints' own call records.
- No ``time.sleep`` (§6.3): the retry ladder's sleeper is patched out and the
  coordinator's waits are served by ``mock_sleep``.
- Move tests name their endpoints (``Endpoint(name=...)``), one fresh name per
  test, so no breaker or wait outlives its test; the derived names are pinned
  by the construction tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

import baldur
from baldur import protect_facade
from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.core.exceptions import (
    CircuitBreakerError,
    LLMUnavailableError,
    TimeoutPolicyError,
)
from baldur.llm import Endpoint, wrap
from baldur.protect_facade import protected
from baldur.scaling.deadline_context import deadline_scope, get_remaining_ms
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitCoordinatorConfig
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.sinks import retry_exhausted_failure_type
from baldur.settings.protect import reset_protect_settings
from baldur.utils.domain_validation import validate_and_normalize_domain
from tests.factories.llm_doubles import (
    FakeAnthropicError,
    FakeAsyncLLMClient,
    FakeGenaiError,
    FakeLLMClient,
    FakeOpenAIError,
    FakeOtherSdkClient,
    openai_status_error,
    raises,
)
from tests.factories.time_helpers import MockSleep, mock_sleep

_SYNC_RETRY_SLEEP = "baldur.services.retry_handler.policy._DEFAULT_SLEEPER"
_ASYNC_SLEEP = "baldur.resilience.policies.async_retry.asyncio.sleep"
_STORE = "baldur.services.retry_handler.sinks.store_to_dlq"
_MOVE_LOG = "llm.endpoint_call_failed"
_REMAINING_MS = "baldur.scaling.deadline_context.get_remaining_ms"


# =============================================================================
# Fixtures and helpers
# =============================================================================


@pytest.fixture
def coordinator() -> Iterator[RateLimitCoordinator]:
    """The process coordinator, over its own in-memory store, without jitter."""
    instance = RateLimitCoordinator(
        storage=InMemoryRateLimitStorage(),
        config=RateLimitCoordinatorConfig(
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
            default_retry_after=0.5,
        ),
    )
    with patch.object(RateLimitCoordinator, "_instance", instance):
        yield instance
        RateLimitCoordinator.reset_instance()


@pytest.fixture(autouse=True)
def sandbox(coordinator) -> Iterator[MockSleep]:
    """Fresh protect caches; no retry sleep; coordinator waits recorded, not slept."""
    reset_protect_settings()
    with patch(_SYNC_RETRY_SLEEP, lambda _seconds: None), mock_sleep() as slept:
        yield slept
    reset_protect_settings()


def _name(role: str) -> str:
    return f"llm.{role}_{uuid.uuid4().hex[:10]}"


def _host() -> str:
    return f"h{uuid.uuid4().hex[:10]}.example.com"


def _client(*answers, cls=FakeLLMClient, host: str | None = None):
    return cls(
        answers=list(answers) or ["answered"],
        base_url=f"https://{host or _host()}/v1",
    )


def _named(client, role: str, **endpoint) -> tuple[Endpoint, str]:
    name = _name(role)
    return Endpoint(client, name=name, **endpoint), name


def _seed_open_breaker(name: str) -> None:
    """Install an OPEN breaker under ``name`` in protect()'s per-name cache."""
    service = CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True,
            failure_threshold=1,
            minimum_calls=1,
            failure_rate_threshold=0,
            recovery_timeout=600,
        ),
        repository=InMemoryCircuitBreakerStateRepository(),
    )
    service.record_failure(name, error_context={"error": "seeded", "type": "Seeded"})
    protect_facade._cb_policy_cache[name] = CircuitBreakerPolicy(
        service_name=name, cb_service=service, hooks=[]
    )


def _move_logs(logs: list[dict]) -> list[dict]:
    return [log for log in logs if log["event"] == _MOVE_LOG]


def _fallback_count(name: str, mode: str) -> float:
    from prometheus_client import REGISTRY

    sample = REGISTRY.get_sample_value(
        "baldur_protect_fallback_total", {"name": name, "mode": mode}
    )
    return sample or 0.0


def _create(wrapped, **kwargs):
    kwargs.setdefault("model", "gpt-4o")
    kwargs.setdefault("messages", [{"role": "user", "content": "hi"}])
    return wrapped.chat.completions.create(**kwargs)


class _Cancelled(BaseException):
    """A cancellation, which no stage may swallow."""


def _overloaded_generate_content(*, model: str, contents: str) -> str:
    """Stands in for a Gen AI ``generate_content`` while the provider is overloaded."""
    raise FakeGenaiError(503, status="UNAVAILABLE")


# =============================================================================
# Contract — the public surface
# =============================================================================


class TestLlmSurfaceContract:
    """``baldur.llm`` is a lazy subpackage with two public names."""

    def test_llm_all_contract(self):
        """``wrap`` and ``Endpoint`` — nothing else is public."""
        assert baldur.llm.__all__ == ["Endpoint", "wrap"]

    def test_llm_is_reachable_but_not_a_top_level_symbol(self):
        """``baldur.llm.wrap`` works; ``from baldur import *`` does not pull it in."""
        assert baldur.llm.wrap is wrap
        assert "llm" not in baldur.__all__

    def test_import_baldur_does_not_load_llm_until_it_is_reached(self):
        """The import closure is unchanged: the package loads on first access only."""
        code = (
            "import sys, baldur\n"
            "print('baldur.llm' in sys.modules)\n"
            "baldur.llm.wrap\n"
            "print('baldur.llm' in sys.modules)\n"
        )
        result = subprocess.run(
            [sys.executable, "-P", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["False", "True"]


# =============================================================================
# Behavior — construction
# =============================================================================


class TestWrapConstructionBehavior:
    """Endpoint names, the prepared client copy, and the combinations refused at wrap time."""

    @pytest.mark.parametrize(
        ("base_url", "pinned", "call_model", "identity"),
        [
            (
                "https://api.openai.com/v1",
                None,
                "gpt-4o-mini",
                "llm.api_openai_com.gpt_4o_mini",
            ),
            (
                "http://LOCALHOST:11434/v1",
                None,
                "Llama3.1:8B",
                "llm.localhost.llama3_1_8b",
            ),
            (
                "https://openrouter.ai/api/v1",
                "meta-llama/llama-3.1-70b",
                "gpt-4o",
                "llm.openrouter_ai.meta_llama_llama_3_1_70b",
            ),
        ],
        ids=["openai", "ollama_case_and_port", "endpoint_model_wins"],
    )
    def test_derived_identity_is_host_and_model(
        self, base_url, pinned, call_model, identity
    ):
        """``llm.<host>.<model>``, lowercased, every other character an underscore."""
        client = FakeLLMClient(
            answers=[FakeOpenAIError(429, code="insufficient_quota")], base_url=base_url
        )
        wrapped = wrap(Endpoint(client, model=pinned))

        with pytest.raises(LLMUnavailableError) as raised:
            _create(wrapped, model=call_model)

        assert raised.value.attempts == ((identity, "quota_exhausted"),)

    def test_account_scoped_call_identity_has_no_model(self):
        """A call without ``model=`` is named after the host alone."""
        client = FakeLLMClient(
            answers=[FakeOpenAIError(401)], base_url="https://api.openai.com/v1"
        )

        with pytest.raises(LLMUnavailableError) as raised:
            wrap(client).models.list()

        assert raised.value.attempts == (("llm.api_openai_com", "auth_failed"),)

    @pytest.mark.parametrize(
        ("model_length", "truncated"),
        [(55, False), (56, True)],
        ids=["exactly_64_kept", "65_truncated"],
    )
    def test_derived_identity_is_held_to_64_characters(self, model_length, truncated):
        """Over 64 characters: cut to 55, then ``_`` and 8 hex digits of its sha1."""
        model = "m" * model_length
        full = f"llm.h_io.{model}"
        client = FakeLLMClient(
            answers=[FakeOpenAIError(402)], base_url="https://h.io/v1"
        )

        with pytest.raises(LLMUnavailableError) as raised:
            _create(wrap(client), model=model)

        identity = raised.value.attempts[0][0]
        assert len(identity) == min(len(full), 64)
        if truncated:
            suffix = hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]
            assert identity == f"{full[:55]}_{suffix}"
        else:
            assert identity == full
        assert validate_and_normalize_domain(identity) == identity

    def test_endpoint_name_replaces_the_derived_identity(self):
        """``Endpoint(name=...)`` is the name the breaker and the wait are kept under."""
        endpoint, name = _named(_client(FakeOpenAIError(402)), "named")

        with pytest.raises(LLMUnavailableError) as raised:
            _create(wrap(endpoint))

        assert raised.value.attempts == ((name, "quota_exhausted"),)

    def test_wrap_name_names_the_primary(self):
        """``wrap(client, name=...)`` is the primary's ``Endpoint(name=...)``."""
        name = _name("primary")

        with pytest.raises(LLMUnavailableError) as raised:
            _create(wrap(_client(FakeOpenAIError(402)), name=name))

        assert raised.value.attempts[0][0] == name

    def test_genai_developer_and_vertex_clients_each_get_their_own_identity(self):
        """No base URL on a Gen AI client: its ``vertexai`` flag names the host."""
        # Given — the real SDK's two modes on one model
        genai = pytest.importorskip("google.genai")
        developer = genai.Client(api_key="test-key")
        vertex = genai.Client(vertexai=True, project="p", location="us-central1")
        for client in (developer, vertex):
            client.models.generate_content = _overloaded_generate_content
        wrapped = wrap(developer, fallbacks=[vertex])

        # When
        with pytest.raises(LLMUnavailableError) as raised:
            wrapped.models.generate_content(model="gemini-2.0-flash", contents="hi")

        # Then
        assert [identity for identity, _ in raised.value.attempts] == [
            "llm.generativelanguage_googleapis_com.gemini_2_0_flash",
            "llm.aiplatform_googleapis_com.gemini_2_0_flash",
        ]

    def test_two_genai_developer_clients_on_one_model_are_refused_identity(self):
        """Two keys on one host share every name, so the second could never answer."""
        genai = pytest.importorskip("google.genai")

        with pytest.raises(ValueError, match="Endpoint\\(name=...\\)"):
            wrap(
                genai.Client(api_key="key-a"),
                fallbacks=[genai.Client(api_key="key-b")],
            )

    def test_prepared_copy_has_sdk_retries_off_and_callers_client_is_untouched(self):
        """Baldur's coordinated retry is the only retry loop; the caller's object keeps its own."""
        client = _client("answered")

        wrapped = wrap(client, timeout=30.0)
        _create(wrapped)

        assert (client.calls[0].max_retries, client.calls[0].client_timeout) == (
            0,
            30.0,
        )
        assert (client.max_retries, client.timeout) == (2, 600.0)
        assert wrapped.max_retries == 0

    def test_real_openai_client_is_copied_with_retries_off(self):
        """The OpenAI SDK's own ``with_options`` copy: 0 on the copy, 2 on the original."""
        openai = pytest.importorskip("openai")
        client = openai.OpenAI(api_key="test-key")

        wrapped = wrap(client)

        assert (wrapped.max_retries, client.max_retries) == (0, 2)

    def test_plain_attributes_pass_through_and_the_wrapper_is_not_the_sdk_class(self):
        """Non-callable values are the client's own; ``isinstance`` checks see a wrapper."""
        client = _client()
        wrapped = wrap(client)

        assert wrapped.base_url == client.base_url
        assert not isinstance(wrapped, FakeLLMClient)
        assert "baldur.llm.wrap" in repr(wrapped)

    @pytest.mark.parametrize(
        "build",
        [
            lambda host: (
                FakeLLMClient(base_url=f"https://{host}/v1"),
                [FakeLLMClient(base_url=f"https://{host}/v1")],
                {},
            ),
            lambda host: (
                Endpoint(FakeLLMClient(base_url=f"https://{host}/v1"), model="gpt-4o"),
                [
                    Endpoint(
                        FakeLLMClient(base_url=f"https://{host}/v1"), model="gpt-4o"
                    )
                ],
                {},
            ),
            lambda host: (
                Endpoint(FakeLLMClient(base_url="https://a.example.com/v1"), name="x"),
                [
                    Endpoint(
                        FakeLLMClient(base_url="https://b.example.com/v1"), name="x"
                    )
                ],
                {},
            ),
            lambda host: (FakeLLMClient(), [FakeOtherSdkClient()], {}),
            lambda host: (
                FakeLLMClient(base_url=f"https://{host}/v1"),
                [FakeAsyncLLMClient(base_url="https://other.example.com/v1")],
                {},
            ),
            lambda host: (
                Endpoint(FakeLLMClient(), name="primary"),
                [],
                {"name": "again"},
            ),
            lambda host: (
                Endpoint(
                    FakeLLMClient(base_url=f"https://{host}/v1"), model="llama3.1-8b"
                ),
                [
                    Endpoint(
                        FakeLLMClient(base_url=f"https://{host}/v1"),
                        model="llama3.1:8b",
                    )
                ],
                {},
            ),
            lambda host: (
                Endpoint(
                    FakeLLMClient(base_url=f"https://{host}/v1"),
                    name="llm." + re.sub(r"[^a-z0-9]", "_", host) + ".gpt_4o",
                ),
                [
                    Endpoint(
                        FakeLLMClient(base_url=f"https://{host}/v1"), model="gpt-4o"
                    )
                ],
                {},
            ),
        ],
        ids=[
            "same_host_no_model_pin",
            "same_host_same_model_pin",
            "same_endpoint_name",
            "different_sdk",
            "sync_and_async",
            "named_twice",
            "model_pins_one_name_once_normalized",
            "name_equal_to_a_pinned_endpoints_name",
        ],
    )
    def test_shared_identity_or_mixed_clients_are_refused_at_wrap_time(self, build):
        """A wrap that could never answer where its primary did not is refused up front."""
        primary, fallbacks, options = build(_host())

        with pytest.raises(ValueError):
            wrap(primary, fallbacks=fallbacks, **options)

    @pytest.mark.parametrize(
        "build",
        [
            lambda client: [Endpoint(client, model="gpt-4o-mini")],
            lambda client: [Endpoint(client, name="llm.second_endpoint")],
        ],
        ids=["same_client_other_model", "same_client_named"],
    )
    def test_endpoints_with_a_distinct_identity_are_accepted(self, build):
        """One client under another model pin, or another name, is a real second endpoint."""
        client = _client()

        wrapped = wrap(client, fallbacks=build(client))

        assert _create(wrapped) == "answered"

    @pytest.mark.parametrize(
        ("sdk", "read"),
        [
            ("anthropic", lambda wrapped: wrapped.messages.stream.__self__._client),
            (
                "openai",
                lambda wrapped: wrapped.chat.completions.stream.__self__._client,
            ),
            ("openai", lambda wrapped: wrapped.with_streaming_response._client),
            (
                "openai",
                lambda wrapped: (
                    wrapped.chat.completions.with_streaming_response._completions._client
                ),
            ),
        ],
        ids=[
            "anthropic_messages_stream",
            "openai_chat_stream",
            "openai_client_with_streaming_response",
            "openai_resource_with_streaming_response",
        ],
    )
    def test_deferred_request_helpers_run_on_the_callers_own_client(self, sdk, read):
        """A helper that sends its request after it returns keeps the SDK's own retries."""
        module = pytest.importorskip(sdk)
        client = (module.Anthropic if sdk == "anthropic" else module.OpenAI)(
            api_key="test-key"
        )

        assert read(wrap(client)) is client

    def test_derived_identity_segments_are_lowercase_alphanumerics(self):
        """Every derived name is a valid domain: ``[a-z0-9_.]`` only."""
        client = FakeLLMClient(
            answers=[FakeOpenAIError(402)], base_url="https://My-Host.Example.COM/v1"
        )

        with pytest.raises(LLMUnavailableError) as raised:
            _create(wrap(client), model="Org/Model@v2 (beta)")

        identity = raised.value.attempts[0][0]
        assert identity == "llm.my_host_example_com.org_model_v2__beta_"
        assert re.fullmatch(r"[a-z0-9_.]+", identity)


# =============================================================================
# Behavior — the move rule (sync)
# =============================================================================


class TestWrapMoveRuleBehavior:
    """After an endpoint fails: move on, re-raise as is, or end in ``LLMUnavailableError``."""

    @pytest.mark.parametrize(
        ("sdk", "build"),
        [
            (None, lambda: FakeOpenAIError(429, code="insufficient_quota")),
            (
                "openai",
                lambda: openai_status_error(429, body={"code": "insufficient_quota"}),
            ),
            (None, lambda: FakeOpenAIError(402)),
        ],
        ids=["quota_fake", "quota_real_openai", "quota_402"],
    )
    def test_quota_moves_at_once_without_retry_or_wait(self, sdk, build, coordinator):
        """An exhausted quota: one call, no wait installed there, the next endpoint answers."""
        # Given
        if sdk is not None:
            pytest.importorskip(sdk)
        primary_client = _client(build())
        fallback_client = _client("from fallback")
        primary, primary_name = _named(primary_client, "primary")
        fallback, _ = _named(fallback_client, "fallback")

        # When
        answer = _create(wrap(primary, fallbacks=[fallback]))

        # Then
        assert answer == "from fallback"
        assert len(primary_client.calls) == 1
        assert coordinator.get_state(primary_name).cooldown_until <= time.time()
        assert len(fallback_client.calls) == 1

    @pytest.mark.parametrize(
        "build",
        [
            lambda: FakeOpenAIError(429),
            lambda: FakeAnthropicError(529),
            lambda: FakeOpenAIError(503),
        ],
        ids=["rate_limited", "overloaded_529", "overloaded_503"],
    )
    def test_limit_or_overload_is_waited_and_retried_then_moves(
        self, build, coordinator, sandbox
    ):
        """The fleet wait is installed and served on the endpoint before the move."""
        # Given — the provider refuses every attempt, each with a fresh error
        primary_client = _client(raises(build))
        fallback_client = _client("from fallback")
        primary, primary_name = _named(primary_client, "primary")
        fallback, _ = _named(fallback_client, "fallback")

        # When
        answer = _create(wrap(primary, fallbacks=[fallback]))

        # Then
        expected_attempts = RetryPolicyConfig.from_settings(
            domain=primary_name
        ).max_attempts
        assert answer == "from fallback"
        assert len(primary_client.calls) == expected_attempts
        assert coordinator.get_state(primary_name).consecutive_429s == expected_attempts
        assert sandbox.total_slept > 0

    def test_provider_invalid_request_is_reraised_and_nothing_else_is_called(self):
        """A rejected request is the caller's to fix: same error, no retry, no move."""
        error = FakeOpenAIError(400, message="Invalid 'messages'")
        primary_client = _client(error)
        fallback_client = _client("from fallback")

        with pytest.raises(FakeOpenAIError) as raised:
            _create(wrap(primary_client, fallbacks=[fallback_client]))

        assert raised.value is error
        assert len(primary_client.calls) == 1
        assert fallback_client.calls == []

    def test_non_move_eligible_error_is_reraised_without_a_move(self):
        """An SDK argument check or a caller bug is not the provider's answer."""
        error = TypeError("create() got an unexpected keyword argument 'modle'")
        fallback_client = _client("from fallback")

        with pytest.raises(TypeError) as raised:
            _create(wrap(_client(error), fallbacks=[fallback_client]))

        assert raised.value is error
        assert fallback_client.calls == []

    @pytest.mark.parametrize(
        "module",
        ["openai._exceptions", "anthropic._exceptions", "google.genai.errors"],
        ids=["openai", "anthropic", "google_genai"],
    )
    def test_sdk_argument_check_is_reraised_without_a_move(self, module):
        """An error the SDK raised before sending anything is not the provider's answer."""
        error_class = type(
            "UnsupportedFunctionError", (ValueError,), {"__module__": module}
        )
        error = error_class("an async function was passed to a sync client")
        fallback_client = _client("from fallback")

        with pytest.raises(ValueError) as raised:
            _create(wrap(_client(error), fallbacks=[fallback_client]))

        assert raised.value is error
        assert fallback_client.calls == []

    @pytest.mark.parametrize(
        "error",
        [
            ConnectionError("connection refused"),
            TimeoutError("read timed out"),
            TimeoutPolicyError(30.0, "cut off"),
            FakeOpenAIError(None, message="Connection error."),
        ],
        ids=[
            "builtin_connection",
            "builtin_timeout",
            "timeout_policy",
            "sdk_connection",
        ],
    )
    def test_transport_failure_moves_as_transient(self, error):
        """A failure below the SDK moves like any connection error, logged as transient."""
        primary, primary_name = _named(_client(error), "primary")
        fallback, _ = _named(_client("from fallback"), "fallback")

        with capture_logs() as logs:
            answer = _create(wrap(primary, fallbacks=[fallback]))

        assert answer == "from fallback"
        assert [(log["endpoint"], log["category"]) for log in _move_logs(logs)] == [
            (primary_name, "transient")
        ]

    def test_httpx_transport_error_moves(self):
        """A raw transport error from the HTTP library underneath is a move too."""
        httpx = pytest.importorskip("httpx")
        error = httpx.ConnectError("connection refused")
        fallback_client = _client("from fallback")

        answer = _create(wrap(_client(error), fallbacks=[fallback_client]))

        assert answer == "from fallback"

    def test_move_observed_auth_failure_logs_once_and_counts_one_fallback(self):
        """A 401 then an answer: one WARNING naming ``auth_failed``, one fallback counted."""
        # Given
        pytest.importorskip("openai")
        pytest.importorskip("prometheus_client")
        primary, primary_name = _named(_client(openai_status_error(401)), "primary")
        fallback, fallback_name = _named(_client("from fallback"), "fallback")
        before = _fallback_count(primary_name, "sync")

        # When
        with capture_logs() as logs:
            answer = _create(wrap(primary, fallbacks=[fallback]))

        # Then
        assert answer == "from fallback"
        moves = _move_logs(logs)
        assert [
            (
                log["log_level"],
                log["endpoint"],
                log["next_endpoint"],
                log["category"],
                log["status"],
                log["error_type"],
            )
            for log in moves
        ] == [
            (
                "warning",
                primary_name,
                fallback_name,
                "auth_failed",
                401,
                "AuthenticationError",
            )
        ]
        assert _fallback_count(primary_name, "sync") == before + 1

    def test_primary_answer_counts_no_fallback_and_logs_no_move(self):
        """When the first endpoint answers, nothing moved: no WARNING, no fallback counted."""
        pytest.importorskip("prometheus_client")
        primary, primary_name = _named(_client("from primary"), "primary")
        fallback_client = _client("from fallback")
        fallback, _ = _named(fallback_client, "fallback")
        before = _fallback_count(primary_name, "sync")

        with capture_logs() as logs:
            answer = _create(wrap(primary, fallbacks=[fallback]))

        assert answer == "from primary"
        assert fallback_client.calls == []
        assert _move_logs(logs) == []
        assert _fallback_count(primary_name, "sync") == before

    def test_move_observed_past_an_open_breaker_writes_no_warning(self):
        """The breaker already says why: the provider is not called, nothing is logged."""
        pytest.importorskip("prometheus_client")
        primary_client = _client("never reached")
        primary, primary_name = _named(primary_client, "primary")
        fallback, _ = _named(_client("from fallback"), "fallback")
        _seed_open_breaker(primary_name)
        before = _fallback_count(primary_name, "sync")

        with capture_logs() as logs:
            answer = _create(wrap(primary, fallbacks=[fallback]))

        assert answer == "from fallback"
        assert primary_client.calls == []
        assert _move_logs(logs) == []
        assert _fallback_count(primary_name, "sync") == before + 1

    def test_move_past_a_wait_longer_than_the_bound_writes_no_warning(
        self, coordinator
    ):
        """A cooldown the endpoint may not sleep through is deferred and stepped over."""
        primary_client = _client("never reached")
        primary, primary_name = _named(primary_client, "primary")
        fallback, _ = _named(_client("from fallback"), "fallback")
        coordinator.on_rate_limited(primary_name, retry_after=3600)

        with capture_logs() as logs:
            answer = _create(wrap(primary, fallbacks=[fallback]))

        assert answer == "from fallback"
        assert primary_client.calls == []
        assert _move_logs(logs) == []

    def test_last_endpoint_failure_is_not_logged_as_a_move(self):
        """A WARNING says where the call went next; after the last endpoint it went nowhere."""
        with capture_logs() as logs, pytest.raises(LLMUnavailableError):
            _create(wrap(_client(FakeOpenAIError(401))))

        assert _move_logs(logs) == []

    def test_unavailable_when_every_endpoint_fails_with_attempts_and_cause(self):
        """No endpoint answered: one error, every endpoint and why, the last error chained."""
        # Given
        last_error = FakeOpenAIError(401)
        primary, primary_name = _named(_client(FakeOpenAIError(402)), "primary")
        fallback, fallback_name = _named(_client(last_error), "fallback")

        # When
        with pytest.raises(LLMUnavailableError) as raised:
            _create(wrap(primary, fallbacks=[fallback]))

        # Then
        assert raised.value.attempts == (
            (primary_name, "quota_exhausted"),
            (fallback_name, "auth_failed"),
        )
        assert raised.value.__cause__ is last_error
        assert primary_name in str(raised.value)
        assert fallback_name in str(raised.value)

    def test_unavailable_when_every_breaker_is_open_never_escapes_as_breaker_error(
        self,
    ):
        """An open breaker never reaches the job as itself: it would park under the endpoint."""
        primary_client = _client("never reached")
        fallback_client = _client("never reached")
        primary, primary_name = _named(primary_client, "primary")
        fallback, fallback_name = _named(fallback_client, "fallback")
        _seed_open_breaker(primary_name)
        _seed_open_breaker(fallback_name)

        with pytest.raises(LLMUnavailableError) as raised:
            _create(wrap(primary, fallbacks=[fallback]))

        assert raised.value.attempts == (
            (primary_name, "breaker_open"),
            (fallback_name, "breaker_open"),
        )
        assert isinstance(raised.value.__cause__, CircuitBreakerError)
        assert (primary_client.calls, fallback_client.calls) == ([], [])

    def test_account_scoped_call_runs_on_the_primary_only(self):
        """Files, batches, model listing: no ``model=``, so no other endpoint is asked."""
        primary_client = _client(FakeOpenAIError(503))
        fallback_client = _client("from fallback")

        with pytest.raises(LLMUnavailableError) as raised:
            wrap(primary_client, fallbacks=[fallback_client]).models.list()

        assert len(raised.value.attempts) == 1
        assert fallback_client.calls == []

    def test_endpoint_model_replaces_the_call_model_on_that_endpoint(self):
        """A fallback serves the job under the model its own provider calls it."""
        primary_client = _client(FakeOpenAIError(402))
        fallback_client = _client("from fallback")
        messages = [{"role": "user", "content": "summarize"}]

        _create(
            wrap(
                primary_client,
                fallbacks=[Endpoint(fallback_client, model="llama-3.1-70b")],
            ),
            model="gpt-4o",
            messages=messages,
        )

        assert primary_client.calls[0].kwargs == {
            "model": "gpt-4o",
            "messages": messages,
        }
        assert fallback_client.calls[0].kwargs == {
            "model": "llama-3.1-70b",
            "messages": messages,
        }

    def test_cancellation_propagates_untouched(self):
        """A ``BaseException`` is neither classified, retried nor moved."""
        primary_client = _client(_Cancelled("stop"))
        fallback_client = _client("from fallback")

        with pytest.raises(_Cancelled):
            _create(wrap(primary_client, fallbacks=[fallback_client]))

        assert len(primary_client.calls) == 1
        assert fallback_client.calls == []

    @pytest.mark.parametrize("failure", ["unavailable", "breakers_open"])
    def test_default_dlq_parks_only_the_job_never_an_endpoint(
        self, failure, monkeypatch
    ):
        """With ``BALDUR_PROTECT_DEFAULT_DLQ=true`` the job is parked once, under its own name."""
        # Given
        monkeypatch.setenv("BALDUR_PROTECT_DEFAULT_DLQ", "true")
        reset_protect_settings()
        primary, primary_name = _named(_client(FakeOpenAIError(503)), "primary")
        fallback, fallback_name = _named(_client(FakeOpenAIError(503)), "fallback")
        if failure == "breakers_open":
            _seed_open_breaker(primary_name)
            _seed_open_breaker(fallback_name)
        llm = wrap(primary, fallbacks=[fallback])
        job_name = f"job.summarize_{uuid.uuid4().hex[:8]}"

        @protected(job_name, dlq=True, circuit_breaker=False)
        def summarize(doc_id: str) -> str:
            return _create(llm, messages=[{"role": "user", "content": doc_id}])

        # When
        with patch(_STORE, autospec=True) as store, pytest.raises(LLMUnavailableError):
            summarize("doc-1")

        # Then
        parked = [
            (call.kwargs["domain"], call.kwargs["failure_type"])
            for call in store.call_args_list
        ]
        assert parked == [
            (job_name, retry_exhausted_failure_type(LLMUnavailableError.__name__))
        ]
        assert store.call_args.kwargs["request_data"] == {"doc_id": "doc-1"}


# =============================================================================
# Behavior — the move rule (async)
# =============================================================================


class TestAsyncWrapMoveRuleBehavior:
    """The async client: the same rule, through ``aprotect`` and the async stages."""

    @pytest.fixture(autouse=True)
    def _no_async_sleep(self) -> Iterator[None]:
        with patch(_ASYNC_SLEEP, new_callable=AsyncMock):
            yield

    def test_wrapped_async_method_is_a_coroutine_function(self):
        """An SDK hides its ``async def`` behind a decorator; the wrap sees through it."""
        wrapped = wrap(_client(cls=FakeAsyncLLMClient))

        assert inspect.iscoroutinefunction(wrapped.chat.completions.create)

    def test_async_endpoint_call_runs_the_async_retry_stage(self):
        """A real coroutine reaches ``aprotect``: an overload is retried, then answered."""
        client = _client(FakeAnthropicError(529), "answered", cls=FakeAsyncLLMClient)

        answer = asyncio.run(_create(wrap(client)))

        assert answer == "answered"
        assert len(client.calls) == 2

    def test_async_quota_moves_and_counts_one_fallback_as_async(self):
        """An exhausted quota moves at once; the fallback is counted under ``mode=async``."""
        pytest.importorskip("prometheus_client")
        primary_client = _client(
            FakeOpenAIError(429, code="insufficient_quota"), cls=FakeAsyncLLMClient
        )
        primary, primary_name = _named(primary_client, "primary")
        fallback, _ = _named(
            _client("from fallback", cls=FakeAsyncLLMClient), "fallback"
        )
        before = _fallback_count(primary_name, "async")

        answer = asyncio.run(_create(wrap(primary, fallbacks=[fallback])))

        assert answer == "from fallback"
        assert len(primary_client.calls) == 1
        assert _fallback_count(primary_name, "async") == before + 1

    def test_async_provider_invalid_request_is_reraised_and_nothing_else_is_called(
        self,
    ):
        """The async path refuses to retry or move a rejected request too."""
        error = FakeOpenAIError(422)
        primary_client = _client(error, cls=FakeAsyncLLMClient)
        fallback_client = _client("from fallback", cls=FakeAsyncLLMClient)

        with pytest.raises(FakeOpenAIError) as raised:
            asyncio.run(_create(wrap(primary_client, fallbacks=[fallback_client])))

        assert raised.value is error
        assert len(primary_client.calls) == 1
        assert fallback_client.calls == []

    def test_async_unavailable_when_every_endpoint_fails(self):
        """No async endpoint answered: ``LLMUnavailableError`` with attempts and cause."""
        last_error = FakeOpenAIError(403)
        primary, primary_name = _named(
            _client(FakeOpenAIError(402), cls=FakeAsyncLLMClient), "primary"
        )
        fallback, fallback_name = _named(
            _client(last_error, cls=FakeAsyncLLMClient), "fallback"
        )

        with pytest.raises(LLMUnavailableError) as raised:
            asyncio.run(_create(wrap(primary, fallbacks=[fallback])))

        assert raised.value.attempts == (
            (primary_name, "quota_exhausted"),
            (fallback_name, "auth_failed"),
        )
        assert raised.value.__cause__ is last_error


# =============================================================================
# Behavior — the request-scoped deadline
# =============================================================================


class TestWrapDeadlineBehavior:
    """Inside a deadline the SDK call gets the time left as its timeout; outside, nothing."""

    def test_call_inside_a_deadline_gets_the_time_left_as_its_timeout(self):
        """The SDK is told to stop by the deadline, not after its own 600 s."""
        client = _client()

        with deadline_scope(10_000):
            bound = get_remaining_ms() / 1000.0
            _create(wrap(client))

        timeout = client.calls[0].kwargs["timeout"]
        assert 0 < timeout <= bound

    def test_caller_numeric_timeout_below_the_time_left_is_kept(self):
        """The tightest bound wins: here the caller's own timeout."""
        client = _client()

        with deadline_scope(10_000):
            _create(wrap(client), timeout=2.5)

        assert client.calls[0].kwargs["timeout"] == 2.5

    def test_wrap_numeric_timeout_below_the_time_left_is_kept(self):
        """The wrap's own ``timeout=`` bounds the call inside a deadline too."""
        client = _client()

        with deadline_scope(10_000):
            _create(wrap(client, timeout=1.5))

        assert client.calls[0].kwargs["timeout"] == 1.5

    def test_retried_attempt_gets_the_time_left_when_it_starts(self):
        """Each attempt is bounded by the deadline as it stands then, not at the first."""
        # Given — a first attempt that fails while the time left falls from 10 s to 3 s
        client = _client(FakeOpenAIError(500), "answered")

        def remaining_ms() -> float:
            return 10_000.0 if not client.calls else 3_000.0

        # When
        with patch(_REMAINING_MS, autospec=True, side_effect=remaining_ms):
            _create(wrap(client))

        # Then
        assert [call.kwargs["timeout"] for call in client.calls] == [10.0, 3.0]

    def test_no_time_left_ends_the_call_before_any_endpoint(self):
        """A spent deadline starts no endpoint: the call ends as ``deadline``, nothing sent."""
        primary_client = _client()
        fallback_client = _client()
        primary, primary_name = _named(primary_client, "primary")

        with deadline_scope(0), pytest.raises(LLMUnavailableError) as raised:
            _create(wrap(primary, fallbacks=[fallback_client]))

        assert raised.value.attempts == ((primary_name, "deadline"),)
        assert raised.value.__cause__ is None
        assert (primary_client.calls, fallback_client.calls) == ([], [])

    def test_call_outside_a_deadline_gets_no_timeout(self):
        """Outside a recovery pass nothing changes: the SDK's own timeout stands."""
        client = _client()

        _create(wrap(client, timeout=1.5))

        assert "timeout" not in client.calls[0].kwargs
        assert client.calls[0].client_timeout == 1.5

    def test_method_without_a_timeout_parameter_gets_none(self):
        """A method that takes no ``timeout`` (Gen AI's) is called as it was."""
        client = FakeOtherSdkClient(answers=["answered"])

        with deadline_scope(10_000):
            wrap(client).messages.create(model="claude-sonnet")

        assert client.calls[0].kwargs == {"model": "claude-sonnet"}

    def test_async_call_inside_a_deadline_gets_the_time_left(self):
        """The deadline rides the context into the async call."""
        client = _client(cls=FakeAsyncLLMClient)

        with deadline_scope(10_000):
            bound = get_remaining_ms() / 1000.0
            with patch(_ASYNC_SLEEP, new_callable=AsyncMock):
                asyncio.run(_create(wrap(client)))

        assert 0 < client.calls[0].kwargs["timeout"] <= bound
