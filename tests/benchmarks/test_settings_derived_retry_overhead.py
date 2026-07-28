"""Per-call cost of the settings-derived retry stage on the cached DLQ profile.

Targets:  ``protect_facade._resolve_retry_stage`` -> ``RetryPolicyConfig.from_settings``

Every other retry benchmark hands ``protect()`` an explicit
``RetryPolicyConfig`` and therefore never reaches ``from_settings`` at all --
the explicit-config branch returns before it. That leaves exactly one shipped
profile paying settings resolution on every call: the canonical
``@dlq_protect`` shape (``circuit_breaker + dlq + retry=True``). Its composer
is cached, but the retry stage is resolved *before* the cache check, so the
resolution -- and the DEBUG event each resolution emits -- is per-call work on
a path built specifically to avoid per-call work.

That emit is not free the way "DEBUG is off in production" suggests: this
project's structlog configuration carries no level filter in the wrapper, so
every ``logger.debug`` runs the full processor chain (including a lock-taking
rate-limit processor) before the stdlib handler decides to drop it. Hence a
watcher.

Setup:    ``protect(name, fn, dlq=True, retry=True, circuit_breaker=True)`` on
          a healthy callable, so the DLQ sink is composed but never fires and
          the measurement isolates resolution + cached-composer dispatch.

Two complementary measurement paths, matching the other 7A rows:

1. **Manual ns-resolution loop** (``test_dlq_protect_profile_quantiles``) --
   ``time.perf_counter_ns()`` around the measured iterations after a warmup
   that primes the composer cache and the CB state. Authoritative quantile
   source, and xdist-safe.
2. **pytest-benchmark cross-validation**
   (``test_dlq_protect_profile_pytest_benchmark``) -- median / iqr / ops for
   comparison against future runs. Auto-disabled under xdist.

The first run establishes the baseline; the thresholds below are a ceiling
with headroom, not a tuned target.
"""

from __future__ import annotations

import statistics
import time
from typing import Any
from unittest.mock import patch

import pytest

from baldur.protect_facade import protect
from baldur.settings.protect import get_protect_settings

# Ceiling, not a tuned target. Baseline measured on the authoring host:
# p50 ~= 1.03 ms, p99 ~= 1.48 ms, of which a cProfile run attributes roughly a
# third to _resolve_retry_stage (settings resolution plus its DEBUG emit) and
# the rest to the composed CB + retry + DLQ-sink chain this profile carries
# anyway. The multiple of headroom below is deliberate: a benchmark that
# flakes on host load stops being read, and this row's job is catching an
# order-of-magnitude regression on a path nothing else watches, not defending
# a percentage.
_TARGET_P50_NS = 5_000_000  # 5 ms
_TARGET_P99_NS = 10_000_000  # 10 ms

_BENCH_NAME = "bench_settings_derived_retry"
_WARMUP_ITERATIONS = 300
_MEASURE_ITERATIONS = 2_000


def _healthy() -> str:
    """Succeed on the first attempt: no retry sleep, no DLQ write."""
    return "ok"


def _call() -> Any:
    """One call through the canonical zero-message-loss decorator profile."""
    return protect(_BENCH_NAME, _healthy, dlq=True, retry=True, circuit_breaker=True)


def _settings_guard() -> None:
    """Confirm ``protect()`` is not short-circuiting before anything is measured.

    A prior test leaving ``BALDUR_PROTECT_ENABLED=false`` makes ``protect()``
    fall through to a bare ``fn()`` call and report ~0 ns -- passing every
    threshold while measuring nothing at all.
    """
    settings = get_protect_settings()
    assert settings.enabled is True, "ProtectSettings.enabled drift detected"


class TestSettingsDerivedRetryOverheadBenchmark:
    """The one shipped profile that resolves retry settings on every call."""

    @pytest.fixture(autouse=True)
    def _isolate_from_outbound_coordination(self, monkeypatch):
        """Take the outbound 429 coordinator out of the measurement.

        The benchmark name is an *identified* domain, so the retry loop would
        otherwise resolve the default rate-limit coordinator. Measured, that
        made no difference on a Redis-less host -- the coordinator is resolved
        once, not per call -- but it is not inert where a broker IS reachable,
        and this row must not start swinging on whether a container happens to
        be up in the runner. Pinning the switch off bounds the measurement to
        the retry-resolution cost on every host alike.

        The coordinator's own cost belongs to the rate-limit rows; the kill
        switch here is the deployment-level lever an operator already has, not
        a test-only backdoor.
        """
        from baldur.settings.rate_limit_backoff import (
            reset_rate_limit_backoff_settings,
        )

        monkeypatch.setenv("BALDUR_RATE_LIMIT_BACKOFF_COORDINATION_ENABLED", "false")
        reset_rate_limit_backoff_settings()
        yield
        reset_rate_limit_backoff_settings()

    def test_the_measured_path_resolves_settings_on_every_call(self):
        """Guard: the benchmark below would be inert if the cache short-circuited.

        The composer cache is what makes this profile fast, and it would be a
        reasonable-looking change to move the retry resolution behind it. If
        that ever happens this benchmark stops measuring the thing it was
        added for, and this node -- not the timing one -- is what says so.
        """
        _settings_guard()
        from baldur.services.retry_handler import models

        resolved_domains: list[str] = []
        real = models.RetryPolicyConfig.from_settings

        def counting(domain: str = "default"):
            resolved_domains.append(domain)
            return real(domain)

        with patch.object(models.RetryPolicyConfig, "from_settings", counting):
            _call()
            _call()

        assert resolved_domains == [_BENCH_NAME, _BENCH_NAME], (
            "retry settings are no longer resolved per call on this profile"
        )

    def test_dlq_protect_profile_quantiles(self):
        """p50 / p99 of one call through the cached zero-message-loss profile."""
        _settings_guard()

        for _ in range(_WARMUP_ITERATIONS):
            _call()

        samples: list[int] = []
        for _ in range(_MEASURE_ITERATIONS):
            start = time.perf_counter_ns()
            _call()
            samples.append(time.perf_counter_ns() - start)

        quantiles = statistics.quantiles(samples, n=1000)
        p50, p99 = quantiles[499], quantiles[989]

        print(  # noqa: T201 - benchmark rows report their numbers
            f"\n[{_BENCH_NAME}] p50={p50 / 1000:.1f}us p99={p99 / 1000:.1f}us "
            f"n={_MEASURE_ITERATIONS}"
        )

        assert p50 < _TARGET_P50_NS, f"p50 {p50}ns exceeds {_TARGET_P50_NS}ns"
        assert p99 < _TARGET_P99_NS, f"p99 {p99}ns exceeds {_TARGET_P99_NS}ns"

    @pytest.mark.benchmark(group="settings-derived-retry")
    def test_dlq_protect_profile_pytest_benchmark(self, benchmark: Any):
        """Cross-validation against the plugin's own statistics."""
        _settings_guard()

        for _ in range(_WARMUP_ITERATIONS):
            _call()

        result = benchmark(_call)

        assert result == "ok"
