"""752 D4 — recording metrics is a no-op when the prometheus extra is absent.

Absence of a metrics backend is a posture, not a per-call fault. It used to
be both: the three module-scope definition modules were unimportable without
the extra, so every consumer's per-call ``from … import`` failed and warned
once per call, and the ~20 ``metrics.record_*_failed`` arms fired on top.

``NoOpMetric`` is the null object that removes the whole class of failure,
and ``BaldurMetrics.__getattr__`` is what routes the unset recorder
attributes to it — guarded on the extra actually being absent, so the ~40
in-tree capability probes keep their meaning on a configured deployment.

Absence simulation: a subprocess with ``sys.modules['prometheus_client']``
poisoned before any baldur import. The two flag-shaped alternatives are both
false greens — the definition modules choose their helper binding while
executing their module body (so a flag patched afterwards never reaches the
no-op path), and there are seven independent availability booleans, so
patching one puts the process in a state no real host has. The subprocess is
the established seam for this posture in this directory, and unlike an
in-process ``importlib.reload`` it cannot leak a half-reloaded metrics
registry into the rest of the worker.

The module-scope importability half lives in ``test_registry_no_prometheus``
alongside the ImportError contract it inverted; this file owns the
behavioral half.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
import textwrap

import pytest

_POISON_PREAMBLE = """
import sys
sys.modules['prometheus_client'] = None
import os
for _name in ('BALDUR_REDIS_URL', 'REDIS_URL', 'BALDUR_RESILIENT_STORAGE_REDIS_URL'):
    os.environ.pop(_name, None)
os.environ['BALDUR_ENVIRONMENT'] = 'development'
"""

# Every per-call failure family this decision exists to remove. Matched
# against the child's whole output, so a demotion that merely relabels the
# line does not pass either.
_FORBIDDEN_EVENT_PATTERNS = (
    r"metrics\.record_\w+_failed",
    r"retry\.\w*recording_failed",
    r"event_handler\.record_dlq_\w+_failed",
    r"metrics\.prometheus_unavailable",
    r"metrics\.up_gauge_registration_failed",
)


def _run_poisoned(snippet: str) -> subprocess.CompletedProcess:
    """Run a snippet in a subprocess where prometheus_client cannot import."""
    return subprocess.run(
        [sys.executable, "-c", _POISON_PREAMBLE + textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_clean(result: subprocess.CompletedProcess, marker: str = "OK") -> str:
    """The child succeeded and emitted no absent-extra failure line."""
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert marker in result.stdout, f"stdout={result.stdout}"

    output = result.stdout + result.stderr
    for pattern in _FORBIDDEN_EVENT_PATTERNS:
        found = re.findall(pattern, output)
        assert not found, f"{pattern} matched {found} in:\n{output}"
    return output


_PROTECTED_CALLS = """
    from baldur import protect

    def ok():
        return 1

    def boom():
        raise RuntimeError("expected")

    for _ in range(3):
        protect("d752", ok, retry=False, circuit_breaker=False, dlq=False)
    for _ in range(3):
        try:
            protect("d752", boom, retry=False, circuit_breaker=False, dlq=False)
        except RuntimeError:
            pass
