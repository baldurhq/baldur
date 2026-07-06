"""OSS-side notification surface for the Baldur framework.

This package provides thin-wrapper access to PRO notification helpers via
:mod:`baldur.notification.helpers`. OSS callsites import from
``baldur.notification.helpers`` so the OSS->PRO boundary is centralized at a
single delegation point.

Status: Internal
"""

from __future__ import annotations

__all__: list[str] = []
