"""
arq Cron Scheduling unit tests (343).

Verifies cron expression parsing, interval-to-cron conversion, and
schedule_periodic() ctx absorption / args forwarding.

Test Categories:
    A. Contract: _FIELD_FULL_RANGES values, cron parsing output,
       weekday conversion boundary values, interval conversion output
    B. Behavior: invalid input handling, schedule_periodic wrapper creation,
       ctx absorption, args/kwargs forwarding, arq_cron interaction
"""

from __future__ import annotations

import sys
from datetime import timedelta
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

# --- arq fake modules (optional dependency) ---
_arq_mock = ModuleType("arq")
_arq_jobs_mock = ModuleType("arq.jobs")
_arq_connections_mock = ModuleType("arq.connections")
_arq_cron_mock = ModuleType("arq.cron")

_arq_mock.create_pool = AsyncMock(name="create_pool")
_arq_mock.ArqRedis = MagicMock(name="ArqRedis")
_arq_jobs_mock.Job = MagicMock(name="Job")
_arq_connections_mock.RedisSettings = MagicMock(name="RedisSettings")
_arq_cron_mock.cron = MagicMock(name="cron")
_arq_cron_mock.CronJob = MagicMock(name="CronJob")

for _name, _mod in [
    ("arq", _arq_mock),
    ("arq.jobs", _arq_jobs_mock),
    ("arq.connections", _arq_connections_mock),
    ("arq.cron", _arq_cron_mock),
]:
    if _name not in sys.modules:
        sys.modules[_name] = _mod

# croniter required for cron parsing tests
pytest.importorskip("croniter", reason="croniter required for cron scheduling tests")

from baldur.adapters.queues.arq_adapter import (
    _FIELD_FULL_RANGES,
    ArqTaskAdapter,
    _interval_to_arq_fields,
    _parse_cron_to_arq_fields,
)
from baldur.interfaces.task_queue import TaskNotFoundError

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def adapter():
    """ArqTaskAdapter instance."""
    return ArqTaskAdapter()


@pytest.fixture
def adapter_with_task(adapter):
    """ArqTaskAdapter with a pre-registered async task."""
    call_log: list[tuple] = []

    async def my_task(*args, **kwargs):
        call_log.append(("called", args, kwargs))
        return "ok"

    adapter._registered_tasks["my_task"] = my_task
    adapter._call_log = call_log  # type: ignore[attr-defined]
    return adapter


@pytest.fixture(autouse=True)
def _reset_arq_cron_mock():
    """Ensure a fresh MagicMock for arq.cron.cron before each test."""
    sys.modules["arq.cron"].cron = MagicMock(name="cron")
    return


def _get_arq_cron_mock() -> MagicMock:
    """Return the current arq.cron.cron mock from sys.modules."""
    return sys.modules["arq.cron"].cron


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestFieldFullRangesContract:
    """_FIELD_FULL_RANGES design contract values (doc 343 §3.1)."""

    def test_minute_range_covers_0_to_59(self):
        """minute full range: {0, 1, ..., 59}."""
        assert _FIELD_FULL_RANGES["minute"] == set(range(60))

    def test_hour_range_covers_0_to_23(self):
        """hour full range: {0, 1, ..., 23}."""
        assert _FIELD_FULL_RANGES["hour"] == set(range(24))

    def test_day_range_covers_1_to_31(self):
        """day full range: {1, 2, ..., 31}."""
        assert _FIELD_FULL_RANGES["day"] == set(range(1, 32))

    def test_month_range_covers_1_to_12(self):
        """month full range: {1, 2, ..., 12}."""
        assert _FIELD_FULL_RANGES["month"] == set(range(1, 13))

    def test_weekday_range_covers_0_to_6(self):
        """weekday full range: {0, 1, ..., 6} (Python convention)."""
        assert _FIELD_FULL_RANGES["weekday"] == set(range(7))


