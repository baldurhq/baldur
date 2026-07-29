"""Celery domain registration at the domain-CONSUMING sites.

The Celery adapter ships its own domain vocabulary — eight keyword patterns
extracted from task names — and before these sites existed seven of those eight
were domains Baldur's own registry rejected: a user who changed nothing got
their tasks collapsed into the fallback label.

Registration deliberately does not live inside ``extract_domain_from_task_name``,
which is reused as a circuit-breaker service-name fallback, but at each site
that consumes the value AS a metric or DLQ domain, before the first consumption
in the same invocation.

Reference:
    src/baldur/adapters/celery/integrations/metric_recorder.py
    src/baldur/adapters/celery/handlers/failure_handler.py
    src/baldur/adapters/celery/baldur_task.py
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from baldur.adapters.celery.baldur_task import baldur_task
from baldur.adapters.celery.handlers.failure_handler import FailureHandler
from baldur.adapters.celery.integrations.metric_recorder import MetricRecorder
from baldur.adapters.celery.signal_config import (
    _DEFAULT_DOMAIN_PATTERNS,
    SignalHooksSettings,
)
from baldur.metrics.registry import (
    _registered_domains,
    get_registered_domains,
    register_domain,
    reset_registered_domains,
    resolve_domain_label,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry, its memos and its cap cache are process-global."""
    original = _registered_domains.copy()
    reset_registered_domains()
    yield
    reset_registered_domains()
    _registered_domains.clear()
    _registered_domains.update(original)


def _make_sender(
    name: str = "app.tasks.do_work",
    max_retries: int | None = 3,
    retries: int = 3,
) -> SimpleNamespace:
    """Stand-in Celery task sender — the handler reads three attributes.

    A namespace rather than a mock: every read is a plain ``getattr``, so a
    spec-less mock would only add the risk of a typo'd attribute passing
    silently.
    """
    return SimpleNamespace(
        name=name,
        max_retries=max_retries,
        request=SimpleNamespace(retries=retries),
    )


# =============================================================================
# Behavior — shipped defaults survive to a label
# =============================================================================


class TestShippedDomainVocabularyBehavior:
    """Every domain Baldur's own defaults and examples name reaches a label.

    Following Baldur's own defaults must not produce domains Baldur's own
    registry rejects — that self-contradiction is what this covers.
    """

    @pytest.mark.parametrize(
        "domain",
        [
            *sorted(_DEFAULT_DOMAIN_PATTERNS),
            # ``@domain_tag`` and ``@baldur_task`` docstring examples.
            "payment",
            "order",
        ],
    )
    def test_shipped_domain_is_admitted_and_self_resolving(self, domain):
        """Admission succeeds and the label equals the declared name."""
        assert register_domain(domain) is True
        assert resolve_domain_label(domain) == domain

    def test_default_pattern_key_count_is_covered(self):
        """The parametrization tracks the shipped table, not a copy of it."""
        assert len(_DEFAULT_DOMAIN_PATTERNS) == 8


# =============================================================================
# Behavior — MetricRecorder methods
# =============================================================================


class TestCeleryMetricRecorderRegistrationBehavior:
    """Each recorder method registers the domain it is about to record.

    Registering inside the recorder method — rather than in each caller —
    is what covers every caller of it, including the retry handler.
    """

    @pytest.fixture
    def recorder(self):
        return MetricRecorder(SignalHooksSettings())

    def test_record_failure_registers_before_recording(self, recorder):
        """Ordering: the slot is claimed before the metric write, not after."""
        seen_at_record_time: list[list[str]] = []

        def _capture(**kwargs):
            seen_at_record_time.append(get_registered_domains())

        with patch(
            "baldur.services.metrics.recorders.record_task_attempt",
            autospec=True,
            side_effect=_capture,
        ) as mock_record:
            recorder.record_failure(
                domain="payment",
                task_name="app.tasks.charge",
                exception=RuntimeError("boom"),
            )

        mock_record.assert_called_once_with(
            domain="payment", attempt_count=1, outcome="failure"
        )
        assert "payment" in seen_at_record_time[0]

    def test_record_success_registers_the_derived_domain(self, recorder):
        """``record_success`` derives its own domain from the task name."""
        with patch(
            "baldur.services.metrics.recorders.record_task_attempt",
            autospec=True,
        ) as mock_record:
            recorder.record_success(
                service_name="order_service", task_name="myapp.tasks.process_order"
            )

        mock_record.assert_called_once_with(
            domain="order", attempt_count=1, outcome="success"
        )
        assert "order" in get_registered_domains()

    def test_record_retry_registers_before_recording(self, recorder):
        """The retry marker's caller needs no registration of its own."""
        seen_at_record_time: list[list[str]] = []

        def _capture(**kwargs):
            seen_at_record_time.append(get_registered_domains())

        with patch(
            "baldur.services.metrics.recorders.record_retry_marker",
            autospec=True,
            side_effect=_capture,
        ) as mock_record:
            recorder.record_retry(domain="inventory", task_name="app.tasks.restock")

        mock_record.assert_called_once_with(domain="inventory")
        assert "inventory" in seen_at_record_time[0]

    def test_unmatched_task_fallback_literal_is_not_registered(self, recorder):
        """Negative assertion: ``unknown`` must stay the unclassified bucket.

        A single-segment task name falls back to the literal ``unknown``, which
        is also what a blank domain sanitizes to — registering it would merge
        unrelated traffic from both channels into one series.
        """
        with patch(
            "baldur.services.metrics.recorders.record_task_attempt",
            autospec=True,
        ):
            recorder.record_success(service_name="svc", task_name="standalone")

        assert "unknown" not in get_registered_domains()
        assert resolve_domain_label("unknown") == "OTHER_DOMAIN"


