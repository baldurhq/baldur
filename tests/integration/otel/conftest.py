"""
OTEL integration test shared fixtures and helpers.

Provides retry helpers that account for OTEL Collector's batch processor
timeout (10s) + tail sampling decision wait (10s) + backend indexing delay.
"""

from __future__ import annotations

import time

import requests


def wait_for_tempo_trace(
    tempo_endpoint: str,
    trace_id: str,
    max_wait: int = 60,
    interval: int = 3,
) -> requests.Response | None:
    """Poll Tempo until a trace is available or timeout.

    Returns the successful response, or None if not found within max_wait.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            resp = requests.get(f"{tempo_endpoint}/api/traces/{trace_id}", timeout=5)
            if resp.status_code == 200:
                return resp
        except requests.RequestException:
            pass
        time.sleep(interval)
    return None


def wait_for_loki_logs(
    loki_endpoint: str,
    query: str,
    max_wait: int = 60,
    interval: int = 3,
) -> dict | None:
    """Poll Loki until logs matching the query appear or timeout.

    Returns the parsed JSON data, or None if no results within max_wait.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{loki_endpoint}/loki/api/v1/query_range",
                params={"query": query, "limit": 10},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("data", {}).get("result", [])
                if results:
                    return data
        except requests.RequestException:
            pass
        time.sleep(interval)
    return None


def wait_for_mimir_metric(
    mimir_endpoint: str,
    query: str,
    max_wait: int = 60,
    interval: int = 3,
) -> dict | None:
    """Poll Mimir until the metric query returns data or timeout.

    Returns the parsed JSON data, or None if no results within max_wait.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{mimir_endpoint}/prometheus/api/v1/query",
                params={"query": query},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("data", {}).get("result", [])
                if results:
                    return data
        except requests.RequestException:
            pass
        time.sleep(interval)
    return None
