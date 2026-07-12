"""
Unit tests for PrometheusQueryClient — remote Prometheus HTTP query client.

Test classification (UNIT_TEST_GUIDELINES §0):
- Contract: PrometheusQueryError.extra_context field set; the mounted retry
  policy (transient-5xx-only, connect/read not retried) from the design spec.
- Behavior: response parsing, error mapping, config resolution, and the exact
  request arguments forwarded to the session — computed against source behavior.

The retry policy lives in the mounted urllib3 HTTPAdapter (below requests), so
it is verified by asserting the built session's Retry configuration rather than
by mocking the injected Session (which bypasses the adapter entirely).

Target: baldur.adapters.metrics.prometheus_query_client
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, create_autospec

import pytest
import requests

from baldur.adapters.metrics.prometheus_query_client import (
    PrometheusQueryClient,
    PrometheusQueryError,
)
from baldur.core.exceptions import BaldurError
from baldur.settings.prometheus import PrometheusSettings

_BASE_URL = "http://prometheus:9090"


def _response(
    *,
    status_code: int = 200,
    json_body: dict | None = None,
    json_raises: bool = False,
) -> Mock:
    """Build a requests.Response test double with a configured json()."""
    resp = Mock(spec=requests.Response)
    resp.status_code = status_code
    if json_raises:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body or {
            "status": "success",
            "data": {"result": []},
        }
    return resp


def _vector(value: float, metric: dict | None = None) -> dict:
    """A Prometheus instant-vector success body carrying a single scalar."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": metric or {}, "value": [1_700_000_000, str(value)]}],
        },
    }


def _client_with_session(session: Mock, **overrides) -> PrometheusQueryClient:
    """Client wired to an injected (mocked) Session — no real network."""
    params = {
        "url": _BASE_URL,
        "headers": {},
        "timeout_seconds": 5.0,
        "verify": True,
        "session": session,
    }
    params.update(overrides)
    return PrometheusQueryClient(**params)


class TestPrometheusQueryErrorContract:
    """PrometheusQueryError context/inheritance contract."""

    def test_is_baldur_error_subclass(self):
        assert issubclass(PrometheusQueryError, BaldurError)

    def test_extra_context_includes_query_and_status_code(self):
        err = PrometheusQueryError("boom", query="up", status_code=503)
        ctx = err.extra_context()
        assert ctx["query"] == "up"
        assert ctx["status_code"] == 503

    def test_extra_context_omits_unset_fields(self):
        """Empty query and unset status_code are not added to the context."""
        err = PrometheusQueryError("boom")
        ctx = err.extra_context()
        assert "query" not in ctx
        assert "status_code" not in ctx


