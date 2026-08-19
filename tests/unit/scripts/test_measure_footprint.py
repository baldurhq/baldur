"""Unit tests for the footprint self-measurement probe.

Test plan source: 760 `## Test Assessment`.

The probe's whole reason to exist is that ``init()`` returns while background
threads are still starting, so the reading taken there is a transient peak
rather than a resident cost. Every test below is anchored to some part of that
claim: which sample the settle rule accepts, how a shrinking delta renders,
and whether the report text survives the console it is printed on.
"""

from __future__ import annotations

import ast
import inspect
import os
import platform
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psutil
import pytest

from baldur import ConfigurationError
from baldur.scripts import measure_footprint as mf

# =============================================================================
# Helpers
# =============================================================================

_LEGACY_CONSOLE_ENCODING = "cp949"

# main() defaults this one variable and imposes nothing else. The name is the
# contract: production spells it inline, so there is no source constant to
# reference.
_ADMIN_PORT_VAR = "BALDUR_ADMIN_PORT"
_EXPLICIT_ADMIN_PORT = "9999"


class _SettleStillRunning(Exception):
    """Raised by a bounded sleep stub to stop a settle loop that has not settled."""


def _encodable_on_a_legacy_console(text: str) -> bool:
    """Whether ``text`` survives a non-UTF-8 console code page."""
    try:
        text.encode(_LEGACY_CONSOLE_ENCODING)
    except UnicodeEncodeError:
        return False
    return True


def _sample(
    *,
    label: str = "settled",
    elapsed: float = 12.0,
    rss_mb: float = 124.0,
    threads: int = 30,
    thread_names: tuple[str, ...] = ("MainThread",),
    cpu: float = 2.16,
) -> mf.Sample:
    """Build a Sample without reading a real process.

    RSS is given in MB and converted through the module's own constant, so a
    test can state the number it means rather than a byte count.
    """
    return mf.Sample(
        label=label,
        elapsed_seconds=elapsed,
        rss_bytes=int(rss_mb * mf._BYTES_PER_MB),
        num_threads=threads,
        thread_names=thread_names,
        cpu_seconds=cpu,
    )


def _stub_process(
    *,
    rss_bytes: float = 124.0 * mf._BYTES_PER_MB,
    user: float = 2.0,
    system: float = 0.16,
    num_threads: int | list[int] = 30,
) -> MagicMock:
    """A psutil.Process double.

    psutil reads the OS process table, which is both I/O-crossing and
    nondeterministic, so it is a sanctioned mock boundary; the production
    signature takes the process as a parameter for exactly this reason.

    ``num_threads`` accepts a list, wired as ``side_effect`` rather than
    ``return_value``: an exhausted sequence raises instead of letting a loop
    that should have stopped spin forever.
    """
    process = MagicMock(spec=psutil.Process)
    process.memory_info.return_value = SimpleNamespace(rss=rss_bytes)
    process.cpu_times.return_value = SimpleNamespace(user=user, system=system)
    if isinstance(num_threads, list):
        process.num_threads.side_effect = list(num_threads)
    else:
        process.num_threads.return_value = num_threads
    return process


# =============================================================================
# settled_index
# =============================================================================


