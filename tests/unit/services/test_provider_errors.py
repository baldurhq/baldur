"""Provider error classification: what an LLM SDK's exception says the provider answered.

Target: ``baldur.services.retry_handler.provider_errors`` — ``classify_provider_error``
and its two helpers, read by the retry, breaker, fallback and wait stages.

The decision table runs twice: over the installed SDKs' own exception classes
(``pytest.importorskip`` per row), and over SDK-free stand-ins whose class
claims an SDK module, so the provenance rule — "the module the class is defined
in" — is what decides, with or without the SDKs. The negatives (``not_provider``)
pin that every other library keeps today's behavior, through ``protect`` too.

UNIT_TEST_GUIDELINES.md:
- Contract: the category vocabulary, the verdict's fields and the parked
  failure-type label are spec values, hardcoded (§0.1).
- Behavior: the status table is an input → output mapping (§2.1), asserted as
  written; attempt counts are read from the retry settings (§1.2).
- No ``time.sleep`` (§6.3): the retry ladder's sleeper is patched out.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Iterator
from http import HTTPStatus
from typing import Any
from unittest.mock import patch

import pytest

from baldur.protect_facade import protect_with_meta
from baldur.services.circuit_breaker import get_circuit_breaker_service
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.provider_errors import (
    ProviderErrorCategory,
    ProviderVerdict,
    classify_provider_error,
    is_invalid_request,
    is_non_retryable_provider_error,
)
from baldur.services.retry_handler.rate_limit_detection import detect_rate_limit
from baldur.services.retry_handler.sinks import retry_exhausted_failure_type
from baldur.settings.protect import reset_protect_settings
from baldur.utils.retry_after import parse_retry_after
from tests.factories.llm_doubles import (
    FakeAnthropicError,
    FakeApiCoreError,
    FakeGenaiError,
    FakeOpenAIError,
    anthropic_status_error,
    gemini_error_body,
    genai_api_error,
    openai_connection_error,
    openai_status_error,
    openai_timeout_error,
)
from tests.factories.time_helpers import freeze_time

_SYNC_RETRY_SLEEP = "baldur.services.retry_handler.policy._DEFAULT_SLEEPER"

RATE_LIMITED = ProviderErrorCategory.RATE_LIMITED
OVERLOADED = ProviderErrorCategory.OVERLOADED
QUOTA = ProviderErrorCategory.QUOTA_EXHAUSTED
AUTH = ProviderErrorCategory.AUTH_FAILED
INVALID = ProviderErrorCategory.INVALID_REQUEST
TRANSIENT = ProviderErrorCategory.TRANSIENT

# Gemini's quota ids: a daily one cannot be waited out, a per-minute one can.
_DAILY_QUOTA_ID = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
_MINUTE_QUOTA_ID = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"


@pytest.fixture
def no_retry_sleep() -> Iterator[None]:
    """Fresh protect caches, and a retry ladder that does not sleep."""
    reset_protect_settings()
    with patch(_SYNC_RETRY_SLEEP, lambda _seconds: None):
        yield
    reset_protect_settings()


def _unique_name(prefix: str) -> str:
    return f"{prefix}.{uuid.uuid4().hex[:10]}"


# =============================================================================
# Contract
# =============================================================================


class TestProviderErrorVocabularyContract:
    """The words the classifier answers in, and the label a parked terminal gets."""

    def test_category_values_contract(self):
        """Six categories, each serialized as its documented value."""
        assert {member.name: member.value for member in ProviderErrorCategory} == {
            "RATE_LIMITED": "rate_limited",
            "OVERLOADED": "overloaded",
            "QUOTA_EXHAUSTED": "quota_exhausted",
            "AUTH_FAILED": "auth_failed",
            "INVALID_REQUEST": "invalid_request",
            "TRANSIENT": "transient",
        }

    def test_category_formats_as_its_value_contract(self):
        """A StrEnum: an f-string or a log field reads the value, not ``Cls.MEMBER``."""
        assert f"{ProviderErrorCategory.OVERLOADED}" == "overloaded"

    def test_verdict_fields_contract(self):
        """The verdict carries category, status, retry_after and provider, in that order."""
        assert [f.name for f in dataclasses.fields(ProviderVerdict)] == [
            "category",
            "status",
            "retry_after",
            "provider",
        ]

    def test_verdict_is_frozen_contract(self):
        """A verdict cannot be changed after it is made."""
        verdict = ProviderVerdict(
            category=TRANSIENT, status=None, retry_after=None, provider="openai"
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            verdict.status = 500  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("error", "provider"),
        [
            (FakeOpenAIError(500), "openai"),
            (FakeAnthropicError(500), "anthropic"),
            (FakeGenaiError(500), "google-genai"),
        ],
        ids=["openai", "anthropic", "google_genai"],
    )
    def test_provider_names_contract(self, error, provider):
        """Each SDK is named as documented on the verdict."""
        assert classify_provider_error(error).provider == provider

    @pytest.mark.parametrize(
        ("type_name", "label"),
        [
            ("LLMUnavailableError", "MAX_RETRIES_LLMUNAVAILABLEERROR"),
            ("TimeoutError", "MAX_RETRIES_TIMEOUTERROR"),
        ],
        ids=["llm_unavailable", "builtin"],
    )
    def test_retry_exhausted_failure_type_label_contract(self, type_name, label):
        """A terminal failure is parked as ``MAX_RETRIES_<TYPE>``, upper-cased."""
        assert retry_exhausted_failure_type(type_name) == label


# =============================================================================
# Behavior — the decision table
# =============================================================================


def _row(sdk: str, build: Callable[[], Exception], category, status) -> Any:
    return (sdk, build, category, status)


_REAL_SDK_ROWS = [
    # OpenAI SDK
    _row("openai", lambda: openai_status_error(429), RATE_LIMITED, 429),
    _row(
        "openai",
        lambda: openai_status_error(
            429,
            body={"code": "insufficient_quota", "type": "insufficient_quota"},
        ),
        QUOTA,
        429,
    ),
    _row("openai", lambda: openai_status_error(402), QUOTA, 402),
    _row("openai", lambda: openai_status_error(401), AUTH, 401),
    _row("openai", lambda: openai_status_error(403), AUTH, 403),
    _row("openai", lambda: openai_status_error(400), INVALID, 400),
    _row("openai", lambda: openai_status_error(404), INVALID, 404),
    _row("openai", lambda: openai_status_error(413), INVALID, 413),
    _row("openai", lambda: openai_status_error(422), INVALID, 422),
    _row("openai", lambda: openai_status_error(408), TRANSIENT, 408),
    _row("openai", lambda: openai_status_error(409), TRANSIENT, 409),
    _row("openai", lambda: openai_status_error(500), TRANSIENT, 500),
    _row("openai", lambda: openai_status_error(502), TRANSIENT, 502),
    _row("openai", lambda: openai_status_error(503), OVERLOADED, 503),
    _row("openai", lambda: openai_status_error(504), TRANSIENT, 504),
    _row("openai", lambda: openai_status_error(200), TRANSIENT, 200),
    _row("openai", openai_connection_error, TRANSIENT, None),
    _row("openai", openai_timeout_error, TRANSIENT, None),
    # Anthropic SDK
    _row("anthropic", lambda: anthropic_status_error(429), RATE_LIMITED, 429),
    _row("anthropic", lambda: anthropic_status_error(529), OVERLOADED, 529),
    _row(
        "anthropic",
        lambda: anthropic_status_error(
            400, message="Your credit balance is too low to access the API."
        ),
        QUOTA,
        400,
    ),
    _row("anthropic", lambda: anthropic_status_error(400), INVALID, 400),
    _row("anthropic", lambda: anthropic_status_error(413), INVALID, 413),
    _row("anthropic", lambda: anthropic_status_error(401), AUTH, 401),
    _row("anthropic", lambda: anthropic_status_error(500), TRANSIENT, 500),
    # Google Gen AI SDK
    _row(
        "google.genai",
        lambda: genai_api_error(429, gemini_error_body(429, retry_delay="45s")),
        RATE_LIMITED,
        429,
    ),
    _row(
        "google.genai",
        lambda: genai_api_error(
            429, gemini_error_body(429, retry_delay="45s", quota_id=_DAILY_QUOTA_ID)
        ),
        QUOTA,
        429,
    ),
    _row("google.genai", lambda: genai_api_error(503), OVERLOADED, 503),
    _row("google.genai", lambda: genai_api_error(403), AUTH, 403),
    _row("google.genai", lambda: genai_api_error(400), INVALID, 400),
    _row("google.genai", lambda: genai_api_error(500), TRANSIENT, 500),
]

_REAL_SDK_IDS = [
    "openai_429",
    "openai_insufficient_quota",
    "openai_402",
    "openai_401",
    "openai_403",
    "openai_400",
    "openai_404",
    "openai_413",
    "openai_422",
    "openai_408",
    "openai_409",
    "openai_500",
    "openai_502",
    "openai_503",
    "openai_504",
    "openai_status_200",
    "openai_connection",
    "openai_timeout",
    "anthropic_429",
    "anthropic_529",
    "anthropic_credit_balance",
    "anthropic_400",
    "anthropic_413",
    "anthropic_401",
    "anthropic_500",
    "genai_429_retry_delay",
    "genai_429_per_day_quota",
    "genai_503",
    "genai_403",
    "genai_400",
    "genai_500",
]


class TestClassifyProviderErrorBehavior:
    """``classify_provider_error`` reads the SDK's status, code and body; first row wins."""

    @pytest.mark.parametrize(
        ("sdk", "build", "category", "status"), _REAL_SDK_ROWS, ids=_REAL_SDK_IDS
    )
    def test_real_sdk_exception_classifies_per_table(
        self, sdk, build, category, status
    ):
        """Each SDK's own exception gets the table's category and its own status."""
        pytest.importorskip(sdk)
        verdict = classify_provider_error(build())

        assert verdict is not None
        assert (verdict.category, verdict.status) == (category, status)

    @pytest.mark.parametrize(
        ("status", "category"),
        [
            (399, TRANSIENT),
            (400, INVALID),
            (401, AUTH),
            (402, QUOTA),
            (403, AUTH),
            (404, INVALID),
            (408, TRANSIENT),
            (409, TRANSIENT),
            (429, RATE_LIMITED),
            (499, INVALID),
            (500, TRANSIENT),
            (503, OVERLOADED),
            (529, OVERLOADED),
            (599, TRANSIENT),
            (600, TRANSIENT),
        ],
        ids=lambda value: str(value),
    )
    def test_status_ranges_classify_at_their_boundaries(self, status, category):
        """The 4xx/5xx ranges end where the table says; outside them is transient."""
        assert classify_provider_error(FakeOpenAIError(status)).category is category

    @pytest.mark.parametrize(
        ("error", "category"),
        [
            (FakeOpenAIError(429, code="insufficient_quota"), QUOTA),
            (FakeOpenAIError(429, error_type="insufficient_quota"), QUOTA),
            (FakeOpenAIError(429, code="rate_limit_exceeded"), RATE_LIMITED),
            (FakeAnthropicError(529), OVERLOADED),
            (FakeAnthropicError(400, message="Credit Balance exhausted"), QUOTA),
            (
                FakeGenaiError(429, gemini_error_body(429, quota_id=_DAILY_QUOTA_ID)),
                QUOTA,
            ),
            (
                FakeGenaiError(429, gemini_error_body(429, quota_id=_MINUTE_QUOTA_ID)),
                RATE_LIMITED,
            ),
            (
                FakeGenaiError(
                    429,
                    {
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                "violations": [{"quotaId": _DAILY_QUOTA_ID}],
                            }
                        ]
                    },
                ),
                QUOTA,
            ),
        ],
        ids=[
            "openai_code_quota",
            "openai_type_quota",
            "openai_other_429_code",
            "anthropic_529",
            "anthropic_credit_balance_any_case",
            "genai_daily_quota",
            "genai_minute_quota",
            "genai_top_level_details",
        ],
    )
    def test_sdk_free_stand_in_classifies_by_its_module(self, error, category):
        """The rules run without the SDKs: the claimed module is what decides."""
        assert classify_provider_error(error).category is category

    def test_status_code_is_read_before_an_integer_code(self):
        """``status_code`` (OpenAI, Anthropic) wins over an integer ``code``."""
        error = FakeOpenAIError(503)
        error.code = 400  # type: ignore[assignment]

        assert classify_provider_error(error).status == 503

    def test_genai_integer_code_is_the_status(self):
        """A Gen AI error has no ``status_code``; its integer ``code`` is the status."""
        assert classify_provider_error(FakeGenaiError(503)).status == 503

    def test_boolean_status_code_is_no_status(self):
        """``True`` is an int to Python, but no HTTP status: the answer never arrived."""
        verdict = classify_provider_error(FakeOpenAIError(True))  # type: ignore[arg-type]

        assert (verdict.status, verdict.category) == (None, TRANSIENT)

    @pytest.mark.parametrize(
        "module",
        ["openai._exceptions", "anthropic._exceptions", "google.genai.errors"],
        ids=["openai", "anthropic", "google_genai"],
    )
    def test_sdk_exception_outside_its_api_error_family_gets_no_verdict(self, module):
        """An SDK's argument check or finish-reason check carries no provider answer."""
        error_class = type(
            "LengthFinishReasonError", (Exception,), {"__module__": module}
        )

        assert classify_provider_error(error_class("stopped at max_tokens")) is None

    def test_real_genai_argument_check_gets_no_verdict(self):
        """Gen AI refuses an async function on a sync client before any request."""
        errors = pytest.importorskip("google.genai.errors")

        error = errors.UnsupportedFunctionError("async function on a sync client")

        assert classify_provider_error(error) is None

    def test_wrapper_library_subclass_is_recognized_through_its_mro(self):
        """A library subclassing the SDK's error (LiteLLM does) is still the SDK's answer."""
        openai = pytest.importorskip("openai")

        class WrapperRateLimitError(openai.RateLimitError):
            pass

        source = openai_status_error(429)
        error = WrapperRateLimitError(
            "limited", response=source.response, body=source.body
        )
        verdict = classify_provider_error(error)

        assert (verdict.provider, verdict.category) == ("openai", RATE_LIMITED)

    def test_attribute_fault_returns_no_verdict(self):
        """Total: an SDK exception whose attributes cannot be read gets no verdict."""

        class ExplodingError(Exception):
            @property
            def status_code(self):
                raise RuntimeError("attribute read failed")

        ExplodingError.__module__ = "openai._exceptions"

        assert classify_provider_error(ExplodingError("x")) is None

    @pytest.mark.parametrize(
        "subject",
        [None, "429 Too Many Requests", {"status_code": 429}, 429],
        ids=["none", "string", "dict", "int"],
    )
    def test_non_exception_returns_no_verdict(self, subject):
        """Only exceptions carry a provider's answer."""
        assert classify_provider_error(subject) is None

    @pytest.mark.parametrize(
        ("error", "non_retryable", "invalid"),
        [
            (FakeOpenAIError(402), True, False),
            (FakeOpenAIError(401), True, False),
            (FakeOpenAIError(400), True, True),
            (FakeOpenAIError(429), False, False),
            (FakeAnthropicError(529), False, False),
            (FakeOpenAIError(500), False, False),
        ],
        ids=["quota", "auth", "invalid", "rate_limited", "overloaded", "transient"],
    )
    def test_helpers_follow_the_category(self, error, non_retryable, invalid):
        """Quota, auth and invalid are not retried; only invalid is no failure at all."""
        assert is_non_retryable_provider_error(error) is non_retryable
        assert is_invalid_request(error) is invalid


