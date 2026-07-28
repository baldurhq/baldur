"""Timely retry-pressure series — the per-attempt half of the retry pair.

``baldur_retry_attempts_started_total`` is incremented at attempt admission,
before the call runs and before any backoff or rate-limit cooldown wait, so
retry pressure is readable while a storm is still in flight. Its companion —
the attempts histogram — takes exactly one observation per sequence, at that
sequence's terminal, and so cannot move at all while sequences sit asleep.

The retry share the shipped alert reads is ``{is_retry="true"}`` over the whole
series: the numerator is a label child of its own denominator. Two consequences
are asserted here. Exactly one child moves per call, which is what makes
numerator ⊆ denominator hold by construction rather than by writer discipline.
And the ``is_retry`` label value is a lowercase *string*: prometheus_client
stringifies label values with ``str()``, so a bare Python bool would emit
``is_retry="True"`` and the shipped ``is_retry="true"`` matcher would select
nothing — an alert that can never fire, against a registry that looks healthy.

Covered: the series shape, the recorder's per-call child selection and its
fail-open envelope, the module facade's domain resolution and delegation, and
the OTel twin's emitted attributes (the backend-parity gate pins the twin's
method *set*, not the values it writes).
"""

from __future__ import annotations

import itertools

import pytest
from structlog.testing import capture_logs

_SERIES = "baldur_retry_attempts_started_total"

# The series is a module-level collector on the process-global prometheus
# REGISTRY shared across the whole xdist worker, so each assertion records
# under a domain nothing else has used. That makes every read an absolute
# value and lets a negative assertion distinguish "never recorded" (absent
# sample) from "recorded zero".
_DOMAIN_SEQUENCE = itertools.count()


def _unique_domain(prefix: str) -> str:
    """A metric domain nothing else in this worker has recorded under."""
    return f"{prefix}_{next(_DOMAIN_SEQUENCE)}"


def _sample(name: str, **labels: str) -> float | None:
    """Read one prometheus sample, or None when that series was never recorded."""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels)


class _RaisingCounter:
    """Counter double that raises where production actually touches it.

    Production calls ``.labels(...)`` on the collector; patching the collector
    itself with a ``side_effect`` never fires, so the fail-open arm would go
    unexercised while the test still passed. ``touched`` is the witness that
    the fault happened at all.
    """

    def __init__(self) -> None:
        self.touched = False

    def labels(self, **_kwargs: str) -> None:
        self.touched = True
        raise RuntimeError("metric registry corrupted")


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
    """The live Prometheus retry recorder, which owns both halves of the pair."""
    from baldur.metrics.recorders.retry import RetryMetricRecorder

    return RetryMetricRecorder()


# =============================================================================
# A. Contract — series shape and the label encoding the alert depends on
# =============================================================================


class TestRetryAttemptStartedLabelContract:
    """Name, label set, and the exact ``is_retry`` label values emitted.

    Hardcoded against the design because the shipped alert and panel
    expressions name all three verbatim: the series, the ``domain`` grouping
    key, and the ``is_retry="true"`` matcher. A silent edit to any of them
    breaks the rule without breaking anything a type checker or a registry
    health check can see.
    """

    def test_attempts_started_series_carries_the_designed_name(self, recorder):
        """The public name the alert, both boards, and the CHANGELOG cite."""
        assert recorder._attempts_started_total._name == _SERIES.removesuffix("_total")

    def test_attempts_started_series_carries_the_designed_label_names(self, recorder):
        """``domain`` groups, ``is_retry`` splits, ``is_synthetic`` excludes."""
        assert recorder._attempts_started_total._labelnames == (
            "domain",
            "is_retry",
            "is_synthetic",
        )

    @pytest.mark.parametrize(
        ("is_retry", "expected_label", "capitalized"),
        [(True, "true", "True"), (False, "false", "False")],
        ids=["retry", "first_attempt"],
    )
    def test_is_retry_label_is_lowercase_for_both_boolean_values(
        self, recorder, is_retry, expected_label, capitalized
    ):
        """The bool is normalized to a lowercase string before it reaches the label.

        The negative half is the one that matters: prometheus_client would
        stringify an unnormalized bool with ``str()``, and the resulting
        ``is_retry="True"`` child is invisible to every shipped matcher, so the
        alert would report zero pressure during a storm.
        """
        domain = _unique_domain("pressure_label")

        recorder.record_attempt_started(domain, is_retry)

        assert (
            _sample(
                _SERIES,
                domain=domain,
                is_retry=expected_label,
                is_synthetic="false",
            )
            == 1.0
        )
        assert (
            _sample(
                _SERIES,
                domain=domain,
                is_retry=capitalized,
                is_synthetic="false",
            )
            is None
        )