class TestSettledIndexBehavior:
    """The rule that decides which sample may be quoted."""

    def test_settled_index_rising_then_stable_thread_count_returns_the_stable_sample(
        self,
    ):
        """The defect this exists to catch: accepting a sample while threads still arrive.

        A predicate that only checked the elapsed floor would return index 1
        here and quote a reading taken four threads short of the inventory.
        """
        # Given - the measured shape: the count climbs, then holds
        samples = [
            _sample(elapsed=12.0, threads=26),
            _sample(elapsed=14.0, threads=28),
            _sample(elapsed=16.0, threads=30),
            _sample(elapsed=18.0, threads=30),
            _sample(elapsed=20.0, threads=30),
        ]

        # When
        index = mf.settled_index(samples, min_elapsed_seconds=10.0)

        # Then - the first sample whose predecessor agrees, not the first past the floor
        assert index == 3

    @pytest.mark.parametrize(
        ("last_elapsed", "expected"),
        [(9.9, None), (10.0, 2), (10.1, 2)],
        ids=["below_floor", "at_floor", "above_floor"],
    )
    def test_settled_index_elapsed_floor_boundary_decides_citability(
        self, last_elapsed, expected
    ):
        """The floor is inclusive: a sample taken exactly at it is citable."""
        samples = [
            _sample(elapsed=1.0, threads=30),
            _sample(elapsed=5.0, threads=30),
            _sample(elapsed=last_elapsed, threads=30),
        ]

        assert mf.settled_index(samples, min_elapsed_seconds=10.0) == expected

    def test_settled_index_thread_count_still_rising_returns_none(self):
        """No pair ever agrees, so there is nothing to quote."""
        samples = [
            _sample(elapsed=12.0, threads=26),
            _sample(elapsed=14.0, threads=28),
            _sample(elapsed=16.0, threads=30),
        ]

        assert mf.settled_index(samples, min_elapsed_seconds=10.0) is None

    def test_settled_index_empty_sample_list_returns_none(self):
        """Nothing was measured, so nothing settled."""
        assert mf.settled_index([], min_elapsed_seconds=10.0) is None

    def test_settled_index_single_sample_has_no_predecessor_to_agree_with(self):
        """One reading cannot be stable: stability is a property of a pair."""
        assert (
            mf.settled_index([_sample(elapsed=99.0)], min_elapsed_seconds=10.0) is None
        )


# =============================================================================
# layer_delta
# =============================================================================


class TestLayerDeltaBehavior:
    """What one stage costs on top of the stage before it."""

    def test_layer_delta_reports_growth_between_two_samples(self):
        """The init() stage against the import stage, in the measured shape."""
        before = _sample(rss_mb=19.0, threads=4, cpu=0.09, thread_names=("MainThread",))
        after = _sample(
            rss_mb=122.6,
            threads=26,
            cpu=2.08,
            thread_names=("MainThread", "Scheduler-scheduler"),
        )

        delta = mf.layer_delta(before, after)

        assert delta["rss_mb"] == pytest.approx(103.6)
        assert delta["threads"] == 22
        assert delta["cpu_seconds"] == pytest.approx(1.99)
        assert delta["new_threads"] == ("Scheduler-scheduler",)

    def test_layer_delta_settling_below_the_peak_reports_a_negative_rss_delta(self):
        """The peak/settle inversion is a real reading, not an error to clamp away.

        Measured on the authoring host: RSS falls from the init()-return peak
        to the settled figure while the thread count is still growing.
        """
        peak = _sample(rss_mb=158.9, threads=26, cpu=2.08)
        settled = _sample(rss_mb=128.0, threads=30, cpu=2.16)

        delta = mf.layer_delta(peak, settled)

        assert delta["rss_mb"] == pytest.approx(-30.9)
        assert delta["threads"] == 4

    def test_layer_delta_identical_samples_report_no_change(self):
        """Nothing moved between the two readings."""
        sample = _sample(thread_names=("MainThread", "l2_sync_0"))

        assert mf.layer_delta(sample, sample) == {
            "rss_mb": 0.0,
            "threads": 0,
            "cpu_seconds": 0.0,
            "new_threads": (),
        }

    def test_layer_delta_new_threads_lists_only_the_arrivals(self):
        """Not the whole inventory, and not the threads that went away."""
        before = _sample(thread_names=("MainThread", "Thread-1"))
        after = _sample(thread_names=("MainThread", "l2_sync_0", "l2_sync_1"))

        delta = mf.layer_delta(before, after)

        assert delta["new_threads"] == ("l2_sync_0", "l2_sync_1")


# =============================================================================
# _settle
# =============================================================================