class TestCronParsingContract:
    """Cron expression -> arq field conversion contract (doc 343 §3.1, §6.1)."""

    def test_parse_every_5_minutes(self):
        """'*/5 * * * *' -> minute={0,5,10,...,55}, rest None."""
        result = _parse_cron_to_arq_fields("*/5 * * * *")
        assert result["minute"] == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
        assert result["hour"] is None
        assert result["day"] is None
        assert result["month"] is None
        assert result["weekday"] is None

    def test_parse_daily_midnight(self):
        """'0 0 * * *' -> minute={0}, hour={0}."""
        result = _parse_cron_to_arq_fields("0 0 * * *")
        assert result["minute"] == {0}
        assert result["hour"] == {0}
        assert result["day"] is None
        assert result["month"] is None
        assert result["weekday"] is None

    def test_parse_weekday_only_mon_to_fri(self):
        """'0 9 * * 1-5' -> weekday={0,1,2,3,4} (cron Mon-Fri -> Python Mon-Fri)."""
        result = _parse_cron_to_arq_fields("0 9 * * 1-5")
        assert result["minute"] == {0}
        assert result["hour"] == {9}
        assert result["weekday"] == {0, 1, 2, 3, 4}

    def test_parse_all_wildcards_returns_all_none(self):
        """'* * * * *' -> all fields None (every minute)."""
        result = _parse_cron_to_arq_fields("* * * * *")
        assert result["minute"] is None
        assert result["hour"] is None
        assert result["day"] is None
        assert result["month"] is None
        assert result["weekday"] is None


class TestWeekdayConversionContract:
    """Weekday convention conversion boundary values (doc 343 §6.1)."""

    def test_cron_sunday_zero_maps_to_python_6(self):
        """cron Sunday=0 -> Python Sunday=6."""
        result = _parse_cron_to_arq_fields("0 0 * * 0")
        assert result["weekday"] == {6}

    def test_cron_sunday_seven_alias_maps_to_python_6(self):
        """cron Sunday=7 (alias) -> Python Sunday=6."""
        result = _parse_cron_to_arq_fields("0 0 * * 7")
        assert result["weekday"] == {6}

    def test_cron_monday_maps_to_python_0(self):
        """cron Monday=1 -> Python Monday=0."""
        result = _parse_cron_to_arq_fields("0 0 * * 1")
        assert result["weekday"] == {0}

    def test_cron_saturday_maps_to_python_5(self):
        """cron Saturday=6 -> Python Saturday=5."""
        result = _parse_cron_to_arq_fields("0 0 * * 6")
        assert result["weekday"] == {5}


class TestIntervalConversionContract:
    """Interval -> arq field conversion contract (doc 343 §3.2, §6.2)."""

    def test_interval_5_minutes_produces_minute_set(self):
        """timedelta(minutes=5) -> minute={0,5,10,...,55}."""
        result = _interval_to_arq_fields(timedelta(minutes=5))
        assert result == {"minute": {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}}

    def test_interval_2_hours_produces_hour_and_minute_set(self):
        """timedelta(hours=2) -> hour={0,2,...,22}, minute={0}."""
        result = _interval_to_arq_fields(timedelta(hours=2))
        assert result == {
            "minute": {0},
            "hour": {0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22},
        }

    def test_interval_15_minutes_produces_quarter_hour_set(self):
        """timedelta(minutes=15) -> minute={0,15,30,45}."""
        result = _interval_to_arq_fields(timedelta(minutes=15))
        assert result == {"minute": {0, 15, 30, 45}}

    def test_interval_1_minute_produces_every_minute_set(self):
        """timedelta(minutes=1) -> minute={0,1,...,59}."""
        result = _interval_to_arq_fields(timedelta(minutes=1))
        assert result == {"minute": set(range(60))}


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestCronParsingErrorBehavior:
    """Cron expression parsing error handling."""

    def test_invalid_cron_expression_raises_value_error(self):
        """Malformed cron string raises ValueError with expression in message."""
        with pytest.raises(ValueError, match="Invalid cron expression"):
            _parse_cron_to_arq_fields("not a cron")

    def test_empty_cron_expression_raises_value_error(self):
        """Empty string raises ValueError."""
        with pytest.raises(ValueError):
            _parse_cron_to_arq_fields("")


