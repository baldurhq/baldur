"""Celery terminal-signal attempt counts and the task-retry marker series.

The attempts histogram's contract is *attempts before resolution* — one
observation per resolved call, carrying how many tries that resolution took.
The Celery signal path used to violate it in both directions: every terminal
recorded a hardcoded 1, and every non-terminal ``task_retry`` signal added its
own observation of 1, dragging the mean toward 1.0. These tests pin the two
halves of the repair:

- ``extract_attempt_count`` derives the real count from the live request, and
  both terminal handlers plumb it through ``MetricRecorder``.
- ``task_retry`` moves onto its own ``baldur_task_retries_total`` counter,
  touching neither the terminal-outcome counter nor the histogram.
"""

from __future__ import annotations

import itertools
from unittest.mock import patch

import pytest

from baldur.adapters.celery.handlers.failure_handler import FailureHandler
from baldur.adapters.celery.handlers.success_handler import SuccessHandler
from baldur.adapters.celery.integrations.metric_recorder import MetricRecorder
from baldur.adapters.celery.signal_config import (
    SignalHooksSettings,
    extract_attempt_count,
    extract_domain_from_task_name,
    extract_service_name,
)

_TASK_NAME = "app.tasks.do_work"

# The retry metrics are module-level collectors on the process-global prometheus
# REGISTRY, so each assertion records under a domain nothing else has used. That
# makes the read an absolute value and lets a negative assertion distinguish
# "never recorded" (absent sample) from "recorded zero".
_DOMAIN_SEQUENCE = itertools.count()


def _unique_domain(prefix: str) -> str:
    """A metric domain nothing else in this worker has recorded under."""
    return f"{prefix}_{next(_DOMAIN_SEQUENCE)}"


def _sample(name: str, **labels: str) -> float | None:
    """Read one prometheus sample, or None when that series was never recorded."""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels)


class _Request:
    """Celery request double carrying only the attributes the extractor reads.

    Attributes are assigned dynamically so a case can omit ``retries`` entirely
    and exercise the missing-attribute branch, which a fixed ``__init__``
    signature could not express.
    """

    def __init__(self, **attrs: object) -> None:
        self.__dict__.update(attrs)


class _Task:
    """Celery task double — the ``sender`` a signal dispatches with."""

    def __init__(self, name: str = _TASK_NAME, **attrs: object) -> None:
        self.name = name
        self.max_retries = 3
        self.__dict__.update(attrs)


# =============================================================================
# extract_attempt_count
# =============================================================================


class TestExtractAttemptCountBehavior:
    """``retries + 1``, degrading to 1 whenever the count is not knowable.

    Degrading to 1 rather than 0 keeps every observation inside the histogram's
    lowest bucket boundary — an unknown-count terminal is still one attempt.
    """

    @pytest.mark.parametrize(
        ("sender", "expected"),
        [
            (_Task(request=_Request(retries=0)), 1),
            (_Task(request=_Request(retries=3)), 4),
            (None, 1),
            (_Task(request=None), 1),
            (_Task(), 1),
            (_Task(request=_Request()), 1),
            (_Task(request=_Request(retries="2")), 1),
            (_Task(request=_Request(retries=-1)), 1),
        ],
        ids=[
            "first_run",
            "after_three_retries",
            "no_sender",
            "request_is_none",
            "sender_without_request",
            "request_without_retries",
            "retries_not_an_int",
            "retries_negative",
        ],
    )
    def test_extract_attempt_count_returns_retries_plus_one_or_degrades_to_one(
        self, sender, expected
    ):
        """The attempt count is one more than Celery's retry count, floored at 1."""
        assert extract_attempt_count(sender) == expected

    def test_extract_attempt_count_never_returns_below_one(self):
        """The floor is a hard invariant across the whole degrade surface.

        A 0 here would be silently swallowed by the histogram's lowest bucket
        and would drag the mean below the one-attempt floor.
        """
        degraded_senders = [
            None,
            _Task(request=None),
            _Task(),
            _Task(request=_Request()),
            _Task(request=_Request(retries="2")),
            _Task(request=_Request(retries=-1)),
        ]
        assert all(extract_attempt_count(s) >= 1 for s in degraded_senders)


# =============================================================================
# MetricRecorder plumbing
# =============================================================================