class TestSettleLoopBehavior:
    """The sampling loop that turns the settle rule into a measurement."""

    def test_settle_returns_the_first_sample_whose_thread_count_repeats(self):
        """The init sample seeds the list so the first candidate has a predecessor."""
        # Given - an origin far enough back that every sample clears the floor
        process = _stub_process(num_threads=[28, 30, 30])
        init_sample = _sample(label=mf._INIT_LABEL, elapsed=2.0, threads=26)
        slept: list[float] = []

        # When
        samples, index = mf._settle(
            process,
            origin=time.monotonic() - 20.0,
            init_sample=init_sample,
            sleep=slept.append,
        )

        # Then
        assert samples[0] is init_sample
        assert [s.num_threads for s in samples] == [26, 28, 30, 30]
        assert index == 3
        assert slept == [mf._SETTLE_SAMPLE_INTERVAL_SECONDS] * 3

    def test_settle_gives_up_when_the_thread_count_never_repeats(self):
        """Past the deadline the loop reports no citable sample rather than a moving one."""
        process = _stub_process(num_threads=[28, 30, 32])
        init_sample = _sample(label=mf._INIT_LABEL, elapsed=2.0, threads=26)

        samples, index = mf._settle(
            process,
            origin=time.monotonic() - 100.0,
            init_sample=init_sample,
            sleep=lambda _seconds: None,
        )

        assert index is None
        assert len(samples) == 2

    def test_settle_measures_its_floor_from_the_init_sample_not_from_process_start(
        self,
    ):
        """A repeat 14 s into the process is only 6 s after init() returned.

        Both the floor and the deadline are relative to init()'s return, so a
        run that has not reached the floor has no exit at all - the injected
        sleep bounds it, and reaching that bound IS the assertion that nothing
        was accepted early. Were the floor absolute, the first pair would be
        returned after a single sleep and no bound would ever be hit.
        """
        process = _stub_process(num_threads=[30, 30, 30])
        init_sample = _sample(label=mf._INIT_LABEL, elapsed=8.0, threads=30)
        slept: list[float] = []

        def bounded_sleep(seconds: float) -> None:
            slept.append(seconds)
            if len(slept) == 3:
                raise _SettleStillRunning

        with pytest.raises(_SettleStillRunning):
            mf._settle(
                process,
                origin=time.monotonic() - 14.0,
                init_sample=init_sample,
                sleep=bounded_sleep,
            )


# =============================================================================
# collect_sample
# =============================================================================


class TestCollectSampleBehavior:
    """One observation of the process, read in a single pass."""

    def test_collect_sample_carries_the_label_and_elapsed_it_was_given(self):
        sample = mf.collect_sample(_stub_process(), "settled", 12.2)

        assert sample.label == "settled"
        assert sample.elapsed_seconds == 12.2

    def test_collect_sample_truncates_a_fractional_rss_to_whole_bytes(self):
        """psutil reports RSS as a number; the Sample field is an int count of bytes."""
        sample = mf.collect_sample(_stub_process(rss_bytes=1234.9), "settled", 1.0)

        assert sample.rss_bytes == 1234

    def test_collect_sample_cpu_seconds_is_user_plus_system(self):
        """Processor time means both halves - a user-only reading understates it."""
        sample = mf.collect_sample(_stub_process(user=1.5, system=0.25), "settled", 1.0)

        assert sample.cpu_seconds == pytest.approx(1.75)

    def test_collect_sample_thread_count_comes_from_the_process_not_the_name_list(self):
        """The two are never interchangeable: names cover threading-created threads only."""
        sample = mf.collect_sample(_stub_process(num_threads=42), "settled", 1.0)

        assert sample.num_threads == 42
        assert len(sample.thread_names) != 42

    def test_collect_sample_thread_names_are_sorted(self):
        """Unsorted, the report's thread list reorders between runs for no reason.

        A live thread whose name sorts ahead of every other pins this:
        threading.enumerate() yields the main thread first, so an unsorted
        tuple would not start with this one.
        """
        # Given - a thread that sorts before MainThread and every library name
        release = threading.Event()
        probe = threading.Thread(target=release.wait, name="!sorts_first", daemon=True)
        probe.start()

        # When
        try:
            sample = mf.collect_sample(_stub_process(), "settled", 1.0)
        finally:
            release.set()
            probe.join(timeout=5.0)

        # Then
        assert sample.thread_names[0] == "!sorts_first"
        assert list(sample.thread_names) == sorted(sample.thread_names)

    def test_collect_sample_reads_each_counter_exactly_once(self):
        """One pass, so the probe's own cost stays below what it measures."""
        process = _stub_process()

        mf.collect_sample(process, "settled", 1.0)

        process.memory_info.assert_called_once_with()
        process.cpu_times.assert_called_once_with()
        process.num_threads.assert_called_once_with()


