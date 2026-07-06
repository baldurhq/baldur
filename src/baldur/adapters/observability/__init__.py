"""
Observability Adapters for Baldur System

This module provides optional observability adapters that extend
the baldur system's telemetry capabilities.

Primary Observability:
    - Prometheus/Grafana: Core metrics (see metrics/prometheus.py)
    - Audit system: Full audit logging with hash chain integrity

Distributed Tracing:
    - trace_id propagation via audit/trace.py (W3C, X-Ray, Zipkin headers)
    - URL template support for Jaeger/Zipkin UI links

Note:
    OpenTelemetry integration was intentionally removed to maintain
    minimal dependencies. The system supports trace_id propagation
    without requiring heavy OTel SDK dependencies (~50MB+).
"""

__all__: list[str] = []