class TestSignalMetricAttemptsBehavior:
    """``MetricRecorder`` forwards the attempt count and routes retries away.

    Asserts the forwarded arguments rather than a return value: the recorder
    returns nothing, so a dropped or swapped ``attempt_count`` is invisible
    anywhere but the call itself.
    """

    @pytest.fixture
    def recorder(self):
        return MetricRecorder(SignalHooksSettings())

    def test_record_success_forwards_the_attempt_count(self, recorder):
        """A task that succeeded on its 4th try records 4, not 1."""
        config = SignalHooksSettings()
        expected_domain = extract_domain_from_task_name(_TASK_NAME, config)

        with patch(
            "baldur.services.metrics.recorders.record_retry_attempt", autospec=True
        ) as mock_record:
            recorder.record_success("svc", _TASK_NAME, attempt_count=4)

        mock_record.assert_called_once_with(
            domain=expected_domain, attempt_count=4, outcome="success"
        )

    def test_record_failure_forwards_the_attempt_count(self, recorder):
        """A task that exhausted 4 attempts records 4 against outcome=failure."""
        with patch(
            "baldur.services.metrics.recorders.record_retry_attempt", autospec=True
        ) as mock_record:
            recorder.record_failure(
                "billing", _TASK_NAME, RuntimeError("boom"), attempt_count=4
            )

        mock_record.assert_called_once_with(
            domain="billing", attempt_count=4, outcome="failure"
        )

    @pytest.mark.parametrize(
        "record",
        [
            lambda r: r.record_success("svc", _TASK_NAME),
            lambda r: r.record_failure("billing", _TASK_NAME, RuntimeError("boom")),
        ],
        ids=["success", "failure"],
    )
    def test_terminal_recorders_default_to_a_single_attempt(self, recorder, record):
        """Omitting the count means "unknown", which is one attempt — never zero."""
        with patch(
            "baldur.services.metrics.recorders.record_retry_attempt", autospec=True
        ) as mock_record:
            record(recorder)

        assert mock_record.call_args.kwargs["attempt_count"] == 1

    def test_record_retry_increments_the_marker_only(self, recorder):
        """A retry signal is non-terminal: marker counter only.

        Negative half is the point — routing it back through
        ``record_retry_attempt`` is exactly the defect being repaired, and it
        would both invent an ``outcome`` value and add a bogus observation of 1
        to the attempts histogram.
        """
        with (
            patch(
                "baldur.services.metrics.recorders.record_retry_marker", autospec=True
            ) as mock_marker,
            patch(
                "baldur.services.metrics.recorders.record_retry_attempt", autospec=True
            ) as mock_attempt,
        ):
            recorder.record_retry("billing", _TASK_NAME)

        mock_marker.assert_called_once_with(domain="billing")
        mock_attempt.assert_not_called()


# =============================================================================
# Retry marker series (Prometheus + OTel twin)
# =============================================================================


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


class TestRetryMarkerRecorderBehavior:
    """``record_retry_marker`` writes its own series under both backends.

    The dedicated series is what keeps a task-queue retry out of the terminal
    outcome vocabulary; without it the alerting expressions cannot tell a retry
    apart from a resolution.
    """

    @pytest.fixture
    def prometheus_recorder(self):
        from baldur.metrics.recorders.retry import RetryMetricRecorder

        return RetryMetricRecorder()

    def test_marker_increments_the_task_retries_counter(self, prometheus_recorder):
        """One retry signal, one increment, labelled by domain."""
        domain = _unique_domain("marker")

        prometheus_recorder.record_retry_marker(domain)

        assert (
            _sample("baldur_task_retries_total", domain=domain, is_synthetic="false")
            == 1.0
        )

    def test_marker_leaves_the_terminal_series_untouched(self, prometheus_recorder):
        """Neither the outcome counter nor the attempts histogram moves.

        This is the dilution guard: an observation here would push the
        histogram's mean toward 1.0 on every retry, for a call that has not
        resolved at all.
        """
        domain = _unique_domain("marker_isolation")

        prometheus_recorder.record_retry_marker(domain)

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
                outcome="retry",
                is_synthetic="false",
            )
            is None
        )

    def test_marker_carries_the_synthetic_label_under_a_test_session(
        self, prometheus_recorder
    ):
        """Synthetic traffic lands on its own series so alerts can exclude it."""
        from baldur.core.test_mode_context import TestModeContext

        domain = _unique_domain("marker_synthetic")

        with TestModeContext.start(session_id="marker-test"):
            prometheus_recorder.record_retry_marker(domain)

        assert (
            _sample("baldur_task_retries_total", domain=domain, is_synthetic="true")
            == 1.0
        )
        assert (
            _sample("baldur_task_retries_total", domain=domain, is_synthetic="false")
            is None
        )

    def test_marker_is_fail_open_when_the_counter_raises(self, prometheus_recorder):
        """A metrics fault must not escape into the Celery signal path."""

        class _BrokenCounter:
            def labels(self, **_kwargs: str) -> None:
                raise RuntimeError("metric registry corrupted")

        prometheus_recorder._task_retries_total = _BrokenCounter()

        # Fails the test only if the fault propagates.
        prometheus_recorder.record_retry_marker("billing")

    def test_otel_twin_adds_to_its_own_counter(self):
        """The OTel recorder writes the same series with the same attributes."""
        from baldur.metrics.otel_backend import _OTELRetryRecorder

        meter = _StubMeter()
        recorder = _OTELRetryRecorder(meter, "baldur", lambda _name: _StubGaugeStore())

        recorder.record_retry_marker("billing")

        assert meter.instruments["baldur_task_retries_total"].adds == [
            (1, {"domain": "billing", "is_synthetic": "false"})
        ]
        assert meter.instruments["baldur_retry_outcomes_total"].adds == []
        assert meter.instruments["baldur_retry_attempts_distribution"].adds == []