# =============================================================================
# collect_posture
# =============================================================================


class TestCollectPostureBehavior:
    """The echo that says which code paths the numbers include, and on what host."""

    @staticmethod
    def _posture_with(runtime: dict) -> dict[str, str]:
        """Serve a chosen runtime posture - a process-global derivation."""
        with patch("baldur.bootstrap.get_runtime_posture", return_value=runtime):
            return mf.collect_posture()

    def test_collect_posture_redis_storage_reports_redis_as_configured(self):
        posture = self._posture_with({"storage": "redis", "metrics": "prometheus"})

        assert posture["storage backend"] == "redis"
        assert posture["redis"] == "configured"

    def test_collect_posture_memory_storage_reports_redis_as_not_configured(self):
        """The reading a shared-storage deployment would not produce."""
        posture = self._posture_with({"storage": "memory", "metrics": "disabled"})

        assert posture["storage backend"] == "memory"
        assert posture["redis"] == "not configured"

    def test_collect_posture_missing_runtime_keys_report_unknown(self):
        """An unrecognised posture says so instead of implying a backend."""
        posture = self._posture_with({})

        assert posture["storage backend"] == "unknown"
        assert posture["metrics backend"] == "unknown"
        assert posture["redis"] == "not configured"

    def test_collect_posture_unset_environment_is_reported_as_unset(self, monkeypatch):
        monkeypatch.delenv("BALDUR_ENVIRONMENT", raising=False)

        assert self._posture_with({"storage": "memory"})["environment"] == "(unset)"

    def test_collect_posture_reports_the_environment_variable_when_set(
        self, monkeypatch
    ):
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "staging")

        assert self._posture_with({"storage": "memory"})["environment"] == "staging"

    def test_collect_posture_carries_the_host_axes_that_decide_comparability(self):
        """RSS does not transfer across OS or interpreter version, so the echo says both."""
        posture = self._posture_with({"storage": "memory"})

        assert posture["python"] == platform.python_version()
        assert posture["cpu count"] == str(os.cpu_count())
        assert platform.system() in posture["os"]


# =============================================================================
# format_posture / format_stage
# =============================================================================


class TestFormatPostureBehavior:
    """The posture echo, rendered as aligned key/value lines."""

    def test_format_posture_pads_every_key_to_the_longest(self):
        """Both values start at the same column: two-space indent, the 15-character
        longest key, then a two-space separator."""
        lines = mf.format_posture({"os": "linux", "storage backend": "memory"})

        assert lines == [
            "  os" + " " * 15 + "linux",
            "  storage backend  memory",
        ]

    def test_format_posture_single_entry_uses_the_separator_and_no_padding(self):
        assert mf.format_posture({"python": "3.12.6"}) == ["  python  3.12.6"]

    def test_format_posture_empty_posture_returns_no_lines(self):
        """The width is computed over the keys, so an empty echo must not raise."""
        assert mf.format_posture({}) == []