class TestPrometheusQueryClientParseBehavior:
    """query / query_range successful response parsing."""

    def test_query_returns_result_vector(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(json_body=_vector(0.25))
        client = _client_with_session(session)

        result = client.query("up")

        assert result == [{"metric": {}, "value": [1_700_000_000, "0.25"]}]

    def test_query_range_returns_result_matrix(self):
        matrix_body = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"metric": {}, "values": [[1_700_000_000, "1.0"]]}],
            },
        }
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(json_body=matrix_body)
        client = _client_with_session(session)

        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
        result = client.query_range("rate(x[5m])", start, end, 60)

        assert result == [{"metric": {}, "values": [[1_700_000_000, "1.0"]]}]

    def test_empty_result_returns_empty_list(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(
            json_body={"status": "success", "data": {"result": []}}
        )
        client = _client_with_session(session)

        assert client.query("up") == []

    def test_missing_data_key_returns_empty_list(self):
        """A success body with no data.result yields an empty list, not KeyError."""
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(json_body={"status": "success"})
        client = _client_with_session(session)

        assert client.query("up") == []


class TestPrometheusQueryClientErrorMappingBehavior:
    """Transport / HTTP / body errors map to PrometheusQueryError."""

    def test_connection_error_wrapped_and_chained(self):
        session = create_autospec(requests.Session, instance=True)
        cause = requests.ConnectionError("refused")
        session.get.side_effect = cause
        client = _client_with_session(session)

        with pytest.raises(PrometheusQueryError) as exc_info:
            client.query("up")

        assert exc_info.value.query == "up"
        assert exc_info.value.__cause__ is cause

    def test_timeout_wrapped(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.side_effect = requests.Timeout("slow")
        client = _client_with_session(session)

        with pytest.raises(PrometheusQueryError):
            client.query("up")

    def test_non_200_status_wrapped_with_status_code(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(status_code=500)
        client = _client_with_session(session)

        with pytest.raises(PrometheusQueryError) as exc_info:
            client.query("up")

        assert exc_info.value.status_code == 500

    def test_invalid_json_body_wrapped(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(json_raises=True)
        client = _client_with_session(session)

        with pytest.raises(PrometheusQueryError):
            client.query("up")

    def test_status_not_success_wrapped_with_error_message(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(
            json_body={"status": "error", "error": "parse error"}
        )
        client = _client_with_session(session)

        with pytest.raises(PrometheusQueryError) as exc_info:
            client.query("bad{query")

        assert "parse error" in str(exc_info.value)


class TestPrometheusQueryClientRequestArgsBehavior:
    """The exact request the client issues (dependency-interaction)."""

    def test_instant_query_forwards_params_and_transport_options(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(json_body=_vector(1.0))
        client = _client_with_session(
            session,
            headers={"Authorization": "Bearer x"},
            timeout_seconds=3.0,
            verify=True,
        )

        client.query("up")

        session.get.assert_called_once_with(
            f"{_BASE_URL}/api/v1/query",
            params={"query": "up"},
            timeout=3.0,
            headers={"Authorization": "Bearer x"},
            verify=True,
        )

    def test_instant_query_with_time_adds_time_param(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(json_body=_vector(1.0))
        client = _client_with_session(session)
        at = datetime(2026, 1, 1, tzinfo=UTC)

        client.query("up", at=at)

        _, kwargs = session.get.call_args
        assert kwargs["params"] == {"query": "up", "time": at.timestamp()}

    def test_range_query_forwards_start_end_step(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(
            json_body={"status": "success", "data": {"result": []}}
        )
        client = _client_with_session(session)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)

        client.query_range("rate(x[5m])", start, end, 60)

        _, kwargs = session.get.call_args
        assert kwargs["params"] == {
            "query": "rate(x[5m])",
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": 60,
        }

    def test_empty_headers_forwarded_as_none(self):
        """No configured headers → headers=None (not an empty dict)."""
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(json_body=_vector(1.0))
        client = _client_with_session(session, headers={})

        client.query("up")

        _, kwargs = session.get.call_args
        assert kwargs["headers"] is None

    def test_trailing_slash_stripped_from_base_url(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(json_body=_vector(1.0))
        client = _client_with_session(session, url="http://prometheus:9090/")

        client.query("up")

        called_url = session.get.call_args[0][0]
        assert called_url == "http://prometheus:9090/api/v1/query"


class TestPrometheusQueryClientConfigResolutionBehavior:
    """Settings resolution, per-field overrides, verify/headers derivation."""

    def test_settings_url_used_when_no_override(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(json_body=_vector(1.0))
        settings = PrometheusSettings(url="http://from-settings:9090")
        client = PrometheusQueryClient(settings=settings, session=session)

        client.query("up")

        called_url = session.get.call_args[0][0]
        assert called_url.startswith("http://from-settings:9090")

    def test_ctor_url_overrides_settings(self):
        session = create_autospec(requests.Session, instance=True)
        session.get.return_value = _response(json_body=_vector(1.0))
        settings = PrometheusSettings(url="http://from-settings:9090")
        client = PrometheusQueryClient(
            url="http://override:9090", settings=settings, session=session
        )

        client.query("up")

        called_url = session.get.call_args[0][0]
        assert called_url.startswith("http://override:9090")

    def test_resolve_headers_unwraps_secret_str(self):
        settings = PrometheusSettings(headers={"Authorization": "Bearer secret"})
        headers = PrometheusQueryClient._resolve_headers(settings)
        assert headers == {"Authorization": "Bearer secret"}

    def test_resolve_verify_false_when_tls_verify_off(self):
        settings = PrometheusSettings(tls_verify=False)
        assert PrometheusQueryClient._resolve_verify(settings) is False

    def test_resolve_verify_uses_ca_cert_path_when_set(self):
        settings = PrometheusSettings(tls_verify=True, tls_ca_cert="/etc/ca.pem")
        assert PrometheusQueryClient._resolve_verify(settings) == "/etc/ca.pem"

    def test_resolve_verify_true_when_no_ca_cert(self):
        settings = PrometheusSettings(tls_verify=True, tls_ca_cert="")
        assert PrometheusQueryClient._resolve_verify(settings) is True


class TestPrometheusQueryClientRetryContract:
    """The mounted urllib3 Retry policy encodes the transient-5xx-only contract.

    Real network I/O is not exercised; the client's job is to *configure* the
    adapter so a transient 5xx is retried while a connection/read failure is
    not. That configuration is the design contract asserted here.
    """

    def test_status_forcelist_is_transient_5xx_only(self):
        client = PrometheusQueryClient(url=_BASE_URL)
        retry = client._session.get_adapter("https://x").max_retries
        assert set(retry.status_forcelist) == {502, 503, 504}

    def test_retry_after_header_does_not_widen_retry_set_beyond_5xx(self):
        """A 413/429 carrying Retry-After must NOT be retried.

        urllib3's respect_retry_after_header defaults to True, which would make
        is_retry() return True for the RETRY_AFTER_STATUS_CODES {413, 429, 503}
        whenever a Retry-After header is present — regardless of status_forcelist
        — widening the retry set beyond transient 5xx and letting a rate-limiting
        backend multiply the per-call timeout via the server-dictated sleep. The
        client pins it False so only the status_forcelist 5xx are retried.
        """
        client = PrometheusQueryClient(url=_BASE_URL, retry_total=1)
        retry = client._session.get_adapter("https://x").max_retries
        assert retry.respect_retry_after_header is False
        assert retry.is_retry("GET", 429, has_retry_after=True) is False
        assert retry.is_retry("GET", 413, has_retry_after=True) is False
        # The status_forcelist 5xx are still retried.
        assert retry.is_retry("GET", 503, has_retry_after=False) is True

    def test_connect_and_read_are_not_retried(self):
        """connect/read pinned to 0 — a down/slow Prometheus waits for next gate."""
        client = PrometheusQueryClient(url=_BASE_URL, retry_total=2)
        retry = client._session.get_adapter("https://x").max_retries
        assert retry.connect == 0
        assert retry.read == 0

    def test_status_budget_follows_retry_total(self):
        client = PrometheusQueryClient(url=_BASE_URL, retry_total=3)
        retry = client._session.get_adapter("https://x").max_retries
        assert retry.total == 3
        assert retry.status == 3

    def test_backoff_factor_forwarded(self):
        client = PrometheusQueryClient(url=_BASE_URL, retry_backoff_factor=0.25)
        retry = client._session.get_adapter("https://x").max_retries
        assert retry.backoff_factor == pytest.approx(0.25)

    def test_only_get_is_retried(self):
        client = PrometheusQueryClient(url=_BASE_URL)
        retry = client._session.get_adapter("https://x").max_retries
        assert set(retry.allowed_methods) == {"GET"}

    def test_retry_mounted_on_both_http_and_https(self):
        client = PrometheusQueryClient(url=_BASE_URL)
        http_retry = client._session.get_adapter("http://x").max_retries
        https_retry = client._session.get_adapter("https://x").max_retries
        assert set(http_retry.status_forcelist) == {502, 503, 504}
        assert set(https_retry.status_forcelist) == {502, 503, 504}
