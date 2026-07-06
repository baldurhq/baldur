"""
Time Helpers for Testing.

datetime.now()와 time.sleep() 호출을 제어하기 위한 유틸리티입니다.

Usage:
    from tests.factories.time_helpers import freeze_time, mock_sleep, get_fixed_datetime

    # FreezeTime - datetime.now()를 특정 시간으로 고정
    with freeze_time("2024-01-15 12:00:00"):  # 테스트에서 원하는 시간 지정
        now = datetime.now()  # 고정된 시간 반환

    # MockSleep - time.sleep()을 모킹하여 즉시 반환
    with mock_sleep() as sleep_mock:
        time.sleep(10)  # 즉시 반환
        assert sleep_mock.total_slept == 10

    # 고정 datetime 생성 (모든 인자 필수)
    fixed_dt = get_fixed_datetime(2024, 1, 15, 12, 0, 0)

Note:
    freezegun 패키지 필요: pip install freezegun
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

# freezegun 사용
_freeze_time: Any = None
try:
    from freezegun import freeze_time as _freeze_time

    FREEZEGUN_AVAILABLE = True
except ImportError:
    FREEZEGUN_AVAILABLE = False


class MockSleep:
    """
    time.sleep()을 추적하는 Mock.

    실제 대기 없이 sleep 호출을 기록하고 즉시 반환합니다.

    Attributes:
        calls: sleep() 호출 기록 [(seconds,), ...]
        total_slept: 총 sleep 시간 (초)
    """

    def __init__(self):
        self.calls = []
        self.total_slept = 0.0

    def __call__(self, seconds: float) -> None:
        """sleep 호출 기록."""
        self.calls.append(seconds)
        self.total_slept += seconds

    @property
    def call_count(self) -> int:
        """호출 횟수."""
        return len(self.calls)

    def assert_called(self) -> None:
        """sleep이 한 번 이상 호출되었는지 확인."""
        assert self.call_count > 0, "sleep was not called"

    def assert_called_with(self, seconds: float) -> None:
        """특정 시간으로 호출되었는지 확인."""
        assert seconds in self.calls, (
            f"sleep({seconds}) was not called. Calls: {self.calls}"
        )


@contextmanager
def mock_sleep() -> Generator[MockSleep, None, None]:
    """
    time.sleep()을 모킹하여 즉시 반환합니다.

    Usage:
        with mock_sleep() as sleep_mock:
            time.sleep(5)  # 즉시 반환
            assert sleep_mock.total_slept == 5
            assert sleep_mock.call_count == 1

    Yields:
        MockSleep: sleep 호출 추적 객체
    """
    mock = MockSleep()
    with patch("time.sleep", side_effect=mock):
        yield mock


@contextmanager
def freeze_time(time_to_freeze: str, **kwargs) -> Generator[None, None, None]:
    """
    datetime.now()를 특정 시간으로 고정합니다.

    freezegun 패키지를 래핑하여 일관된 인터페이스를 제공합니다.

    Args:
        time_to_freeze: ISO 형식 시간 문자열 (예: "2024-01-15 12:00:00")
        **kwargs: freezegun.freeze_time에 전달할 추가 인자

    Usage:
        with freeze_time("2024-01-15 12:00:00"):
            now = datetime.now()  # 고정된 시간 반환

    Yields:
        None

    Raises:
        ImportError: freezegun이 설치되지 않은 경우
    """
    if not FREEZEGUN_AVAILABLE:
        raise ImportError(
            "freezegun is required for freeze_time. Install with: pip install freezegun"
        )

    with _freeze_time(time_to_freeze, **kwargs):
        yield


def get_fixed_datetime(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    tz: timezone | None = UTC,
) -> datetime:
    """
    테스트용 고정 datetime 반환.

    일관된 테스트를 위해 고정된 시간을 생성합니다.
    년/월/일은 필수 인자로, 하드코딩 없이 명시적으로 지정해야 합니다.

    Args:
        year: 연도 (필수)
        month: 월 (필수)
        day: 일 (필수)
        hour: 시 (기본: 0)
        minute: 분 (기본: 0)
        second: 초 (기본: 0)
        tz: 시간대 (기본: UTC)

    Returns:
        datetime: 지정된 시간의 datetime 객체

    Example:
        dt = get_fixed_datetime(2024, 1, 15, 12, 30, 0)
    """
    dt = datetime(year, month, day, hour, minute, second)
    if tz:
        dt = dt.replace(tzinfo=tz)
    return dt


def make_datetime_range(
    start: datetime,
    count: int,
    delta: timedelta = timedelta(seconds=1),
) -> list:
    """
    datetime 범위 생성.

    테스트에서 시간 순서가 있는 여러 datetime이 필요할 때 사용합니다.

    Args:
        start: 시작 시간
        count: 생성할 datetime 개수
        delta: 각 datetime 간 간격

    Returns:
        list[datetime]: datetime 리스트
    """
    return [start + delta * i for i in range(count)]