"""


class TestNoOpMetricContract:
    """The null object's surface — the closure of the shapes consumers use.

    Importable with or without the extra; these cases pin the class itself
    and need no absence simulation.
    """

    def test_attribute_access_returns_the_same_singleton(self):
        from baldur.metrics.registry import NOOP_METRIC

        assert NOOP_METRIC.anything is NOOP_METRIC

    def test_an_attribute_chain_of_any_depth_resolves(self):
        """Consumers reach through recorders to collectors and labels."""
        from baldur.metrics.registry import NOOP_METRIC

        assert NOOP_METRIC.dlq.pending.labels.whatever is NOOP_METRIC

    def test_calling_the_stub_returns_the_stub(self):
        """The dominant consumer shape: resolve an attribute, then call it."""
        from baldur.metrics.registry import NOOP_METRIC

        assert NOOP_METRIC.record_resolution("d", 1, "success") is NOOP_METRIC

    def test_a_single_underscore_reach_through_is_admitted(self):
        """The DLQ pending gauge is reached as ``metrics.dlq._pending_gauge``.

        A stub that refused it would make the ``hasattr`` guard False, and
        the safe-gauge wrapper would never be built.
        """
        from baldur.metrics.registry import NOOP_METRIC

        assert NOOP_METRIC._pending_gauge is NOOP_METRIC

    @pytest.mark.parametrize(
        "dunder",
        ["__deepcopy__", "__copy__", "__wrapped__"],
        ids=["deepcopy", "copy", "inspect"],
    )
    def test_dunder_probes_get_an_honest_attribute_error(self, dunder):
        """Copy, pickle and introspection must not be answered with a stub."""
        from baldur.metrics.registry import NOOP_METRIC

        with pytest.raises(AttributeError):
            getattr(NOOP_METRIC, dunder)

    def test_the_stub_is_usable_as_a_context_manager(self):
        """One recorder method is a ``@contextmanager``; a call-only stub
        would return the stub and then fail on ``__enter__``."""
        from baldur.metrics.registry import NOOP_METRIC

        with NOOP_METRIC.http_request_timer("GET", "/x") as entered:
            assert entered is NOOP_METRIC

    def test_the_context_manager_never_suppresses_an_exception(self):
        """A swallowing stub would turn an absent extra into silent failure."""
        from baldur.metrics.registry import NOOP_METRIC

        with pytest.raises(ValueError, match="boom"), NOOP_METRIC.timer():
            raise ValueError("boom")

    def test_exit_reports_that_it_handled_nothing(self):
        from baldur.metrics.registry import NOOP_METRIC

        assert NOOP_METRIC.__exit__(None, None, None) is False

    @pytest.mark.parametrize(
        "args",
        [
            ("name", "description", ["label"]),
            ("name", "description", ["label"], [0.1, 0.5]),
        ],
        ids=["counter_gauge_signature", "histogram_signature"],
    )
    def test_the_factory_absorbs_every_helper_signature(self, args):
        """It stands in for all three ``get_or_create_*`` helpers at once."""
        from baldur.metrics.registry import NOOP_METRIC, noop_metric_factory

        assert noop_metric_factory(*args) is NOOP_METRIC


class TestBaldurMetricsCapabilityProbeContract:
    """``__getattr__`` is guarded on the extra — the guard IS the decision.

    An unguarded catch-all would answer the ~40 in-tree capability probes
    with a truthy stub on a fully-configured deployment, and would make two
    already-dead metric writes silently "succeed" instead of staying
    visibly dead.
    """

    def test_an_unknown_attribute_raises_when_prometheus_is_present(self):
        from baldur.metrics.prometheus import get_metrics

        with pytest.raises(AttributeError):
            get_metrics().definitely_not_a_recorder

    def test_capability_probes_keep_answering_no_when_prometheus_is_present(self):
        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()

        assert hasattr(metrics, "definitely_not_a_recorder") is False
        assert getattr(metrics, "definitely_not_a_recorder", None) is None

    def test_an_unset_recorder_resolves_to_the_stub_when_the_extra_is_absent(self):
        result = _run_poisoned(
            """
            from baldur.metrics.prometheus import get_metrics
            from baldur.metrics.registry import NOOP_METRIC

            metrics = get_metrics()
            assert metrics._initialized is False
            assert metrics.dlq is NOOP_METRIC, metrics.dlq
            assert getattr(metrics, 'canary', None) is NOOP_METRIC
            assert hasattr(metrics, 'retry') is True
            print('OK')
            """
        )
        _assert_clean(result)

    def test_underscore_names_stay_out_of_the_absorber_when_absent(self):
        """``_initialized`` and friends must answer honestly in both postures."""
        result = _run_poisoned(
            """
            from baldur.metrics.prometheus import get_metrics

            metrics = get_metrics()
            try:
                metrics._definitely_not_set
            except AttributeError:
                print('OK')
            else:
                print('ABSORBED')
            """
        )
        _assert_clean(result)

    def test_the_absent_extra_notice_is_a_posture_not_a_warning(self):
        """``prometheus.unavailable`` fires once, at INFO."""
        result = _run_poisoned(
            """
            from structlog.testing import capture_logs
            from baldur.metrics.prometheus import BaldurMetrics

            with capture_logs() as logs:
                BaldurMetrics()
            records = [e for e in logs if e.get('event') == 'prometheus.unavailable']
            assert len(records) == 1, records
            assert records[0]['log_level'] == 'info', records[0]
            print('OK')
            """
        )
        _assert_clean(result)

    def test_the_import_time_registry_notice_is_gone_entirely(self):
        """A demotion would still print — nothing has configured logging yet."""
        result = _run_poisoned(
            """
            import baldur.metrics.registry as registry
            assert registry.PROMETHEUS_AVAILABLE is False
            registry.ensure_up_gauge()
            print('OK')
            """
        )
        _assert_clean(result)


class TestAbsentExtraRecordingSilenceBehavior:
    """Repeated protected calls record nothing and say nothing about it."""

    def test_repeated_protected_calls_emit_no_recording_failure(self):
        """Zero, not "once" — the flood was per call, not per process."""
        result = _run_poisoned(
            _PROTECTED_CALLS
            + """
            print('OK')
            """
        )
        _assert_clean(result)

    def test_the_retry_observability_facades_resolve_instead_of_failing(self):
        """Fixing importability is what makes the warn arms unreachable."""
        result = _run_poisoned(
            """
            from baldur.services.retry_handler import observability
            assert observability is not None
            from baldur.services.metrics.recorders import record_sla_breach
            record_sla_breach('d752')
            print('OK')
            """
        )
        _assert_clean(result)

    def test_dlq_event_handlers_record_nothing_and_warn_about_nothing(self):
        """SafeGauge caches its wrapper for the process lifetime, so a broken
        one would warn on every DLQ event for the rest of the run."""
        result = _run_poisoned(
            """
            from baldur.metrics.event_handlers import (
                DLQMetricEventHandler,
                _get_safe_pending_gauge,
            )

            gauge = _get_safe_pending_gauge()
            assert gauge is not None, 'the wrapper should be built from the stub'
            DLQMetricEventHandler.on_item_created('payment', 'PG_TIMEOUT', 0.5)
            DLQMetricEventHandler.on_item_resolved('payment', 'auto_replay', 1.5)
            print('OK')
            """
        )
        _assert_clean(result)


class TestReadBackConsumersDoNotSeeTheStubBehavior:
    """A null object stands in for *recording*, never for *reading*.

    ``BaldurMetrics.__getattr__`` makes the capability probes stop
    short-circuiting, which is harmless for the ~40 consumers that only
    record: they call a no-op instead of skipping a call. One consumer reads
    a value **back** through a recorder — ``MetricSyncService`` reads the
    in-memory DLQ gauge and subtracts it from the store's actual count — and
    for that one, a stub where a number belongs is not a no-op: attribute
    access answers with the stub, the read "succeeds", and the arithmetic
    downstream raises ``TypeError: unsupported operand type(s) for -: 'int'
    and 'NoOpMetric'``, out through the admin drift-report handler.

    So the probe cannot be "does the attribute exist" for a read consumer;
    it has to be "is anything actually recording".
    """

    def test_the_drift_report_survives_an_absent_metrics_backend(self):
        """``GET /metrics/drift-report`` — the handler has no try/except."""
        result = _run_poisoned(
            """
            from baldur.services.metric_sync_service import MetricSyncService

            report = MetricSyncService().get_drift_report()
            assert 'metrics' in report, report
            print('OK')
            """
        )
        _assert_clean(result)

    def test_a_dry_run_sync_survives_an_absent_metrics_backend(self):
        """The second consumer of the same capture — ``POST /metrics/sync``."""
        result = _run_poisoned(
            """
            from baldur.services.metric_sync_service import MetricSyncService

            out = MetricSyncService().sync_metrics(dry_run=True, actor='t')
            assert out['status'] == 'dry_run', out
            print('OK')
            """
        )
        _assert_clean(result)

    def test_the_captured_gauge_state_is_empty_rather_than_stubbed(self):
        """Empty, not zero-filled: a fabricated 0 in-memory count against a
        non-zero store count would report drift that does not exist."""
        result = _run_poisoned(
            """
            from baldur.services.metric_sync_service import MetricSyncService

            captured = MetricSyncService()._capture_current_state(['payment'])
            assert captured['dlq_pending'] == {}, captured
            print('OK')
            """
        )
        _assert_clean(result)


class TestNoOpStubSatisfiesEveryRecorderProtocolContract:
    """The mechanized seal: the stub's surface is derived, never authored.

    Two consecutive design runs shipped a hand-listed stub surface and both
    missed a consumer protocol — first ``__call__``, then ``__enter__``.
    This walk reads the recorder attributes off a real ``BaldurMetrics`` and
    every public method off those recorder classes, then drives all of them
    through the absent posture. A recorder or method added later is covered
    without editing anything here.
    """

    @staticmethod
    def _derive_plan() -> list[tuple[str, str, int, bool]]:
        """(recorder attribute, method, required positional args, is a ``with``)."""
        from baldur.metrics.prometheus import get_metrics

        plan: list[tuple[str, str, int, bool]] = []
        for attr, recorder in sorted(vars(get_metrics()).items()):
            if attr.startswith("_"):
                continue
            if not type(recorder).__module__.startswith("baldur"):
                continue  # ``prefix`` is a plain str, set before the early return
            for name, fn in inspect.getmembers(type(recorder), inspect.isfunction):
                if name.startswith("_"):
                    continue
                required = sum(
                    1
                    for index, param in enumerate(
                        inspect.signature(fn).parameters.values()
                    )
                    if index > 0
                    and param.default is inspect.Parameter.empty
                    and param.kind
                    not in (param.VAR_POSITIONAL, param.VAR_KEYWORD, param.KEYWORD_ONLY)
                )
                # ``@contextmanager`` wraps a generator function; the wrapped
                # attribute is how the class itself reports that shape.
                is_context_manager = inspect.isgeneratorfunction(
                    getattr(fn, "__wrapped__", None)
                )
                plan.append((attr, name, required, is_context_manager))
        return plan

    def test_the_derivation_finds_the_recorder_surface(self):
        """Guards the walk below against silently degrading to zero cases."""
        plan = self._derive_plan()

        assert len(plan) > 100, len(plan)
        assert any(is_ctx for *_, is_ctx in plan), (
            "no @contextmanager recorder method found — the ``with`` arm of "
            "the walk would prove nothing"
        )

    def test_every_recorder_method_no_ops_silently_through_the_stub(self):
        plan = self._derive_plan()
        result = _run_poisoned(
            f"""
            import json
            from structlog.testing import capture_logs
            from baldur.metrics.prometheus import get_metrics

            plan = json.loads({json.dumps(json.dumps(plan))})
            metrics = get_metrics()  # the construction notice is not part of the walk
            failures = []
            with capture_logs() as logs:
                for attr, method, required, is_context_manager in plan:
                    call = getattr(getattr(metrics, attr), method)
                    args = [None] * required
                    try:
                        call(*args)
                    except BaseException as exc:
                        failures.append(f'{{attr}}.{{method}}() {{type(exc).__name__}}: {{exc}}')
                    if not is_context_manager:
                        continue
                    try:
                        with call(*args):
                            pass
                    except BaseException as exc:
                        failures.append(f'with {{attr}}.{{method}}() {{type(exc).__name__}}: {{exc}}')

            print('WALKED', len(plan))
            print('FAILURES', json.dumps(failures))
            print('RECORDS', json.dumps([e.get('event') for e in logs]))
            print('OK')
            """
        )
        output = _assert_clean(result)

        walked = int(re.search(r"^WALKED (\d+)$", output, re.M).group(1))
        failures = json.loads(re.search(r"^FAILURES (.*)$", output, re.M).group(1))
        records = json.loads(re.search(r"^RECORDS (.*)$", output, re.M).group(1))

        assert walked == len(plan)
        assert failures == []
        assert records == []

    def test_the_regression_shapes_that_two_authored_stubs_missed(self):
        """A call after an attribute chain, and a real ``with`` block."""
        result = _run_poisoned(
            """
            from baldur.metrics.prometheus import get_metrics

            get_metrics().retry.record_resolution('d', 1, 'success')
            with get_metrics().infra.http_request_timer('GET', '/x'):
                pass
            try:
                with get_metrics().infra.http_request_timer('GET', '/x'):
                    raise ValueError('boom')
            except ValueError:
                print('PROPAGATES')
            print('OK')
            """
        )
        output = _assert_clean(result)

        assert "PROPAGATES" in output


class TestProtectRecorderStickyLevelBehavior:
    """An absent extra is a posture; any other construction fault is not.

    The sticky latch is unchanged in both arms — only which level announces
    it moves, so a genuinely broken recorder keeps its WARNING.
    """

    @pytest.fixture(autouse=True)
    def reset_recorder(self):
        from baldur.metrics.recorders.protect import reset_protect_recorder

        reset_protect_recorder()
        yield
        reset_protect_recorder()

    @pytest.mark.parametrize(
        ("error", "expected_level"),
        [
            (ImportError("prometheus_client missing"), "debug"),
            (RuntimeError("collector registry corrupt"), "warning"),
        ],
        ids=["absent_extra", "real_fault"],
    )
    def test_construction_failure_level_splits_on_the_failure_kind(
        self, error, expected_level
    ):
        from unittest.mock import patch

        from structlog.testing import capture_logs

        from baldur.metrics.recorders.protect import get_protect_recorder

        with (
            patch(
                "baldur.metrics.recorders.protect.ProtectMetricRecorder",
                side_effect=error,
            ),
            capture_logs() as logs,
        ):
            first = get_protect_recorder()
            second = get_protect_recorder()

        records = [
            entry
            for entry in logs
            if entry.get("event") == "metrics.protect_recorder_unavailable_sticky"
        ]
        assert first is None
        assert second is None
        assert len(records) == 1, "the latch must keep it to one announcement"
        assert records[0]["log_level"] == expected_level


class TestBackpressureMetricsPostureBehavior:
    """``scaling/metrics.py`` carries its own ``find_spec`` availability flag.

    A registry-scoped no-op never reaches it, so this is the one absent-extra
    site whose seam is the module's own flag rather than the import poison.
    """

    def test_absent_extra_construction_reports_at_debug(self):
        from unittest.mock import patch

        from structlog.testing import capture_logs

        from baldur.scaling.metrics import BackpressureMetrics

        with (
            patch("baldur.scaling.metrics.HAS_PROMETHEUS", False),
            capture_logs() as logs,
        ):
            BackpressureMetrics()

        records = [
            entry
            for entry in logs
            if entry.get("event") == "backpressure_metrics.prometheus_unavailable"
        ]
        assert len(records) == 1
        assert records[0]["log_level"] == "debug"