class TestFormatStageBehavior:
    """One stage: its absolute reading, then what it cost over the stage before."""

    def test_format_stage_without_a_previous_sample_reports_the_reading_alone(self):
        """The interpreter baseline has nothing to be a delta against."""
        lines = mf.format_stage(
            _sample(label="settled", elapsed=12.2, rss_mb=124.0, threads=30, cpu=2.16),
            None,
        )

        assert lines == [
            "  settled  (+12.2s)",
            "      RSS    124.0 MB   threads  30   CPU   2.16 s",
        ]

    def test_format_stage_renders_a_growing_rss_delta_with_a_plus_sign(self):
        lines = mf.format_stage(
            _sample(rss_mb=122.6, threads=26, cpu=2.08),
            _sample(rss_mb=19.0, threads=4, cpu=0.09),
        )

        assert lines[2] == "      delta   +103.6 MB   threads +22   CPU  +1.99 s"

    def test_format_stage_renders_a_shrinking_rss_delta_with_a_minus_sign(self):
        """The settle-below-peak reading must not render as growth."""
        lines = mf.format_stage(
            _sample(rss_mb=128.0, threads=30, cpu=2.16),
            _sample(rss_mb=158.9, threads=26, cpu=2.08),
        )

        assert lines[2] == "      delta    -30.9 MB   threads  +4   CPU  +0.08 s"

    def test_format_stage_with_no_new_threads_omits_the_thread_name_line(self):
        """An empty arrival list prints nothing, rather than an empty label."""
        before = _sample(threads=30, thread_names=("MainThread",))
        after = _sample(threads=30, thread_names=("MainThread",))

        lines = mf.format_stage(after, before)

        assert len(lines) == 3
        assert not any("new threads" in line for line in lines)

    def test_format_stage_lists_the_threads_that_arrived_since_the_previous_sample(
        self,
    ):
        before = _sample(thread_names=("MainThread",))
        after = _sample(thread_names=("MainThread", "l2_sync_0", "l2_sync_1"))

        lines = mf.format_stage(after, before)

        assert lines[-1] == "      new threads: l2_sync_0, l2_sync_1"


# =============================================================================
# main - exit routing
# =============================================================================


class TestMainExitRoutingBehavior:
    """Which of main()'s three exits is taken, and what it says on the way out.

    The measurement is covered by the helper tests above and end to end by the
    subprocess smoke, which only ever walks the exit-0 path. What nothing else
    reaches is the routing between the exits. ``baldur.init`` is patched to a
    no-op here, so no daemon thread and no outbox worker start - the leak that
    forces the smoke into a subprocess does not apply, and these stay in-suite.
    """

    @pytest.fixture
    def probe_env(self, monkeypatch):
        """Pin the admin port and serve a stub process to main().

        main() writes ``BALDUR_ADMIN_PORT`` into ``os.environ`` on its first
        line; setting it here hands the key to monkeypatch so it cannot survive
        into the next test on this xdist worker. psutil reads the OS process
        table - the same I/O-crossing boundary the helper tests stub.
        """
        monkeypatch.setenv(_ADMIN_PORT_VAR, _EXPLICIT_ADMIN_PORT)
        with patch("psutil.Process", return_value=_stub_process()):
            yield

    @pytest.fixture
    def never_settles(self, monkeypatch):
        """Retune the settle loop so its deadline expires on the first pass.

        The floor is left at its real value, so no sample can qualify and the
        loop exits the way a process that never settles exits - through its own
        deadline, with the real ``_settle`` and ``settled_index`` running.
        Patching ``_settle`` out would have skipped both.
        """
        monkeypatch.setattr(mf, "_SETTLE_SAMPLE_INTERVAL_SECONDS", 0.0)
        monkeypatch.setattr(mf, "_SETTLE_TIMEOUT_SECONDS", 0.0)

    def test_main_never_settling_exits_nonzero_without_a_citable_figure(
        self, probe_env, never_settles, capsys
    ):
        """The one outcome the whole settle stage exists to prevent.

        A process whose threads are still arriving has no quotable resident
        cost. Printing one anyway would make the peak/settle distinction - the
        reason this script has a settle stage at all - decorative.
        """
        # Given - init succeeds; the settle loop gives up (fixtures)
        with patch("baldur.init"):
            # When
            exit_code = mf.main()

        # Then
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "No citable resident figure" in out
        assert "settled, citable" not in out
        assert mf._COMPLETE_BOUNDARY in out

    def test_main_configuration_error_names_the_cause_and_claims_no_measurement(
        self, probe_env, capsys
    ):
        """init() refuses to start in production without shared storage.

        That refusal is the framework's own posture rather than a fault in the
        probe, so it is reported instead of raised - but it must not read as a
        measurement that came out empty.
        """
        refusal = ConfigurationError("shared storage is required in production")

        with patch("baldur.init", side_effect=refusal):
            exit_code = mf.main()

        out = capsys.readouterr().out
        assert exit_code == 1
        assert "baldur.init() refused to start" in out
        assert str(refusal) in out
        assert "No measurement was taken" in out
        assert "settled, citable" not in out
        assert mf._COMPLETE_BOUNDARY in out

    def test_main_lets_every_other_init_failure_propagate(self, probe_env):
        """Fail-loud: exactly one exception is caught, and this is not it.

        A widened ``except`` would swallow a real fault and then report on a
        process that never initialised - the opposite of what the one caught
        case is for.
        """
        with patch("baldur.init", side_effect=RuntimeError("entitlement bus down")):
            with pytest.raises(RuntimeError, match="entitlement bus down"):
                mf.main()

    def test_main_does_not_override_an_explicitly_configured_admin_port(
        self, probe_env
    ):
        """setdefault, so a port the operator chose still wins.

        The probe imposes exactly one value on the posture it is measuring, and
        only where the operator expressed none. Asserted on the refusal path
        because the environment write is main()'s first line, before any branch.
        """
        with patch("baldur.init", side_effect=ConfigurationError("no storage")):
            mf.main()

        assert os.environ[_ADMIN_PORT_VAR] == _EXPLICIT_ADMIN_PORT