class TestIntervalConversionErrorBehavior:
    """Interval conversion error handling (doc 343 §3.2 Strategy B)."""

    def test_sub_minute_interval_raises_value_error(self):
        """timedelta(seconds=30) -> ValueError (sub-minute not supported)."""
        with pytest.raises(ValueError, match="sub-minute intervals"):
            _interval_to_arq_fields(timedelta(seconds=30))

    def test_non_divisor_of_60_minutes_raises_value_error(self):
        """timedelta(minutes=7) -> ValueError (7 does not evenly divide 60)."""
        with pytest.raises(ValueError, match="cannot be evenly expressed"):
            _interval_to_arq_fields(timedelta(minutes=7))

    def test_sub_minute_precision_raises_value_error(self):
        """timedelta(seconds=90) -> ValueError (1.5 min = sub-minute precision)."""
        with pytest.raises(ValueError, match="sub-minute precision"):
            _interval_to_arq_fields(timedelta(seconds=90))

    def test_sub_hour_minute_remainder_raises_value_error(self):
        """timedelta(hours=2, minutes=30) -> ValueError (sub-hour remainder)."""
        with pytest.raises(ValueError, match="sub-hour minute remainder"):
            _interval_to_arq_fields(timedelta(hours=2, minutes=30))

    def test_non_divisor_of_24_hours_raises_value_error(self):
        """timedelta(hours=5) -> ValueError (5 does not evenly divide 24)."""
        with pytest.raises(ValueError, match="cannot be evenly expressed"):
            _interval_to_arq_fields(timedelta(hours=5))

    def test_zero_interval_raises_value_error(self):
        """timedelta(0) -> ValueError (0 seconds = sub-minute)."""
        with pytest.raises(ValueError, match="sub-minute intervals"):
            _interval_to_arq_fields(timedelta(0))


class TestSchedulePeriodicValidationBehavior:
    """schedule_periodic() input validation."""

    @pytest.mark.asyncio
    async def test_both_cron_and_interval_raises_value_error(self, adapter_with_task):
        """Providing both cron and interval raises ValueError."""
        with pytest.raises(ValueError, match="Cannot specify both"):
            await adapter_with_task.schedule_periodic(
                "my_task", cron="*/5 * * * *", interval=timedelta(minutes=5)
            )

    @pytest.mark.asyncio
    async def test_neither_cron_nor_interval_raises_value_error(
        self, adapter_with_task
    ):
        """Providing neither cron nor interval raises ValueError."""
        with pytest.raises(ValueError, match="Either cron or interval"):
            await adapter_with_task.schedule_periodic("my_task")

    @pytest.mark.asyncio
    async def test_unknown_task_raises_task_not_found(self, adapter):
        """Scheduling an unregistered task raises TaskNotFoundError."""
        with pytest.raises(TaskNotFoundError, match="Unknown task"):
            await adapter.schedule_periodic("nonexistent", cron="* * * * *")

    @pytest.mark.asyncio
    async def test_invalid_cron_in_schedule_raises_value_error(self, adapter_with_task):
        """Invalid cron expression propagates ValueError from parser."""
        with pytest.raises(ValueError, match="Invalid cron expression"):
            await adapter_with_task.schedule_periodic("my_task", cron="bad")

    @pytest.mark.asyncio
    async def test_unsupported_interval_in_schedule_raises_value_error(
        self, adapter_with_task
    ):
        """Unsupported interval propagates ValueError from converter."""
        with pytest.raises(ValueError, match="sub-minute intervals"):
            await adapter_with_task.schedule_periodic(
                "my_task", interval=timedelta(seconds=10)
            )


class TestSchedulePeriodicCronBehavior:
    """schedule_periodic() with cron expression — arq_cron interaction."""

    @pytest.mark.asyncio
    async def test_cron_calls_arq_cron_with_parsed_fields(self, adapter_with_task):
        """arq.cron.cron() receives parsed cron fields as kwargs."""
        # When
        await adapter_with_task.schedule_periodic("my_task", cron="*/5 * * * *")

        # Then
        arq_cron_fn = _get_arq_cron_mock()
        arq_cron_fn.assert_called_once()
        call_kwargs = arq_cron_fn.call_args.kwargs
        assert call_kwargs["name"] == "my_task"
        assert call_kwargs["minute"] == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
        assert call_kwargs["hour"] is None
        assert call_kwargs["day"] is None
        assert call_kwargs["month"] is None
        assert call_kwargs["weekday"] is None

    @pytest.mark.asyncio
    async def test_schedule_returns_cron_prefix_with_task_name(self, adapter_with_task):
        """schedule_periodic returns 'cron:{task_name}'."""
        result = await adapter_with_task.schedule_periodic("my_task", cron="* * * * *")
        assert result == "cron:my_task"

    @pytest.mark.asyncio
    async def test_schedule_appends_to_cron_jobs_list(self, adapter_with_task):
        """Scheduled cron job is appended to adapter._cron_jobs."""
        # Given
        mock_cron_job = MagicMock()
        _get_arq_cron_mock().return_value = mock_cron_job

        # When
        await adapter_with_task.schedule_periodic("my_task", cron="* * * * *")

        # Then
        assert len(adapter_with_task._cron_jobs) == 1
        assert adapter_with_task._cron_jobs[0] is mock_cron_job


