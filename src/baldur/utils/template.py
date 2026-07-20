"""
Template string substitution utilities.

Provides SafeFormatDict, which substitutes an empty string for keys missing
during format_map(). Promoted from the _SafeFormatDict in incident_timeline.py
into a shared utility.

Usage:
    from baldur.utils.template import SafeFormatDict

    template = "Service {service_name} is down in {region}"
    context = {"service_name": "payment_api"}
    result = template.format_map(SafeFormatDict(context))
    # result: "Service payment_api is down in "
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()


class SafeFormatDict(dict):
    """dict that substitutes an empty string for keys missing in format_map().

    Used with the built-in str.format_map(); substitutes an empty string for
    missing variables so substitution stays safe instead of raising KeyError.
    """

    def __missing__(self, key: str) -> str:
        logger.warning(
            "template.missing_variable",
            template_variable_key=key,
        )
        return ""