# =============================================================================
# B. Behavior — RetryMetricRecorder.record_attempt_started
# =============================================================================


class TestRetryAttemptStartedRecorderBehavior:
    """One call moves exactly one child, and a fault never leaves the recorder.

    The ratio's denominator is the whole series and its numerator one child of
    it, so "increment both" or "increment neither" would not fail loudly — it
    would quietly skew a shipped alert. The child selection is therefore
    asserted from both sides on every call.
    """

    @pytest.mark.parametrize(
        ("is_retry", "moved", "untouched"),
        [(True, "true", "false"), (False, "false", "true")],
        ids=["retry", "first_attempt"],
    )
    def test_record_attempt_started_moves_exactly_one_child_per_call(
        self, recorder, is_retry, moved, untouched
    ):
        """The sibling child stays absent — never recorded, not recorded zero."""
        domain = _unique_domain("pressure_child")

        recorder.record_attempt_started(domain, is_retry)

        assert (
            _sample(_SERIES, domain=domain, is_retry=moved, is_synthetic="false") == 1.0
        )
        assert (
            _sample(_SERIES, domain=domain, is_retry=untouched, is_synthetic="false")
            is None
        )

    def test_repeated_records_accumulate_on_the_same_child(self, recorder):
        """A three-attempt sequence's shape: one first attempt, two retries."""
        # Given
        domain = _unique_domain("pressure_accumulate")

        # When — the admission pattern of a 3-attempt sequence
        recorder.record_attempt_started(domain, False)
        recorder.record_attempt_started(domain, True)
        recorder.record_attempt_started(domain, True)

        # Then — the retry share of this domain is 2/3, readable from the pair
        retries = _sample(_SERIES, domain=domain, is_retry="true", is_synthetic="false")
        first = _sample(_SERIES, domain=domain, is_retry="false", is_synthetic="false")
        assert retries == 2.0
        assert first == 1.0

    def test_record_attempt_started_leaves_the_terminal_series_untouched(
        self, recorder
    ):
        """The timely half writes nothing to the resolution-lagged half.

        The two answer different questions and feed different panels; a start
        that also observed the histogram would inflate the distribution by one
        sample per attempt instead of one per resolved call.
        """
        domain = _unique_domain("pressure_isolation")

        recorder.record_attempt_started(domain, True)

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
                outcome="success",
                is_synthetic="false",
            )
            is None
        )

    def test_record_attempt_started_carries_the_synthetic_label_of_the_context(
        self, recorder
    ):
        """Synthetic traffic lands on its own child so the alert can exclude it."""
        from baldur.core.test_mode_context import TestModeContext

        domain = _unique_domain("pressure_synthetic")

        with TestModeContext.start(session_id="retry-pressure-test"):
            recorder.record_attempt_started(domain, True)

        assert (
            _sample(_SERIES, domain=domain, is_retry="true", is_synthetic="true") == 1.0
        )
        assert (
            _sample(_SERIES, domain=domain, is_retry="true", is_synthetic="false")
            is None
        )

    def test_record_attempt_started_is_fail_open_when_the_counter_raises(
        self, recorder
    ):
        """A metrics fault must never escape into the retry loop.

        The recorder runs at the top of every attempt, on the business call's
        own thread; raising here would turn an observability fault into a
        failed request on a path that was about to succeed.
        """
        counter = _RaisingCounter()
        recorder._attempts_started_total = counter

        with capture_logs() as logs:
            recorder.record_attempt_started("billing", True)

        assert counter.touched
        assert any(
            log["event"] == "metrics.record_attempt_started_failed"
            and log["log_level"] == "warning"
            for log in logs
        )


# =============================================================================
# C. Behavior — the services.metrics.recorders module facade
# =============================================================================


