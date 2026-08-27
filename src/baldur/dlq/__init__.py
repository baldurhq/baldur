"""OSS-side DLQ surface for the Baldur framework.

This package centralizes the DLQ + postmortem store delegation at a single
point, :mod:`baldur.dlq.helpers`. ``store_to_dlq`` resolves the PRO
``DLQService`` when it is registered and the OSS ``DLQCaptureService``
otherwise, so capture works on a plain OSS install; the compression and
postmortem helpers stay PRO-only and no-op when PRO is absent.

Note: ``baldur.services.dlq_outbox`` is a separate subsystem and is unrelated
to this package.

Status: Internal
"""

from __future__ import annotations

__all__: list[str] = []