def _httpx_status_error(status: int) -> Exception:
    import httpx

    request = httpx.Request("POST", "https://api.example.com/v1/things")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


def _requests_http_error(status: int) -> Exception:
    import requests

    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Client Error", response=response)


_NOT_PROVIDER_ROWS = [
    ("google.api_core 429", lambda: FakeApiCoreError(HTTPStatus.TOO_MANY_REQUESTS)),
    ("google.api_core 400", lambda: FakeApiCoreError(HTTPStatus.BAD_REQUEST)),
    ("httpx 400", lambda: _httpx_status_error(400)),
    ("requests 400", lambda: _requests_http_error(400)),
    ("builtin ConnectionError", lambda: ConnectionError("refused")),
    ("ValueError", lambda: ValueError("bad input")),
]


class TestNotProviderErrorBehavior:
    """Every other library keeps today's behavior: no verdict, retried, counted, fallen back."""

    @pytest.mark.parametrize(
        ("label", "build"), _NOT_PROVIDER_ROWS, ids=[r[0] for r in _NOT_PROVIDER_ROWS]
    )
    def test_not_provider_exception_gets_no_verdict(self, label, build):
        """Not raised by a recognized SDK → no verdict, so no helper claims it."""
        error = build()

        assert classify_provider_error(error) is None
        assert is_non_retryable_provider_error(error) is False
        assert is_invalid_request(error) is False

    @pytest.mark.parametrize(
        ("label", "build"),
        [_NOT_PROVIDER_ROWS[1], _NOT_PROVIDER_ROWS[2]],
        ids=["google_api_core_400", "httpx_400"],
    )
    def test_not_provider_400_under_protect_retries_counts_and_falls_back(
        self, label, build, no_retry_sleep
    ):
        """A 400 from another library is retried to the cap, counted, and served by ``f``."""
        # Given
        name = _unique_name("svc.not_provider")
        calls = {"n": 0}
        error = build()

        def call() -> str:
            calls["n"] += 1
            raise error

        # When
        result = protect_with_meta(name, call, retry=True, fallback=lambda: "served")

        # Then
        expected_attempts = RetryPolicyConfig.from_settings(domain=name).max_attempts
        assert calls["n"] == expected_attempts
        assert result.attempts == expected_attempts
        assert result.metadata["reason"] == "max_attempts"
        assert (result.fallback_used, result.value) == (True, "served")
        state = get_circuit_breaker_service().get_or_create_state(name)
        assert state.failure_count == 1


