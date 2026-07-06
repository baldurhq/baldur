"""
OpenTelemetry Collector redaction processor integration tests.

Verifies that the redaction processor masks sensitive data correctly.
- Value-pattern-based masking (internal IPs, server paths, JWT tokens, etc.)
- As of v0.96.0: only the traces pipeline is supported
"""

import os
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requires_otel

# tests/integration/otel/ -> repo root is 4 levels up
_COLLECTOR_CONFIG_PATH = (
    Path(__file__).resolve().parents[4]
    / "examples"
    / "monitoring"
    / "otel-collector.yml"
)


@pytest.fixture(scope="module")
def collector_config():
    """Load otel-collector.yml."""
    with open(_COLLECTOR_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestRedactionProcessorConfiguration:
    """Redaction processor configuration checks."""

    def test_redaction_processor_exists(self, collector_config):
        """The redaction processor is present in the configuration."""
        processors = collector_config.get("processors", {})
        assert "redaction" in processors, "redaction processor missing from config"

    def test_allow_all_keys_enabled(self, collector_config):
        """allow_all_keys is set to true."""
        redaction = collector_config["processors"]["redaction"]
        assert redaction.get("allow_all_keys") is True, (
            "allow_all_keys must be true (mask values instead of dropping keys)"
        )

    def test_blocked_values_contains_internal_ip_patterns(self, collector_config):
        """Internal IP patterns are listed in blocked_values."""
        redaction = collector_config["processors"]["redaction"]
        blocked_values = redaction.get("blocked_values", [])

        # Check RFC 1918 private IP patterns
        ip_patterns_found = {
            "10.x.x.x": False,
            "192.168.x.x": False,
            "172.16-31.x.x": False,
        }

        for pattern in blocked_values:
            if "10\\." in pattern:
                ip_patterns_found["10.x.x.x"] = True
            if "192\\.168\\." in pattern:
                ip_patterns_found["192.168.x.x"] = True
            if "172\\." in pattern:
                ip_patterns_found["172.16-31.x.x"] = True

        for ip_range, found in ip_patterns_found.items():
            assert found, f"{ip_range} pattern missing from blocked_values"

    def test_blocked_values_contains_server_path_patterns(self, collector_config):
        """Server path patterns are listed in blocked_values."""
        redaction = collector_config["processors"]["redaction"]
        blocked_values = redaction.get("blocked_values", [])

        path_patterns_found = {"/home/": False, "/var/": False, "C:\\\\": False}

        for pattern in blocked_values:
            if "/home/" in pattern:
                path_patterns_found["/home/"] = True
            if "/var/" in pattern:
                path_patterns_found["/var/"] = True
            if "C:\\\\" in pattern:
                path_patterns_found["C:\\\\"] = True

        for path, found in path_patterns_found.items():
            assert found, f"'{path}' pattern missing from blocked_values"

    def test_blocked_values_contains_bearer_token_pattern(self, collector_config):
        """Bearer token pattern is listed in blocked_values."""
        redaction = collector_config["processors"]["redaction"]
        blocked_values = redaction.get("blocked_values", [])

        bearer_found = any("Bearer" in pattern for pattern in blocked_values)
        assert bearer_found, "Bearer token pattern missing from blocked_values"

    def test_blocked_values_contains_jwt_token_pattern(self, collector_config):
        """JWT token pattern is listed in blocked_values."""
        redaction = collector_config["processors"]["redaction"]
        blocked_values = redaction.get("blocked_values", [])

        jwt_found = any("eyJ" in pattern for pattern in blocked_values)
        assert jwt_found, "JWT token pattern (eyJ) missing from blocked_values"

    def test_summary_mode_configured(self, collector_config):
        """The summary mode is configured."""
        redaction = collector_config["processors"]["redaction"]
        summary = redaction.get("summary")
        assert summary in ["debug", "info", "silent"], (
            f"invalid summary mode: {summary}"
        )


class TestRedactionProcessorInPipelines:
    """Redaction processor pipeline membership checks (as of v0.96.0)."""

    def test_redaction_in_traces_pipeline(self, collector_config):
        """The traces pipeline includes the redaction processor."""
        pipelines = collector_config["service"]["pipelines"]
        traces_processors = pipelines["traces"]["processors"]
        assert "redaction" in traces_processors, (
            "redaction processor missing from traces pipeline"
        )

    def test_redaction_is_last_processor_in_traces(self, collector_config):
        """Redaction is the last processor in the traces pipeline (last line of defense)."""
        pipelines = collector_config["service"]["pipelines"]
        traces_processors = pipelines["traces"]["processors"]

        last_processor = traces_processors[-1]
        assert last_processor == "redaction", (
            f"redaction is not last in the traces pipeline; last: {last_processor}"
        )

    def test_logs_pipeline_uses_attributes_processor(self, collector_config):
        """The logs pipeline uses the attributes/logs processor."""
        pipelines = collector_config["service"]["pipelines"]
        logs_processors = pipelines["logs"]["processors"]
        assert "attributes/logs" in logs_processors, (
            "attributes/logs processor missing from logs pipeline"
        )

    def test_redaction_in_logs_pipeline(self, collector_config):
        """The logs pipeline includes the redaction processor (v0.115.0+)."""
        pipelines = collector_config["service"]["pipelines"]
        logs_processors = pipelines["logs"]["processors"]
        assert "redaction" in logs_processors, (
            "redaction processor missing from logs pipeline (requires v0.115.0+)"
        )

    def test_redaction_in_metrics_pipeline(self, collector_config):
        """The metrics pipeline includes the redaction processor (v0.115.0+)."""
        pipelines = collector_config["service"]["pipelines"]
        metrics_processors = pipelines["metrics"]["processors"]
        assert "redaction" in metrics_processors, (
            "redaction processor missing from metrics pipeline (requires v0.115.0+)"
        )


class TestPrometheusRemoteWriteQueue:
    """prometheusremotewrite exporter remote_write_queue configuration checks."""

    def test_prometheusremotewrite_has_remote_write_queue(self, collector_config):
        """The prometheusremotewrite exporter has a remote_write_queue."""
        exporters = collector_config.get("exporters", {})
        prw = exporters.get("prometheusremotewrite", {})
        remote_write_queue = prw.get("remote_write_queue")
        assert remote_write_queue is not None, (
            "prometheusremotewrite has no remote_write_queue"
        )

    def test_prometheusremotewrite_has_wal(self, collector_config):
        """The prometheusremotewrite exporter has a WAL (disk buffering)."""
        exporters = collector_config.get("exporters", {})
        prw = exporters.get("prometheusremotewrite", {})
        wal = prw.get("wal")
        assert wal is not None, (
            "prometheusremotewrite has no WAL (disk buffering required)"
        )
        assert wal.get("directory") is not None, "WAL directory is not configured"

    def test_prometheusremotewrite_queue_enabled(self, collector_config):
        """The prometheusremotewrite remote_write_queue is enabled."""
        prw = collector_config["exporters"]["prometheusremotewrite"]
        remote_write_queue = prw.get("remote_write_queue", {})
        assert remote_write_queue.get("enabled") is True, (
            "prometheusremotewrite remote_write_queue is disabled"
        )

    def test_prometheusremotewrite_queue_size_configured(self, collector_config):
        """The prometheusremotewrite queue_size is configured."""
        prw = collector_config["exporters"]["prometheusremotewrite"]
        remote_write_queue = prw.get("remote_write_queue", {})
        queue_size = remote_write_queue.get("queue_size")
        assert queue_size is not None, (
            f"invalid prometheusremotewrite queue_size: {queue_size}"
        )
        assert queue_size > 0, f"invalid prometheusremotewrite queue_size: {queue_size}"

    def test_tempo_and_loki_use_file_storage(self, collector_config):
        """The Tempo and Loki exporters use file_storage."""
        exporters = collector_config.get("exporters", {})

        # Tempo
        tempo = exporters.get("otlp/tempo", {})
        tempo_storage = tempo.get("sending_queue", {}).get("storage")
        assert tempo_storage == "file_storage", (
            f"Tempo exporter does not use file_storage: {tempo_storage}"
        )

        # Loki (v0.144.0+: uses otlphttp/loki)
        loki = exporters.get("otlphttp/loki", {})
        loki_storage = loki.get("sending_queue", {}).get("storage")
        assert loki_storage == "file_storage", (
            f"Loki exporter does not use file_storage: {loki_storage}"
        )


class TestCollectorWithRedactionLive:
    """Verify that a live Collector starts with the redaction processor."""

    def test_collector_health_check_via_http(self):
        """The Collector health check passes (HTTP request)."""
        import requests

        collector_health_url = os.environ.get(
            "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133"
        )
        try:
            response = requests.get(collector_health_url, timeout=5)
            assert response.status_code == 200, (
                f"Collector health check failed: {response.status_code}"
            )
        except requests.RequestException as e:
            pytest.fail(f"Cannot connect to Collector: {e}")

    def test_collector_metrics_endpoint_available(self):
        """The Collector metrics endpoint responds."""
        import requests

        base_url = os.environ.get(
            "COLLECTOR_METRICS_ENDPOINT", "http://otel-collector:8888"
        )
        collector_metrics_url = (
            f"{base_url}/metrics" if "/metrics" not in base_url else base_url
        )
        try:
            response = requests.get(collector_metrics_url, timeout=5)
            # Check the metrics endpoint response
            assert response.status_code == 200, (
                f"Collector metrics endpoint failed: {response.status_code}"
            )
            # Check Prometheus-format metrics
            assert "otelcol" in response.text or "process" in response.text, (
                "Collector returned no metrics"
            )
        except requests.RequestException as e:
            pytest.fail(f"Cannot connect to Collector metrics endpoint: {e}")


class TestRedactionMaskingBehavior:
    """Verify actual masking behavior.

    Send traces/logs containing sensitive data via OTLP, then confirm
    masking in Tempo/Loki.

    Test method:
    1. Send a trace containing sensitive data via OTLP HTTP
    2. Query the trace from the Tempo API
    3. Confirm the sensitive data was masked
    """

    @pytest.fixture(scope="class")
    def otlp_endpoint(self):
        """OTLP HTTP endpoint."""
        return os.environ.get(
            "OTLP_HTTP_ENDPOINT",
            os.environ.get("OTEL_COLLECTOR_ENDPOINT", "http://otel-collector:4318"),
        )

    @pytest.fixture(scope="class")
    def tempo_endpoint(self):
        """Tempo query endpoint."""
        return os.environ.get("TEMPO_ENDPOINT", "http://tempo:3200")

    def _generate_trace_id(self):
        """Generate a 32-char hex trace ID."""
        import uuid

        return uuid.uuid4().hex

    def _generate_span_id(self):
        """Generate a 16-char hex span ID."""
        import uuid

        return uuid.uuid4().hex[:16]

    def test_jwt_token_masked_in_trace(self, otlp_endpoint, tempo_endpoint):
        """A JWT token is masked in trace attributes."""
        import time

        import requests

        trace_id = self._generate_trace_id()
        span_id = self._generate_span_id()

        # Send a trace containing sensitive data
        jwt_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"  # noqa: S105

        trace_data = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "test-service"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "test-scope"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "test-span-with-jwt",
                                    "kind": 1,
                                    "startTimeUnixNano": str(int(time.time() * 1e9)),
                                    "endTimeUnixNano": str(
                                        int((time.time() + 0.1) * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "auth.token",
                                            "value": {"stringValue": jwt_token},
                                        },
                                        {
                                            "key": "user.id",
                                            "value": {"stringValue": "user123"},
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        # Send via OTLP HTTP
        try:
            response = requests.post(
                f"{otlp_endpoint}/v1/traces",
                json=trace_data,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            assert response.status_code in [200, 202], (
                f"OTLP send failed: {response.status_code}"
            )
        except requests.RequestException as e:
            pytest.fail(f"OTLP send failed: {e}")

        # Query the trace from Tempo (batch 10s + tail sampling 10s + indexing)
        from tests.integration.otel.conftest import wait_for_tempo_trace

        response = wait_for_tempo_trace(tempo_endpoint, trace_id, max_wait=45)
        if response is None:
            pytest.skip("Trace was not indexed within the wait window")

        trace_content = response.text
        assert jwt_token not in trace_content, "JWT token was not masked!"

    def test_internal_ip_masked_in_trace(self, otlp_endpoint, tempo_endpoint):
        """An internal IP address is masked in trace attributes."""
        import time

        import requests

        trace_id = self._generate_trace_id()
        span_id = self._generate_span_id()

        # Send a trace containing sensitive data
        internal_ip = "10.0.0.123"

        trace_data = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "test-service"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "test-scope"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "test-span-with-ip",
                                    "kind": 1,
                                    "startTimeUnixNano": str(int(time.time() * 1e9)),
                                    "endTimeUnixNano": str(
                                        int((time.time() + 0.1) * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "server.address",
                                            "value": {"stringValue": internal_ip},
                                        },
                                        {
                                            "key": "request.id",
                                            "value": {"stringValue": "req-001"},
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        # Send via OTLP HTTP
        try:
            response = requests.post(
                f"{otlp_endpoint}/v1/traces",
                json=trace_data,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            assert response.status_code in [200, 202], (
                f"OTLP send failed: {response.status_code}"
            )
        except requests.RequestException as e:
            pytest.fail(f"OTLP send failed: {e}")

        # Query the trace from Tempo (batch 10s + tail sampling 10s + indexing)
        from tests.integration.otel.conftest import wait_for_tempo_trace

        response = wait_for_tempo_trace(tempo_endpoint, trace_id, max_wait=45)
        if response is None:
            pytest.skip("Trace was not indexed within the wait window")

        trace_content = response.text
        assert internal_ip not in trace_content, "internal IP was not masked!"

    def test_bearer_token_masked_in_trace(self, otlp_endpoint, tempo_endpoint):
        """A Bearer token is masked in trace attributes."""
        import time

        import requests

        trace_id = self._generate_trace_id()
        span_id = self._generate_span_id()

        bearer_token = "Bearer sk-1234567890abcdefghij"

        trace_data = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "test-service"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "test-scope"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "test-span-with-bearer",
                                    "kind": 1,
                                    "startTimeUnixNano": str(int(time.time() * 1e9)),
                                    "endTimeUnixNano": str(
                                        int((time.time() + 0.1) * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "http.request.header.authorization",
                                            "value": {"stringValue": bearer_token},
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        try:
            response = requests.post(
                f"{otlp_endpoint}/v1/traces",
                json=trace_data,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            assert response.status_code in [200, 202], (
                f"OTLP send failed: {response.status_code}"
            )
        except requests.RequestException as e:
            pytest.fail(f"OTLP send failed: {e}")

        # Query the trace from Tempo (batch 10s + tail sampling 10s + indexing)
        from tests.integration.otel.conftest import wait_for_tempo_trace

        response = wait_for_tempo_trace(tempo_endpoint, trace_id, max_wait=45)
        if response is None:
            pytest.skip("Trace was not indexed within the wait window")

        trace_content = response.text
        assert bearer_token not in trace_content, "Bearer token was not masked!"