# =============================================================================
# Terminal signal handlers
# =============================================================================


class TestSignalHandlersAttemptCountBehavior:
    """Both terminal handlers read the live request and pass the real count."""

    @pytest.fixture
    def _patch_success_integrations(self):
        with (
            patch(
                "baldur.adapters.celery.handlers.success_handler.CircuitBreakerRecorder",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.handlers.success_handler.MetricRecorder",
                autospec=True,
            ) as mock_metric_cls,
        ):
            yield mock_metric_cls.return_value

    @pytest.fixture
    def _patch_failure_integrations(self):
        with (
            patch(
                "baldur.adapters.celery.handlers.failure_handler.CircuitBreakerRecorder",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.handlers.failure_handler.DLQRecorder",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.handlers.failure_handler.ForensicCapture",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.handlers.failure_handler.MetricRecorder",
                autospec=True,
            ) as mock_metric_cls,
        ):
            yield mock_metric_cls.return_value

    def test_success_handler_passes_the_resolved_attempt_count(
        self, _patch_success_integrations
    ):
        """A task that succeeded after 2 retries reports 3 attempts."""
        # Given a terminal success signal from a task on its third attempt
        config = SignalHooksSettings()
        handler = SuccessHandler(config)
        sender = _Task(request=_Request(retries=2))

        # When the signal is handled
        handler.handle(sender=sender)

        # Then the metric recorder receives the derived count, not a literal 1
        _patch_success_integrations.record_success.assert_called_once_with(
            extract_service_name(_TASK_NAME, config),
            _TASK_NAME,
            attempt_count=3,
        )

    def test_failure_handler_passes_the_resolved_attempt_count(
        self, _patch_failure_integrations
    ):
        """A task that failed after 2 retries reports 3 attempts."""
        config = SignalHooksSettings()
        handler = FailureHandler(config)
        sender = _Task(request=_Request(retries=2))
        exception = RuntimeError("boom")

        handler.handle(sender=sender, task_id="task-1", exception=exception)

        _patch_failure_integrations.record_failure.assert_called_once_with(
            extract_domain_from_task_name(_TASK_NAME, config),
            _TASK_NAME,
            exception,
            attempt_count=3,
        )

    def test_success_handler_degrades_to_one_attempt_without_a_sender(
        self, _patch_success_integrations
    ):
        """A dispatch with no live request context still records one attempt."""
        handler = SuccessHandler(SignalHooksSettings())

        handler.handle(sender=None)

        assert (
            _patch_success_integrations.record_success.call_args.kwargs["attempt_count"]
            == 1
        )

    def test_failure_handler_degrades_to_one_attempt_without_a_sender(
        self, _patch_failure_integrations
    ):
        """A worker-lost failure has no request to read — one attempt, not zero."""
        handler = FailureHandler(SignalHooksSettings())

        handler.handle(sender=None, task_id="task-1", exception=RuntimeError("boom"))

        assert (
            _patch_failure_integrations.record_failure.call_args.kwargs["attempt_count"]
            == 1
        )
