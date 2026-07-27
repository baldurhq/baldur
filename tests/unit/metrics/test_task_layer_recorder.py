"""Task-layer attempt series — the task queue's own attempts denominator.

A Baldur-protected call and the Celery task that may wrap it are two layers
resolving two different things. They used to share
``baldur_retry_attempts_distribution``, so a task terminal's observation
diluted the protected call's mean. The task layer now owns its own pair,
``baldur_task_attempts_distribution`` / ``baldur_task_outcomes_total``, whose
buckets and label sets deliberately mirror the retry pair so a shipped alert
or panel expression transfers with one token changed.

Covered here: the pair's shape (names, labels, buckets, boundary landing), the
recorder's both-legs-in-one-call contract, the module facade's domain
resolution and fail-open envelope, and the OTel twin that keeps the two
backends' method sets equal.
"""

from __future__ import annotations

import itertools

import pytest
from structlog.testing import capture_logs

# The task-layer metrics are module-level collectors on the process-global
# prometheus REGISTRY, so each assertion records under a domain nothing else has
# used. That makes every read an absolute value and lets a negative assertion
# distinguish "never recorded" (absent sample) from "recorded zero".
_DOMAIN_SEQUENCE = itertools.count()


def _unique_domain(prefix: str) -> str:
    """A metric domain nothing else in this worker has recorded under."""
    return f"{prefix}_{next(_DOMAIN_SEQUENCE)}"


def _sample(name: str, **labels: str) -> float | None:
    """Read one prometheus sample, or None when that series was never recorded."""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels)


class _StubInstrument:
    """OTel instrument double that captures what it was asked to record."""

    def __init__(self) -> None:
        self.adds: list[tuple[float, dict]] = []

    def add(self, amount: float, attributes: dict | None = None) -> None:
        self.adds.append((amount, attributes or {}))

    def record(self, amount: float, attributes: dict | None = None) -> None:
        self.adds.append((amount, attributes or {}))


class _StubMeter:
    """Minimal OTel meter double — hands out capturing instruments by name."""

    def __init__(self) -> None:
        self.instruments: dict[str, _StubInstrument] = {}

    def _instrument(self, name: str) -> _StubInstrument:
        instrument = _StubInstrument()
        self.instruments[name] = instrument
        return instrument

    def create_counter(self, name: str, **_kwargs: object) -> _StubInstrument:
        return self._instrument(name)

    def create_histogram(self, name: str, **_kwargs: object) -> _StubInstrument:
        return self._instrument(name)

    def create_observable_gauge(self, name: str, **_kwargs: object) -> None:
        return None


class _StubGaugeStore:
    """Gauge-store double satisfying the observable-gauge callback wiring."""

    def callback(self, _options: object = None) -> list:
        return []


@pytest.fixture
def recorder():
    """The live Prometheus retry recorder, which owns both layers' series."""
    from baldur.metrics.recorders.retry import RetryMetricRecorder

    return RetryMetricRecorder()


# =============================================================================
# A. Contract — series shape, mirrored from the retry pair
# =============================================================================


class TestTaskLayerSeriesContract:
    """Names, label sets and bucket boundaries of the task-layer pair.

    Hardcoded against the design so a silent edit to either definition fails
    here rather than at the next dashboard render: the shipped panel and alert
    expressions are the retry expressions with one token changed, which only
    holds while the two pairs stay shape-identical.
    """

    def test_task_attempts_histogram_is_named_for_the_task_layer(self, recorder):
        """A distinct series name is the whole separation — not a new label."""
        assert (
            recorder._task_attempts_histogram._name
            == "baldur_task_attempts_distribution"
        )

    def test_task_attempts_histogram_carries_the_documented_buckets(self, recorder):
        """Integer boundaries 1..10 — an attempt count, not a duration."""
        assert recorder._task_attempts_histogram._upper_bounds == [
            1.0,
            2.0,
            3.0,
            4.0,
            5.0,
            6.0,
            7.0,
            8.0,
            9.0,
            10.0,
            float("inf"),
        ]

    def test_task_attempts_buckets_equal_the_retry_attempts_buckets(self, recorder):
        """Element-wise parity with the pair the panel expressions came from."""
        assert (
            recorder._task_attempts_histogram._upper_bounds
            == recorder._attempts_histogram._upper_bounds
        )

    def test_task_attempts_histogram_label_names_equal_the_retry_histogram(
        self, recorder
    ):
        """Same label set, so ``sum by (domain)`` transfers unchanged."""
        assert recorder._task_attempts_histogram._labelnames == (
            "domain",
            "is_synthetic",
        )
        assert (
            recorder._task_attempts_histogram._labelnames
            == recorder._attempts_histogram._labelnames
        )

    def test_task_outcomes_counter_label_names_equal_the_retry_counter(self, recorder):
        """``outcome`` is the third label on both counters."""
        assert recorder._task_outcomes_total._labelnames == (
            "domain",
            "outcome",
            "is_synthetic",
        )
        assert (
            recorder._task_outcomes_total._labelnames
            == recorder._outcomes_total._labelnames
        )

    def test_attempt_count_on_a_boundary_lands_in_that_boundary_bucket(self, recorder):
        """3 attempts is counted at ``le="3.0"``, not first at ``le="4.0"``.

        ``le`` semantics are inclusive; an off-by-one here would shift every
        task-attempt observation one bucket while the count and sum still read
        correctly, so the p95 panel would silently overstate pressure.
        """
        domain = _unique_domain("task_boundary")

        recorder.record_task_attempt(domain, 3, "success")

        assert (
            _sample(
                "baldur_task_attempts_distribution_bucket",
                domain=domain,
                is_synthetic="false",
                le="3.0",
            )
            == 1.0
        )
        assert (
            _sample(
                "baldur_task_attempts_distribution_bucket",
                domain=domain,
                is_synthetic="false",
                le="2.0",
            )
            == 0.0
        )

    def test_attempt_count_above_the_top_boundary_lands_only_in_infinity(
        self, recorder
    ):
        """An 11-attempt task is over range: +Inf only, top finite bucket empty."""
        domain = _unique_domain("task_overflow")

        recorder.record_task_attempt(domain, 11, "failure")

        assert (
            _sample(
                "baldur_task_attempts_distribution_bucket",
                domain=domain,
                is_synthetic="false",
                le="10.0",
            )
            == 0.0
        )
        assert (
            _sample(
                "baldur_task_attempts_distribution_bucket",
                domain=domain,
                is_synthetic="false",
                le="+Inf",
            )
            == 1.0
        )


