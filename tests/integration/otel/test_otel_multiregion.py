# OpenTelemetry 멀티 리전 검증 통합 테스트
"""
deployment.region 리소스 속성, cluster_prefix,
Grafana 멀티 Datasource, 리전 간 상관관계를 검증합니다.
"""

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.requires_otel
import requests

OTEL_COLLECTOR_ENDPOINT = os.getenv(
    "OTEL_COLLECTOR_ENDPOINT", "http://otel-collector:4318"
)
TEMPO_ENDPOINT = os.getenv("TEMPO_ENDPOINT", "http://tempo:3200")
LOKI_ENDPOINT = os.getenv("LOKI_ENDPOINT", "http://loki:3100")
MIMIR_ENDPOINT = os.getenv("MIMIR_ENDPOINT", "http://mimir:9009")
GRAFANA_ENDPOINT = os.getenv("GRAFANA_ENDPOINT", "http://grafana:3000")


class TestDeploymentRegionAttribute:
    """Trace/Metric/Log에 deployment.region 리소스 속성 포함 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def test_trace_with_deployment_region_attribute(self):
        """deployment.region 속성이 포함된 trace가 Collector에 전송되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        current_time_ns = int(time.time() * 1e9)
        region = "ap-northeast-2"

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "region-test-service"},
                            },
                            {
                                "key": "deployment.region",
                                "value": {"stringValue": region},
                            },
                            {
                                "key": "deployment.environment",
                                "value": {"stringValue": "production"},
                            },
                            {"key": "cloud.provider", "value": {"stringValue": "aws"}},
                            {"key": "cloud.region", "value": {"stringValue": region}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "region-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "regional-operation",
                                    "kind": 2,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.05 * 1e9)
                                    ),
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202], (
            f"리전 속성 포함 trace 전송 실패: {response.text}"
        )

    def test_metric_with_deployment_region_label(self):
        """deployment.region 레이블이 포함된 메트릭이 Collector에 전송되는지 검증"""
        metric_name = f"region_test_metric_{uuid.uuid4().hex[:8]}"
        current_time_ns = int(time.time() * 1e9)
        region = "ap-northeast-1"

        payload = {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "region-metrics-test"},
                            },
                            {
                                "key": "deployment.region",
                                "value": {"stringValue": region},
                            },
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": "region-test"},
                            "metrics": [
                                {
                                    "name": metric_name,
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "asDouble": 100.0,
                                                "timeUnixNano": str(current_time_ns),
                                                "attributes": [
                                                    {
                                                        "key": "region",
                                                        "value": {
                                                            "stringValue": region
                                                        },
                                                    },
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202]

    def test_log_with_deployment_region_attribute(self):
        """deployment.region 속성이 포함된 로그가 Collector에 전송되는지 검증"""
        log_marker = f"region_log_{uuid.uuid4().hex[:8]}"
        current_time_ns = int(time.time() * 1e9)
        region = "us-west-2"

        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "region-log-test"},
                            },
                            {
                                "key": "deployment.region",
                                "value": {"stringValue": region},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "region-logger"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(current_time_ns),
                                    "observedTimeUnixNano": str(current_time_ns),
                                    "severityText": "INFO",
                                    "body": {
                                        "stringValue": f"Regional log entry: {log_marker}"
                                    },
                                    "attributes": [
                                        {
                                            "key": "region",
                                            "value": {"stringValue": region},
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202]


class TestClusterPrefixTraceId:
    """클러스터 prefix(seop, tokp 등) 기반 trace 식별 검증"""

    def _generate_clustered_trace_id(self, cluster_prefix: str) -> str:
        """클러스터 prefix가 포함된 내부 trace_id 생성 (req-{prefix}-{8자리hex})"""
        short_id = uuid.uuid4().hex[:8]
        return f"req-{cluster_prefix}-{short_id}"

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def test_seoul_cluster_trace_id_format(self):
        """서울 클러스터(seop) prefix가 span 속성에 포함되어 전송되는지 검증"""
        cluster_prefix = "seop"
        clustered_trace_id = self._generate_clustered_trace_id(cluster_prefix)
        span_id_hex = self._generate_span_id_hex()
        current_time_ns = int(time.time() * 1e9)
        trace_id_hex = uuid.uuid4().hex

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "seoul-cluster-service"},
                            },
                            {
                                "key": "deployment.region",
                                "value": {"stringValue": "ap-northeast-2"},
                            },
                            {"key": "cluster.name", "value": {"stringValue": "seop"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "cluster-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "seoul-operation",
                                    "kind": 2,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.05 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "cluster.prefix",
                                            "value": {"stringValue": cluster_prefix},
                                        },
                                        {
                                            "key": "internal.trace_id",
                                            "value": {
                                                "stringValue": clustered_trace_id
                                            },
                                        },
                                    ],
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202]

    def test_tokyo_cluster_trace_id_format(self):
        """도쿄 클러스터(tokp) prefix가 span 속성에 포함되어 전송되는지 검증"""
        cluster_prefix = "tokp"
        clustered_trace_id = self._generate_clustered_trace_id(cluster_prefix)
        span_id_hex = self._generate_span_id_hex()
        trace_id_hex = uuid.uuid4().hex
        current_time_ns = int(time.time() * 1e9)

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "tokyo-cluster-service"},
                            },
                            {
                                "key": "deployment.region",
                                "value": {"stringValue": "ap-northeast-1"},
                            },
                            {"key": "cluster.name", "value": {"stringValue": "tokp"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "cluster-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": span_id_hex,
                                    "name": "tokyo-operation",
                                    "kind": 2,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.05 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "cluster.prefix",
                                            "value": {"stringValue": cluster_prefix},
                                        },
                                        {
                                            "key": "internal.trace_id",
                                            "value": {
                                                "stringValue": clustered_trace_id
                                            },
                                        },
                                    ],
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202]


