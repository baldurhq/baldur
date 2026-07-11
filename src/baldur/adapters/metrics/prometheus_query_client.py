"""
Prometheus HTTP query client.

A shared, connection-pooled client for the Prometheus HTTP query API
(``GET /api/v1/query`` instant, ``GET /api/v1/query_range`` range). It holds a
persistent ``requests.Session`` so the sequential queries of one evaluation
reuse a single keep-alive connection instead of a fresh TCP+TLS handshake per
call, and sends operator-configured auth/tenancy headers and TLS options on
every request.

Distinct from ``baldur.adapters.prometheus_adapter``: that adapter reads the
*in-process* ``prometheus_client`` registry (no network); this client issues
*remote* PromQL over HTTP against a Prometheus server or a PromQL-compatible
backend (Grafana Mimir, VictoriaMetrics, Thanos, Grafana Cloud). Use this one
for range/instant queries against a metrics backend; use the adapter for local
counter reads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import requests
import structlog
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from baldur.core.exceptions import BaldurError
from baldur.settings.prometheus import PrometheusSettings, get_prometheus_settings

logger = structlog.get_logger()

# Protocol constants (exempt from settings routing): the retryable transient
# 5xx status codes and the HTTP verb allowlist. The query API is GET-only, and
# only a transient upstream 5xx (load-balancer drain, momentary overload) is
# worth retrying — a connection or read-timeout failure means Prometheus is
# down or slow and should wait for the next gate cycle, not be retried inline.
_RETRY_STATUS_FORCELIST = [502, 503, 504]
_RETRY_ALLOWED_METHODS = frozenset(["GET"])

_QUERY_PATH = "/api/v1/query"
_QUERY_RANGE_PATH = "/api/v1/query_range"

__all__ = ["PrometheusQueryClient", "PrometheusQueryError"]


class PrometheusQueryError(BaldurError):
    """Raised when a Prometheus HTTP query fails.

    Covers transport errors, non-2xx responses, and a JSON body whose
    ``status`` is not ``"success"``. Wraps the underlying transport exception
    via ``raise ... from``.
    """

    def __init__(
        self,
        message: str = "",
        *,
        query: str = "",
        status_code: int | None = None,
        code: str = "",
    ):
        super().__init__(message, code=code)
        self.query = query
        self.status_code = status_code

    def extra_context(self) -> dict[str, Any]:
        ctx = super().extra_context()
        if self.query:
            ctx["query"] = self.query
        if self.status_code is not None:
            ctx["status_code"] = self.status_code
        return ctx


class PrometheusQueryClient:
    """Connection-pooled client for the Prometheus HTTP query API.

    Config resolves from :class:`PrometheusSettings`; every field can be
    overridden per-instance, and a ``requests.Session`` may be injected as a
    test seam. The mounted ``HTTPAdapter`` carries a bounded transient-5xx
    retry (connection/read failures are not retried).
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        verify: bool | str | None = None,
        retry_total: int | None = None,
        retry_backoff_factor: float | None = None,
        settings: PrometheusSettings | None = None,
        session: requests.Session | None = None,
    ) -> None:
        settings = settings or get_prometheus_settings()

        base_url = settings.url if url is None else url
        self._base_url = base_url.rstrip("/")
        self._timeout = (
            settings.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self._headers = self._resolve_headers(settings) if headers is None else headers
        self._verify: bool | str = (
            self._resolve_verify(settings) if verify is None else verify
        )
        self._retry_total = settings.retry_total if retry_total is None else retry_total
        self._retry_backoff_factor = (
            settings.retry_backoff_factor
            if retry_backoff_factor is None
            else retry_backoff_factor
        )
        self._session = session or self._build_session()

    @staticmethod
    def _resolve_headers(settings: PrometheusSettings) -> dict[str, str]:
        """Unwrap SecretStr header values for transport."""
        return {k: v.get_secret_value() for k, v in settings.headers.items()}

    @staticmethod
    def _resolve_verify(settings: PrometheusSettings) -> bool | str:
        """TLS verify value: False to skip, a CA path, or True."""
        if not settings.tls_verify:
            return False
        if settings.tls_ca_cert:
            return settings.tls_ca_cert
        return True

    def _build_session(self) -> requests.Session:
        """Build a Session whose mounted adapter retries only transient 5xx.

        ``connect`` and ``read`` are pinned to 0 so connection and read-timeout
        failures raise immediately (they wait for the next gate rather than
        multiplying the per-call timeout); ``status`` allows up to
        ``retry_total`` retries of a 502/503/504.
        """
        retry = Retry(
            total=self._retry_total,
            connect=0,
            read=0,
            status=self._retry_total,
            backoff_factor=self._retry_backoff_factor,
            status_forcelist=_RETRY_STATUS_FORCELIST,
            allowed_methods=_RETRY_ALLOWED_METHODS,
        )
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=retry)
        # requests mounts only http(s) adapters, so a non-http scheme has no
        # adapter and raises InvalidSchema — the scheme guard is satisfied by
        # construction.
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def query(
        self,
        promql: str,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Run an instant query and return the ``data.result`` vector.

        Args:
            promql: The PromQL expression.
            at: Evaluation instant (UTC). Omitted → server evaluates at "now".

        Returns:
            The instant-vector result list (each item has ``metric`` + ``value``).
        """
        params: dict[str, Any] = {"query": promql}
        if at is not None:
            params["time"] = at.timestamp()
        return self._request(_QUERY_PATH, params, promql)

    def query_range(
        self,
        promql: str,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> list[dict[str, Any]]:
        """Run a range query and return the ``data.result`` matrix.

        Returns:
            The range-matrix result list (each item has ``metric`` + ``values``).
        """
        params: dict[str, Any] = {
            "query": promql,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step_seconds,
        }
        return self._request(_QUERY_RANGE_PATH, params, promql)

    def _request(
        self,
        path: str,
        params: dict[str, Any],
        promql: str,
    ) -> list[dict[str, Any]]:
        url = f"{self._base_url}{path}"
        try:
            response = self._session.get(
                url,
                params=params,
                timeout=self._timeout,
                headers=self._headers or None,
                verify=self._verify,
            )
        except requests.RequestException as exc:
            logger.warning(
                "prometheus_query_client.query_failed",
                query=promql,
                error=str(exc),
            )
            raise PrometheusQueryError(
                f"Prometheus request failed: {exc}",
                query=promql,
            ) from exc

        if response.status_code != 200:
            logger.warning(
                "prometheus_query_client.query_failed",
                query=promql,
                status_code=response.status_code,
            )
            raise PrometheusQueryError(
                f"Prometheus returned HTTP {response.status_code}",
                query=promql,
                status_code=response.status_code,
            )

        try:
            body = response.json()
        except ValueError as exc:
            logger.warning(
                "prometheus_query_client.query_failed",
                query=promql,
                error="invalid_json",
            )
            raise PrometheusQueryError(
                "Prometheus returned a non-JSON body",
                query=promql,
                status_code=response.status_code,
            ) from exc

        if body.get("status") != "success":
            error = body.get("error", "unknown error")
            logger.warning(
                "prometheus_query_client.query_failed",
                query=promql,
                error=error,
            )
            raise PrometheusQueryError(
                f"Prometheus query status not success: {error}",
                query=promql,
                status_code=response.status_code,
            )

        result = body.get("data", {}).get("result", [])
        logger.debug(
            "prometheus_query_client.query_succeeded",
            query=promql,
            series=len(result),
        )
        return result
