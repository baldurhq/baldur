"""
템플릿 문자열 치환 유틸리티.

format_map()에서 누락된 키를 빈 문자열로 대체하는 SafeFormatDict를 제공한다.
incident_timeline.py의 _SafeFormatDict를 공용 유틸리티로 승격한 것이다.

Usage:
    from baldur.utils.template import SafeFormatDict

    template = "Service {service_name} is down in {region}"
    context = {"service_name": "payment_api"}
    result = template.format_map(SafeFormatDict(context))
    # result: "Service payment_api is down in "

Reference:
    docs/baldur/middleware_system/273_RUNBOOK_PATTERN_MATCHER.md §8
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()


class SafeFormatDict(dict):
    """format_map()에서 누락 키를 빈 문자열로 대체하는 dict.

    Python 내장 str.format_map()과 사용하며, 누락된 변수가 있어도
    KeyError 없이 빈 문자열로 대체하여 안전하게 치환한다.
    """

    def __missing__(self, key: str) -> str:
        logger.warning(
            "template.missing_variable",
            template_variable_key=key,
        )
        return ""