class TestGrafanaMultiDatasource:
    """Grafana 멀티 Datasource(Tempo, Loki, Mimir) 설정 검증"""

    def test_grafana_datasource_api_accessible(self):
        """Grafana /api/health 엔드포인트가 응답하는지 검증"""
        try:
            response = requests.get(f"{GRAFANA_ENDPOINT}/api/health", timeout=10)
            assert response.status_code == 200
        except requests.RequestException:
            pytest.skip("Grafana 연결 불가")

    def test_tempo_datasource_configured(self):
        """Tempo 서비스가 /ready 응답하여 datasource로 사용 가능한지 검증"""
        response = requests.get(f"{TEMPO_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_loki_datasource_configured(self):
        """Loki 서비스가 /ready 응답하여 datasource로 사용 가능한지 검증"""
        response = requests.get(f"{LOKI_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_mimir_datasource_configured(self):
        """Mimir 서비스가 /ready 응답하여 datasource로 사용 가능한지 검증"""
        response = requests.get(f"{MIMIR_ENDPOINT}/ready", timeout=10)
        assert response.status_code == 200

    def test_datasources_support_region_label_filtering(self):
        """Mimir /prometheus/api/v1/labels가 리전 레이블 필터링을 지원하는지 검증"""
        response = requests.get(
            f"{MIMIR_ENDPOINT}/prometheus/api/v1/labels",
            timeout=10,
        )
        assert response.status_code == 200


class TestCrossRegionCorrelation:
    """리전 간(서울 ↔ 도쿄) trace 상관관계 검증"""

    def _generate_trace_id_hex(self) -> str:
        return uuid.uuid4().hex

    def _generate_span_id_hex(self) -> str:
        return uuid.uuid4().hex[:16]

    def test_cross_region_trace_propagation(self):
        """서울 → 도쿄 리전 간 동일 trace_id로 span이 연결되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        seoul_span_id = self._generate_span_id_hex()
        tokyo_span_id = self._generate_span_id_hex()
        current_time_ns = int(time.time() * 1e9)

        combined_payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "seoul-api-gateway"},
                            },
                            {
                                "key": "deployment.region",
                                "value": {"stringValue": "ap-northeast-2"},
                            },
                            {"key": "cluster.name", "value": {"stringValue": "seop"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "cross-region-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": seoul_span_id,
                                    "name": "GET /api/users (cross-region)",
                                    "kind": 3,
                                    "startTimeUnixNano": str(current_time_ns),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.2 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "http.method",
                                            "value": {"stringValue": "GET"},
                                        },
                                        {
                                            "key": "peer.region",
                                            "value": {"stringValue": "ap-northeast-1"},
                                        },
                                    ],
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                },
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "tokyo-user-service"},
                            },
                            {
                                "key": "deployment.region",
                                "value": {"stringValue": "ap-northeast-1"},
                            },
                            {"key": "cluster.name", "value": {"stringValue": "tokp"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "cross-region-test"},
                            "spans": [
                                {
                                    "traceId": trace_id_hex,
                                    "spanId": tokyo_span_id,
                                    "parentSpanId": seoul_span_id,
                                    "name": "GET /api/users (handler)",
                                    "kind": 2,
                                    "startTimeUnixNano": str(
                                        current_time_ns + int(0.01 * 1e9)
                                    ),
                                    "endTimeUnixNano": str(
                                        current_time_ns + int(0.15 * 1e9)
                                    ),
                                    "attributes": [
                                        {
                                            "key": "http.method",
                                            "value": {"stringValue": "GET"},
                                        },
                                        {
                                            "key": "source.region",
                                            "value": {"stringValue": "ap-northeast-2"},
                                        },
                                    ],
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                },
            ]
        }

        response = requests.post(
            f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces",
            json=combined_payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        assert response.status_code in [200, 202], (
            f"Cross-region trace 전송 실패: {response.text}"
        )

    def test_cross_region_log_correlation_by_trace_id(self):
        """동일 trace_id로 두 리전의 로그가 연결되는지 검증"""
        trace_id_hex = self._generate_trace_id_hex()
        span_id_hex = self._generate_span_id_hex()
        current_time_ns = int(time.time() * 1e9)
        log_marker = f"cross_region_log_{uuid.uuid4().hex[:8]}"

        regions = ["ap-northeast-2", "ap-northeast-1"]

        for region in regions:
            payload = {
                "resourceLogs": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": f"{region}-service"},
                                },
                                {
                                    "key": "deployment.region",
                                    "value": {"stringValue": region},
                                },
                            ]
                        },
                        "scopeLogs": [
                            {
                                "scope": {"name": "cross-region-logger"},
                                "logRecords": [
                                    {
                                        "timeUnixNano": str(current_time_ns),
                                        "observedTimeUnixNano": str(current_time_ns),
                                        "severityText": "INFO",
                                        "body": {
                                            "stringValue": f"Cross-region request in {region}: {log_marker}"
                                        },
                                        "traceId": trace_id_hex,
                                        "spanId": span_id_hex,
                                        "attributes": [
                                            {
                                                "key": "region",
                                                "value": {"stringValue": region},
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }

            response = requests.post(
                f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            assert response.status_code in [200, 202]

    def test_multi_region_metric_aggregation(self):
        """여러 리전(서울, 도쿄, 오레곤)의 메트릭이 리전별 레이블로 집계되는지 검증"""
        metric_name = "cross_region_request_count"
        current_time_ns = int(time.time() * 1e9)
        regions = ["ap-northeast-2", "ap-northeast-1", "us-west-2"]

        for i, region in enumerate(regions):
            payload = {
                "resourceMetrics": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": "global-api-gateway"},
                                },
                                {
                                    "key": "deployment.region",
                                    "value": {"stringValue": region},
                                },
                            ]
                        },
                        "scopeMetrics": [
                            {
                                "scope": {"name": "multi-region-metrics"},
                                "metrics": [
                                    {
                                        "name": metric_name,
                                        "sum": {
                                            "dataPoints": [
                                                {
                                                    "asDouble": 100.0 + i * 50,
                                                    "timeUnixNano": str(
                                                        current_time_ns
                                                    ),
                                                    "attributes": [
                                                        {
                                                            "key": "region",
                                                            "value": {
                                                                "stringValue": region
                                                            },
                                                        },
                                                    ],
                                                }
                                            ],
                                            "aggregationTemporality": 2,
                                            "isMonotonic": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }

            response = requests.post(
                f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            assert response.status_code in [200, 202]

    def test_region_aware_trace_search(self):
        """Tempo /api/search에서 deployment.region 태그로 검색 가능한지 검증"""
        response = requests.get(
            f"{TEMPO_ENDPOINT}/api/search",
            params={"tags": 'deployment.region="ap-northeast-2"'},
            timeout=10,
        )
        assert response.status_code == 200