# =============================================================================
# Contract
# =============================================================================


class TestMeasureFootprintContract:
    """Design-contract values, and the encoding rule the report text lives under."""

    def test_settle_constants_match_the_measurement_protocol(self):
        """Floor 10 s past init()'s return, a sample every 2 s, 60 s before giving up."""
        assert mf._SETTLE_MIN_SECONDS == 10.0
        assert mf._SETTLE_SAMPLE_INTERVAL_SECONDS == 2.0
        assert mf._SETTLE_TIMEOUT_SECONDS == 60.0

    def test_settle_constants_preserve_the_pairwise_comparison_guarantee(self):
        """Whatever these are retuned to, the shape has to survive.

        A floor shorter than two sampling intervals would be reached before any
        pair could be compared, and a timeout at or below the floor would
        expire before a citable sample was ever possible.
        """
        assert mf._SETTLE_MIN_SECONDS >= 2 * mf._SETTLE_SAMPLE_INTERVAL_SECONDS
        assert mf._SETTLE_TIMEOUT_SECONDS > mf._SETTLE_MIN_SECONDS

    def test_the_completion_boundary_line_is_the_documented_one(self):
        """Readers are told everything after this line is shutdown work, so the
        exact text is a published contract rather than an internal label."""
        assert mf._COMPLETE_BOUNDARY == "--- measurement complete ---"

    def test_the_legacy_console_check_rejects_the_character_that_broke_the_script(self):
        """Guards the sweep below from passing because its predicate never says no."""
        assert _encodable_on_a_legacy_console("plain ascii") is True
        assert _encodable_on_a_legacy_console("an em dash \u2014 here") is False

    def test_every_report_literal_survives_a_legacy_console_code_page(self):
        """Pins a defect found by running the script, not by reading it: a single
        non-ASCII character in the report crashed it with UnicodeEncodeError on a
        legacy console, so the diagnostic died on the machine being diagnosed.

        Docstrings are exempt - they are never printed. Every other string
        literal in this module is report text.
        """
        tree = ast.parse(inspect.getsource(mf))
        documented = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, documented)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }

        offenders = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
            and not _encodable_on_a_legacy_console(node.value)
        ]

        assert offenders == []