class TestSchedulePeriodicIntervalBehavior:
    """schedule_periodic() with interval — arq_cron interaction."""

    @pytest.mark.asyncio
    async def test_interval_calls_arq_cron_with_converted_fields(
        self, adapter_with_task
    ):
        """arq.cron.cron() receives interval-derived minute set."""
        # When
        await adapter_with_task.schedule_periodic(
            "my_task", interval=timedelta(minutes=15)
        )

        # Then
        arq_cron_fn = _get_arq_cron_mock()
        arq_cron_fn.assert_called_once()
        call_kwargs = arq_cron_fn.call_args.kwargs
        assert call_kwargs["name"] == "my_task"
        assert call_kwargs["minute"] == {0, 15, 30, 45}
        # Interval only sets minute — hour/day/month/weekday not in kwargs
        assert "hour" not in call_kwargs

    @pytest.mark.asyncio
    async def test_hours_interval_calls_arq_cron_with_hour_set(self, adapter_with_task):
        """arq.cron.cron() receives hour set for hours-level interval."""
        # When
        await adapter_with_task.schedule_periodic(
            "my_task", interval=timedelta(hours=4)
        )

        # Then
        arq_cron_fn = _get_arq_cron_mock()
        call_kwargs = arq_cron_fn.call_args.kwargs
        assert call_kwargs["minute"] == {0}
        assert call_kwargs["hour"] == {0, 4, 8, 12, 16, 20}


class TestSchedulePeriodicWrapperBehavior:
    """Wrapper creation, ctx absorption, and args/kwargs forwarding (doc 343 §3.3, §3.4)."""

    @pytest.mark.asyncio
    async def test_always_creates_wrapper_with_scheduled_qualname(
        self, adapter_with_task
    ):
        """Wrapper __qualname__ ends with [scheduled] regardless of args."""
        await adapter_with_task.schedule_periodic("my_task", cron="* * * * *")

        wrapper = _get_arq_cron_mock().call_args.args[0]
        assert wrapper.__qualname__.endswith("[scheduled]")

    @pytest.mark.asyncio
    async def test_wrapper_absorbs_ctx_without_forwarding(self, adapter_with_task):
        """Wrapper receives arq ctx but does NOT pass it to the original function."""
        # Given
        await adapter_with_task.schedule_periodic("my_task", cron="* * * * *")
        wrapper = _get_arq_cron_mock().call_args.args[0]

        # When — simulate arq calling wrapper(ctx)
        fake_ctx = {"redis": MagicMock(), "job_id": "test-job"}
        result = await wrapper(fake_ctx)

        # Then — original function called without ctx
        assert result == "ok"
        assert len(adapter_with_task._call_log) == 1
        assert adapter_with_task._call_log[0] == ("called", (), {})

    @pytest.mark.asyncio
    async def test_wrapper_forwards_captured_args_and_kwargs(self, adapter_with_task):
        """Wrapper forwards the args/kwargs captured at schedule time."""
        # Given
        await adapter_with_task.schedule_periodic(
            "my_task",
            cron="0 0 * * *",
            args=("a", "b"),
            kwargs={"key": "value"},
        )
        wrapper = _get_arq_cron_mock().call_args.args[0]

        # When — simulate arq calling wrapper(ctx)
        result = await wrapper({"redis": MagicMock()})

        # Then
        assert result == "ok"
        assert len(adapter_with_task._call_log) == 1
        assert adapter_with_task._call_log[0] == (
            "called",
            ("a", "b"),
            {"key": "value"},
        )

    @pytest.mark.asyncio
    async def test_wrapper_no_args_still_absorbs_ctx(self, adapter_with_task):
        """Even without args/kwargs, wrapper absorbs ctx (no TypeError)."""

        # Given — task that accepts no arguments
        async def no_arg_task():
            return "done"

        adapter_with_task._registered_tasks["my_task"] = no_arg_task

        await adapter_with_task.schedule_periodic("my_task", cron="* * * * *")
        wrapper = _get_arq_cron_mock().call_args.args[0]

        # When — arq calls wrapper(ctx) — should NOT raise TypeError
        result = await wrapper({"redis": MagicMock()})

        # Then
        assert result == "done"
