"""OSS-side DLQ surface for the Baldur framework.

This package provides thin-wrapper access to PRO DLQ + postmortem store
helpers via :mod:`baldur.dlq.helpers`. OSS callsites import from
``baldur.dlq.helpers`` so the OSS->PRO boundary is centralized at a single
delegation point.

Note: ``baldur.services.dlq_outbox`` is a separate subsystem and is unrelated
to this package.

Status: Internal
"""

from __future__ import annotations

__all__: list[str] = []