# =============================================================================
# B. Behavior — RetryMetricRecorder.record_task_attempt
# =============================================================================


class TestTaskLayerRecorderBehavior:
    """One call writes both legs of the pair, and nothing else.

    The counter is the alert's denominator and the histogram is its numerator
    source, so a one-leg exit would not fail loudly — it would quietly skew a
    ratio. Both legs are therefore asserted in one Act.
    """

    @pytest.mark.parametrize("outcome", ["success", "failure"])
    @pytest.mark.parametrize("attempt_count", [1, 6])
    def test_record_task_attempt_writes_both_legs_in_one_call(
        self, recorder, outcome, attempt_count
    ):
        """Histogram observes the attempt count; counter increments the outcome."""
        domain = _unique_domain("task_both_legs")

        recorder.record_task_attempt(domain, attempt_count, outcome)

        assert (
            _sample(
                "baldur_task_attempts_distribution_count",
                domain=domain,
                is_synthetic="false",
            )
            == 1.0
        )
        assert _sample(
            "baldur_task_attempts_distribution_sum",
            domain=domain,
            is_synthetic="false",
        ) == float(attempt_count)
        assert (
            _sample(
                "baldur_task_outcomes_total",
                domain=domain,
                outcome=outcome,
                is_synthetic="false",
            )
            == 1.0
        )

    def test_record_task_attempt_leaves_the_protected_call_series_untouched(
        self, recorder
    ):
        """The separation, asserted from the retry pair's side.

        This is the defect being repaired: the same call used to land on
        ``baldur_retry_attempts_distribution`` and dilute its mean.
        """
        domain = _unique_domain("task_isolation")

        recorder.record_task_attempt(domain, 6, "failure")

        assert (
            _sample(
                "baldur_retry_attempts_distribution_count",
                domain=domain,
                is_synthetic="false",
            )
            is None
        )
        assert (
            _sample(
                "baldur_retry_outcomes_total",
                domain=domain,
                outcome="failure",
                is_synthetic="false",
            )
            is None
        )

    def test_record_task_attempt_carries_the_synthetic_label_in_a_test_session(
        self, recorder
    ):
        """Synthetic traffic lands on its own series so alerts can exclude it."""
        from baldur.core.test_mode_context import TestModeContext

        domain = _unique_domain("task_synthetic")

        with TestModeContext.start(session_id="task-layer-test"):
            recorder.record_task_attempt(domain, 2, "success")

        assert (
            _sample(
                "baldur_task_attempts_distribution_count",
                domain=domain,
                is_synthetic="true",
            )
            == 1.0
        )
        assert (
            _sample(
                "baldur_task_attempts_distribution_count",
                domain=domain,
                is_synthetic="false",
            )
            is None
        )

    def test_record_task_attempt_is_fail_open_when_the_histogram_raises(self, recorder):
        """A metrics fault must not escape into the Celery signal path.

        The recorder runs inside the worker's terminal signal handler; raising
        here would turn an observability fault into a task failure.
        """

        class _BrokenHistogram:
            def labels(self, **_kwargs: str) -> None:
                raise RuntimeError("metric registry corrupted")

        recorder._task_attempts_histogram = _BrokenHistogram()

        with capture_logs() as logs:
            recorder.record_task_attempt("billing", 3, "failure")

        assert any(
            log["event"] == "metrics.record_task_attempt_failed"
            and log["log_level"] == "warning"
            for log in logs
        )

    def test_record_task_attempt_is_fail_open_when_the_counter_raises(self, recorder):
        """The second leg faulting is swallowed the same way as the first."""

        class _BrokenCounter:
            def labels(self, **_kwargs: str) -> None:
                raise RuntimeError("metric registry corrupted")

        recorder._task_outcomes_total = _BrokenCounter()

        with capture_logs() as logs:
            recorder.record_task_attempt("billing", 3, "failure")

        assert any(
            log["event"] == "metrics.record_task_attempt_failed"
            and log["log_level"] == "warning"
            for log in logs
        )