# =============================================================================
# Behavior — the provider's stated wait
# =============================================================================


class TestProviderRetryHintBehavior:
    """The wait: ``retry-after-ms``, then ``Retry-After``, then Gemini's ``retryDelay``."""

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),
            ({"retry-after": "9"}, 9.0),
            ({"retry-after-ms": "abc", "retry-after": "7"}, 7.0),
            ({"retry-after-ms": "nan", "retry-after": "7"}, 7.0),
            ({"retry-after-ms": "-5", "retry-after": "7"}, 7.0),
            ({"retry-after-ms": "inf", "retry-after": "7"}, 7.0),
            ({"retry-after-ms": "", "retry-after": "7"}, 7.0),
            ({"retry-after-ms": "abc", "retry-after": "-1"}, None),
            ({"retry-after": "Infinity"}, None),
            ({}, None),
        ],
        ids=[
            "ms_before_seconds",
            "seconds_alone",
            "unparseable_ms_falls_through",
            "nan_ms_falls_through",
            "negative_ms_falls_through",
            "infinite_ms_falls_through",
            "empty_ms_falls_through",
            "both_unusable",
            "infinite_seconds",
            "no_headers",
        ],
    )
    def test_header_hint_precedence_and_validation(self, headers, expected):
        """``retry-after-ms`` first (÷1000), else ``Retry-After``; junk is no hint."""
        verdict = classify_provider_error(FakeOpenAIError(429, headers=headers))

        assert verdict.retry_after == expected

    def test_real_openai_retry_after_ms_header_is_read(self):
        """The OpenAI SDK's response headers carry the millisecond hint."""
        pytest.importorskip("openai")
        error = openai_status_error(429, headers={"retry-after-ms": "250"})

        assert classify_provider_error(error).retry_after == 0.25

    def test_http_date_retry_after_is_seconds_until_that_date(self):
        """The HTTP-date form is the remaining time, as the canonical parser reads it."""
        header = "Fri, 02 Oct 2026 12:00:30 GMT"
        with freeze_time("2026-10-02 12:00:00"):
            verdict = classify_provider_error(
                FakeOpenAIError(429, headers={"retry-after": header})
            )
            expected = parse_retry_after(header)

        assert verdict.retry_after == expected
        assert expected == 30.0

    def test_past_http_date_retry_after_is_no_hint(self):
        """A date already gone asks for no wait."""
        header = "Fri, 02 Oct 2026 11:59:00 GMT"
        with freeze_time("2026-10-02 12:00:00"):
            verdict = classify_provider_error(
                FakeOpenAIError(429, headers={"retry-after": header})
            )

        assert verdict.retry_after is None

    @pytest.mark.parametrize(
        ("retry_delay", "expected"),
        [("45s", 45.0), ("45.47s", 45.47), ("45", 45.0), ("-3s", None), ("soon", None)],
        ids=["seconds", "fractional", "no_unit", "negative", "unparseable"],
    )
    def test_gemini_body_retry_delay_is_read_in_seconds(self, retry_delay, expected):
        """Gemini states its wait in the body's ``RetryInfo``, not a header."""
        error = FakeGenaiError(429, gemini_error_body(429, retry_delay=retry_delay))

        assert classify_provider_error(error).retry_after == expected

    def test_gemini_non_string_retry_delay_is_no_hint(self):
        """Only the documented ``"<n>s"`` string form is a hint."""
        body = gemini_error_body(429)
        body["error"]["details"].append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": 45}
        )

        assert classify_provider_error(FakeGenaiError(429, body)).retry_after is None

    def test_header_hint_is_read_before_the_gemini_body(self):
        """A response header, when one exists, outranks the body's delay."""
        error = FakeGenaiError(429, gemini_error_body(429, retry_delay="45s"))
        error.response = FakeOpenAIError(429, headers={"retry-after": "3"}).response

        assert classify_provider_error(error).retry_after == 3.0

    def test_real_genai_retry_delay_is_read(self):
        """The Gen AI SDK's own exception keeps the body the delay is read from."""
        pytest.importorskip("google.genai")
        error = genai_api_error(429, gemini_error_body(429, retry_delay="45s"))

        assert classify_provider_error(error).retry_after == 45.0

    def test_daily_quota_with_a_body_delay_installs_no_wait(self):
        """A per-day quota is not saved by waiting, whatever delay the body names."""
        error = FakeGenaiError(
            429,
            gemini_error_body(429, retry_delay="45s", quota_id=_DAILY_QUOTA_ID),
        )

        assert classify_provider_error(error).category is QUOTA
        assert detect_rate_limit(error) == (False, None)
