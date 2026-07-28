"""Celery terminals write the task layer's series, never the protected call's.

A Baldur-protected call inside a Celery task resolves twice: the inner retry
policy terminates at its own choke point, and the task terminates at the
``task_success`` / ``task_failure`` signal. Both records are correct at their
own layer, but they used to share ``baldur_retry_attempts_distribution`` — one
denominator for two populations — so the mean the retry-pressure alert reads
was diluted by up to 2x on exactly the shape the framework recommends.

The terminals now write ``baldur_task_attempts_distribution`` /
``baldur_task_outcomes_total``. These tests assert the re-routing from the
series' own side against a live registry rather than through a patched facade:
patching ``record_retry_resolution`` and asserting it was not called would pass
even if the recorder stopped recording altogether, since the symbol is no
longer imported by this module at all.

The forwarding itself (the terminal plumbs the real attempt count into the
facade) is covered next door in ``test_signal_metric_attempts.py``; what lives
here is the destination and the un-dilution regression.
"""

from __future__ import annotations

import itertools

import pytest

from baldur.adapters.celery.integrations.metric_recorder import MetricRecorder
from baldur.adapters.celery.signal_config import SignalHooksSettings

_TASK_NAME = "app.tasks.do_work"

# Both layers reach the recorder through the module facade, which collapses any
# unregistered domain to the shared fallback label. Registering a domain nothing
# else in this worker uses keeps every read below an absolute value.
_DOMAIN_SEQUENCE = itertools.count()


def _registered_domain(prefix: str) -> str:
    """Register and return a domain no other test has recorded under."""
    from baldur.metrics.registry import register_domain

    domain = f"{prefix}_{next(_DOMAIN_SEQUENCE)}"
    assert register_domain(domain) is True, (
        "domain registration hit the cardinality limit — the assertions below "
        "would silently read the shared fallback label instead"
    )
    return domain


def _sample(name: str, **labels: str) -> float | None:
    """Read one prometheus sample, or None when that series was never recorded."""
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels)


def _recorder_for(domain: str) -> MetricRecorder:
    """A ``MetricRecorder`` whose task name maps explicitly onto ``domain``.

    ``record_success`` derives the domain from the task name while
    ``record_failure`` takes it directly, so the explicit mapping is what lets
    both terminals be driven against the same series.
    """
    return MetricRecorder(SignalHooksSettings(task_domain_mapping={_TASK_NAME: domain}))


class TestTaskLayerMetricsBehavior:
    """The Celery terminals' destination, and the dilution they no longer cause."""

    def test_record_success_does_not_record_retry_resolution(self):
        """A task success lands on the task pair, and only there."""
        domain = _registered_domain("task_success")

        _recorder_for(domain).record_success("svc", _TASK_NAME, attempt_count=3)

        assert (
            _sample(
                "baldur_task_attempts_distribution_count",
                domain=domain,
                is_synthetic="false",
            )
            == 1.0
        )
        assert (
            _sample(
                "baldur_task_outcomes_total",
                domain=domain,
                outcome="success",
                is_synthetic="false",
            )
            == 1.0
        )
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

    def test_record_failure_does_not_record_retry_resolution(self):
        """A task failure lands on the task pair, and only there."""
        domain = _registered_domain("task_failure")

        _recorder_for(domain).record_failure(
            domain, _TASK_NAME, RuntimeError("boom"), attempt_count=4
        )

        assert (
            _sample(
                "baldur_task_attempts_distribution_count",
                domain=domain,
                is_synthetic="false",
            )
            == 1.0
        )
        assert (
            _sample(
                "baldur_task_outcomes_total",
                domain=domain,
                outcome="failure",
                is_synthetic="false",
            )
            == 1.0
        )
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

    def test_terminals_plumb_the_real_attempt_count_onto_the_task_histogram(self):
        """A task that took 6 attempts observes 6, not the terminal count of 1."""
        domain = _registered_domain("task_plumbing")

        _recorder_for(domain).record_failure(
            domain, _TASK_NAME, RuntimeError("boom"), attempt_count=6
        )

        assert (
            _sample(
                "baldur_task_attempts_distribution_sum",
                domain=domain,
                is_synthetic="false",
            )
            == 6.0
        )

    @pytest.mark.parametrize(
        ("drive_terminal", "task_outcome"),
        [
            (
                lambda recorder, domain: recorder.record_success(
                    "svc", _TASK_NAME, attempt_count=6
                ),
                "success",
            ),
            (
                lambda recorder, domain: recorder.record_failure(
                    domain, _TASK_NAME, RuntimeError("boom"), attempt_count=6
                ),
                "failure",
            ),
        ],
        ids=["success_terminal", "failure_terminal"],
    )
    def test_task_terminal_causes_no_dilution_of_the_retry_histogram(
        self, drive_terminal, task_outcome
    ):
        """The regression that would have failed before the layers were split.

        One protected call exhausts after 3 attempts and one Celery task
        resolves after 6, for the same domain. The retry histogram must read
        exactly one observation of 3 — a mean of 3.0, the true inner pressure —
        rather than two observations averaging 4.5. That mean is the numerator
        of the shipped retry-pressure alert, so a second writer here does not
        break anything loudly; it just moves the alert's firing point.
        """
        # Given a domain whose protected call exhausted after 3 attempts
        from baldur.services.retry_handler.observability import record_retry_outcome

        domain = _registered_domain("dilution")
        record_retry_outcome(domain, 3, "exhausted")

        # When the surrounding Celery task also reaches its terminal, at 6
        drive_terminal(_recorder_for(domain), domain)

        # Then the retry series carries the inner resolution and nothing else
        assert (
            _sample(
                "baldur_retry_attempts_distribution_count",
                domain=domain,
                is_synthetic="false",
            )
            == 1.0
        )
        assert (
            _sample(
                "baldur_retry_attempts_distribution_sum",
                domain=domain,
                is_synthetic="false",
            )
            == 3.0
        )
        assert (
            _sample(
                "baldur_retry_outcomes_total",
                domain=domain,
                outcome="exhausted",
                is_synthetic="false",
            )
            == 1.0
        )

        # And the task series carries the task's own resolution, undiluted
        assert (
            _sample(
                "baldur_task_attempts_distribution_count",
                domain=domain,
                is_synthetic="false",
            )
            == 1.0
        )
        assert (
            _sample(
                "baldur_task_attempts_distribution_sum",
                domain=domain,
                is_synthetic="false",
            )
            == 6.0
        )
        assert (
            _sample(
                "baldur_task_outcomes_total",
                domain=domain,
                outcome=task_outcome,
                is_synthetic="false",
            )
            == 1.0
        )