class TestRetryAttemptStartedFacadeBehavior:
    """The facade resolves the domain, delegates, and never raises.

    Every production caller reaches the recorder through it, so the cardinality
    guard and the fail-open envelope are the facade's contract, not the
    recorder's — and the domain arriving here is caller-controlled (``protect()``
    passes its own name straight through).
    """

    def test_facade_resolves_an_unregistered_domain_to_the_fallback(self):
        """An unregistered protect name collapses to the bounded fallback label."""
        from unittest.mock import MagicMock, patch

        from baldur.metrics.registry import _FALLBACK_DOMAIN
        from baldur.services.metrics.recorders import record_retry_attempt_started

        mock_metrics = MagicMock(spec=["retry"])
        with patch("baldur.metrics.prometheus.get_metrics", return_value=mock_metrics):
            record_retry_attempt_started("never_registered_retry_domain", True)

        mock_metrics.retry.record_attempt_started.assert_called_once_with(
            _FALLBACK_DOMAIN, True
        )

    def test_facade_passes_a_registered_domain_through_with_its_is_retry_flag(self):
        """A registered domain keeps its own label, and the flag is forwarded."""
        from unittest.mock import MagicMock, patch

        from baldur.services.metrics.recorders import record_retry_attempt_started

        mock_metrics = MagicMock(spec=["retry"])
        with patch("baldur.metrics.prometheus.get_metrics", return_value=mock_metrics):
            record_retry_attempt_started("external_service", False)

        mock_metrics.retry.record_attempt_started.assert_called_once_with(
            "external_service", False
        )

    def test_facade_is_fail_open_and_warns_when_the_recorder_raises(self):
        """A backend fault is logged, not propagated up into the retry loop."""
        from unittest.mock import MagicMock, patch

        from baldur.services.metrics.recorders import record_retry_attempt_started

        mock_metrics = MagicMock(spec=["retry"])
        mock_metrics.retry.record_attempt_started.side_effect = RuntimeError(
            "metrics backend down"
        )

        with (
            patch("baldur.metrics.prometheus.get_metrics", return_value=mock_metrics),
            capture_logs() as logs,
        ):
            record_retry_attempt_started("external_service", True)

        record = next(
            log
            for log in logs
            if log["event"] == "metrics.record_attempt_started_failed"
        )
        assert record["log_level"] == "warning"


# =============================================================================
# D. Behavior — the OTel twin
# =============================================================================


class TestRetryAttemptStartedOtelTwinBehavior:
    """The OTel recorder writes the same series with the same attribute values.

    The backend-parity gate pins that the twin *exists* with an equal method
    set; it cannot see what the twin emits. The lowercase normalization is
    duplicated on this side of the fence, so one shipped matcher works on
    either backend only as long as both spellings agree — which is exactly what
    a gate on method names would let drift.
    """

    @pytest.fixture
    def otel_recorder(self):
        from baldur.metrics.otel_backend import _OTELRetryRecorder

        meter = _StubMeter()
        return _OTELRetryRecorder(
            meter, "baldur", lambda _name: _StubGaugeStore()
        ), meter

    @pytest.mark.parametrize(
        ("is_retry", "expected_label"),
        [(True, "true"), (False, "false")],
        ids=["retry", "first_attempt"],
    )
    def test_otel_twin_emits_the_same_lowercase_is_retry_label(
        self, otel_recorder, is_retry, expected_label
    ):
        """Attribute-for-attribute equal to what the Prometheus child carries."""
        recorder, meter = otel_recorder

        recorder.record_attempt_started("billing", is_retry)

        assert meter.instruments[_SERIES].adds == [
            (
                1,
                {
                    "domain": "billing",
                    "is_retry": expected_label,
                    "is_synthetic": "false",
                },
            )
        ]

    def test_otel_twin_leaves_the_terminal_instruments_untouched(self, otel_recorder):
        """Same separation as the Prometheus recorder, asserted per instrument."""
        recorder, meter = otel_recorder

        recorder.record_attempt_started("billing", True)

        assert meter.instruments["baldur_retry_attempts_distribution"].adds == []
        assert meter.instruments["baldur_retry_outcomes_total"].adds == []

    def test_otel_twin_is_fail_open_when_the_instrument_raises(self, otel_recorder):
        """Fail-open parity with the Prometheus recorder."""
        recorder, _meter = otel_recorder

        class _BrokenInstrument:
            def __init__(self) -> None:
                self.touched = False

            def add(self, *_args: object, **_kwargs: object) -> None:
                self.touched = True
                raise RuntimeError("otel exporter down")

        instrument = _BrokenInstrument()
        recorder._attempts_started_total = instrument

        with capture_logs() as logs:
            recorder.record_attempt_started("billing", True)

        assert instrument.touched
        assert any(
            log["event"] == "metrics.record_attempt_started_failed"
            and log["log_level"] == "warning"
            for log in logs
        )