# =============================================================================
# C. Behavior — the services.metrics.recorders module facade
# =============================================================================


class TestTaskLayerFacadeBehavior:
    """``record_task_attempt`` resolves the domain, delegates, and never raises.

    Every production caller reaches the recorder through this facade, so the
    cardinality guard and the fail-open envelope are its contract — not the
    recorder's.
    """

    def test_facade_resolves_an_unregistered_domain_to_the_fallback(self):
        """The domain comes from a Celery task name — arbitrary, so guarded."""
        from unittest.mock import MagicMock, patch

        from baldur.metrics.registry import _FALLBACK_DOMAIN
        from baldur.services.metrics.recorders import record_task_attempt

        mock_metrics = MagicMock(spec=["retry"])
        with patch("baldur.metrics.prometheus.get_metrics", return_value=mock_metrics):
            record_task_attempt("never_registered_task_domain", 4, "failure")

        mock_metrics.retry.record_task_attempt.assert_called_once_with(
            _FALLBACK_DOMAIN, 4, "failure"
        )

    def test_facade_passes_a_registered_domain_through_unchanged(self):
        """A registered domain reaches the recorder with its own label."""
        from unittest.mock import MagicMock, patch

        from baldur.services.metrics.recorders import record_task_attempt

        mock_metrics = MagicMock(spec=["retry"])
        with patch("baldur.metrics.prometheus.get_metrics", return_value=mock_metrics):
            record_task_attempt("external_service", 6, "success")

        mock_metrics.retry.record_task_attempt.assert_called_once_with(
            "external_service", 6, "success"
        )

    def test_facade_is_fail_open_and_warns_when_the_recorder_raises(self):
        """A backend fault is logged, not propagated to the signal handler."""
        from unittest.mock import MagicMock, patch

        from baldur.services.metrics.recorders import record_task_attempt

        mock_metrics = MagicMock(spec=["retry"])
        mock_metrics.retry.record_task_attempt.side_effect = RuntimeError(
            "metrics backend down"
        )

        with (
            patch("baldur.metrics.prometheus.get_metrics", return_value=mock_metrics),
            capture_logs() as logs,
        ):
            record_task_attempt("external_service", 3, "failure")

        record = next(
            log for log in logs if log["event"] == "metrics.record_task_attempt_failed"
        )
        assert record["log_level"] == "warning"


# =============================================================================
# D. Behavior — the OTel twin
# =============================================================================


class TestTaskLayerOtelTwinBehavior:
    """The OTel recorder writes the same two series with the same attributes.

    The twin is required for backend parity: a method present on only one
    backend is a signal that silently disappears for every operator running
    the other one.
    """

    @pytest.fixture
    def otel_recorder(self):
        from baldur.metrics.otel_backend import _OTELRetryRecorder

        meter = _StubMeter()
        return _OTELRetryRecorder(
            meter, "baldur", lambda _name: _StubGaugeStore()
        ), meter

    def test_otel_twin_records_both_instruments_with_matching_attributes(
        self, otel_recorder
    ):
        """Histogram takes the count, counter takes 1, attributes are identical
        except for the counter's extra ``outcome``."""
        recorder, meter = otel_recorder

        recorder.record_task_attempt("billing", 4, "failure")

        assert meter.instruments["baldur_task_attempts_distribution"].adds == [
            (4, {"domain": "billing", "is_synthetic": "false"})
        ]
        assert meter.instruments["baldur_task_outcomes_total"].adds == [
            (1, {"domain": "billing", "outcome": "failure", "is_synthetic": "false"})
        ]

    def test_otel_twin_leaves_the_protected_call_series_untouched(self, otel_recorder):
        """Same separation as the Prometheus recorder, asserted per instrument."""
        recorder, meter = otel_recorder

        recorder.record_task_attempt("billing", 4, "failure")

        assert meter.instruments["baldur_retry_attempts_distribution"].adds == []
        assert meter.instruments["baldur_retry_outcomes_total"].adds == []
        assert meter.instruments["baldur_task_retries_total"].adds == []

    def test_otel_twin_is_fail_open_when_an_instrument_raises(self, otel_recorder):
        """Fail-open parity with the Prometheus recorder."""
        recorder, _meter = otel_recorder

        class _BrokenInstrument:
            def record(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("otel exporter down")

        recorder._task_attempts_histogram = _BrokenInstrument()

        with capture_logs() as logs:
            recorder.record_task_attempt("billing", 4, "failure")

        assert any(
            log["event"] == "metrics.record_task_attempt_failed"
            and log["log_level"] == "warning"
            for log in logs
        )