# =============================================================================
# Behavior — FailureHandler ordering
# =============================================================================


class TestCeleryFailureHandlerRegistrationBehavior:
    """Registration precedes the DLQ store, not just the metrics step.

    This handler consumes the domain in its DLQ store (step 2) BEFORE its
    metrics step (step 3), so registering at the metrics step would leave the
    DLQ family's first record on the fallback label.
    """

    @pytest.fixture
    def _patch_integrations(self):
        with (
            patch(
                "baldur.adapters.celery.handlers.failure_handler.CircuitBreakerRecorder",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.handlers.failure_handler.DLQRecorder",
                autospec=True,
            ) as mock_dlq_cls,
            patch(
                "baldur.adapters.celery.handlers.failure_handler.MetricRecorder",
                autospec=True,
            ) as mock_metric_cls,
            patch(
                "baldur.adapters.celery.handlers.failure_handler.ForensicCapture",
                autospec=True,
            ),
        ):
            yield {"dlq_cls": mock_dlq_cls, "metric_cls": mock_metric_cls}

    def test_domain_is_registered_before_the_dlq_store_step(self, _patch_integrations):
        """The same-invocation guarantee holds from the FIRST consumption."""
        seen_at_store_time: list[list[str]] = []
        _patch_integrations["dlq_cls"].return_value.store.side_effect = (
            lambda **kwargs: seen_at_store_time.append(get_registered_domains())
        )

        handler = FailureHandler(SignalHooksSettings())
        handler.handle(
            sender=_make_sender(name="myapp.tasks.process_order"),
            task_id="task-1",
            exception=RuntimeError("fail"),
        )

        _patch_integrations["dlq_cls"].return_value.store.assert_called_once()
        assert "order" in seen_at_store_time[0]

    def test_registered_domain_is_the_one_the_store_receives(self, _patch_integrations):
        """One value, one label — the store key and the registry agree."""
        handler = FailureHandler(SignalHooksSettings())
        handler.handle(
            sender=_make_sender(name="myapp.tasks.process_order"),
            task_id="task-1",
            exception=RuntimeError("fail"),
        )

        stored_domain = _patch_integrations[
            "dlq_cls"
        ].return_value.store.call_args.kwargs["domain"]
        assert stored_domain == "order"
        assert resolve_domain_label(stored_domain) == "order"


# =============================================================================
# Behavior — @baldur_task track_dlq branch
# =============================================================================


class TestBaldurTaskRegistrationBehavior:
    """Registration is inside the ``track_dlq`` branch, not at resolution.

    With ``track_dlq=False`` the resolved value is consumed only as a
    circuit-breaker service name, where it is not a metric domain and a slot
    would be burned for nothing.
    """

    @pytest.fixture
    def _patch_recorders(self):
        with (
            patch(
                "baldur.adapters.celery.baldur_task.CircuitBreakerRecorder",
                autospec=True,
            ),
            patch(
                "baldur.adapters.celery.baldur_task.DLQRecorder", autospec=True
            ) as mock_dlq_cls,
        ):
            yield mock_dlq_cls

    def test_track_dlq_branch_registers_the_resolved_domain(self, _patch_recorders):
        """A tracked failure claims the slot before the DLQ store."""

        @baldur_task(domain="payment", track_dlq=True)
        def charge():
            raise RuntimeError("gateway down")

        with pytest.raises(RuntimeError, match="gateway down"):
            charge()

        assert "payment" in get_registered_domains()
        _patch_recorders.return_value.store.assert_called_once()

    def test_track_dlq_disabled_registers_nothing(self, _patch_recorders):
        """Negative assertion: a CB-only task burns no cap slot."""
        before = get_registered_domains()

        @baldur_task(domain="payment", track_dlq=False)
        def charge():
            raise RuntimeError("gateway down")

        with pytest.raises(RuntimeError, match="gateway down"):
            charge()

        assert get_registered_domains() == before
        _patch_recorders.return_value.store.assert_not_called()

    def test_successful_task_registers_nothing(self, _patch_recorders):
        """The registration site is on the failure path only."""
        before = get_registered_domains()

        @baldur_task(domain="payment", track_dlq=True)
        def charge():
            return "ok"

        assert charge() == "ok"
        assert get_registered_domains() == before

    def test_registry_failure_does_not_mask_the_business_exception(
        self, _patch_recorders
    ):
        """The call sits inside an ``except`` block ahead of the re-raise.

        A raising registry boundary here would swap the caller's exception for
        an unrelated one AND kill the DLQ store that follows it, which is why
        ``register_domain`` is total.
        """
        with patch(
            "baldur.metrics.registry._admission_refusal_reason",
            autospec=True,
            side_effect=RuntimeError("registry exploded"),
        ):

            @baldur_task(domain="payment", track_dlq=True)
            def charge():
                raise ValueError("gateway down")

            with pytest.raises(ValueError, match="gateway down"):
                charge()

        _patch_recorders.return_value.store.assert_called_once()
