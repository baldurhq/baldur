"""
모듈별 ConfigApplier를 조합하는 Composite ConfigApplier.

appliers 리스트를 순서대로 순회하며, 첫 번째로 처리 가능한
applier가 요청을 수행한다. 어떤 applier도 처리하지 못하면 False를 반환한다.
"""

from typing import Protocol

import structlog

logger = structlog.get_logger()


class ConfigApplierProtocol(Protocol):
    """ConfigApplier Protocol (core/runtime_feedback.py 정의와 동일)."""

    def get_current(self, parameter: str) -> float: ...
    def apply(self, parameter: str, value: float) -> bool: ...
    def rollback(self, parameter: str, value: float) -> bool: ...


class CompositeConfigApplier:
    """
    모듈별 ConfigApplier를 조합하는 Composite.

    appliers 리스트를 순서대로 순회하며, 첫 번째로 처리 가능한
    applier가 요청을 수행한다. 어떤 applier도 처리하지 못하면
    False를 반환한다.
    """

    def __init__(self, appliers: list[ConfigApplierProtocol]):
        if not appliers:
            raise ValueError("CompositeConfigApplier requires at least one applier")
        self._appliers = appliers

    def get_current(self, parameter: str) -> float:
        """첫 번째로 처리 가능한 applier에서 값 조회."""
        last_error: Exception | None = None
        for applier in self._appliers:
            try:
                return applier.get_current(parameter)
            except (ValueError, KeyError) as e:
                last_error = e
                continue
        # 모든 applier가 실패 → 마지막 에러 전파
        raise ValueError(
            f"No applier can handle parameter '{parameter}'"
        ) from last_error

    def apply(self, parameter: str, value: float) -> bool:
        """첫 번째로 True를 반환하는 applier에 위임."""
        for applier in self._appliers:
            if applier.apply(parameter, value):
                return True
        logger.warning(
            "composite_config_applier.no_applier_handled",
            config_parameter=parameter,
            config_value=value,
        )
        return False

    def rollback(self, parameter: str, value: float) -> bool:
        """apply()와 동일한 라우팅 로직으로 롤백."""
        return any(applier.rollback(parameter, value) for applier in self._appliers)
