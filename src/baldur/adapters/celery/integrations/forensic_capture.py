"""
Forensic Capture — capture forensic context for failed tasks.

Wraps lazy imports to baldur.services.forensic_context so the signal
handler layer never crashes due to missing optional dependencies.
"""

from __future__ import annotations

from typing import Any

import structlog

__all__ = ["ForensicCapture"]

logger = structlog.get_logger()


class ForensicCapture:
    """Capture forensic context for failed Celery tasks."""

    def capture(
        self,
        task_name: str,
        task_id: str,
        exception: Exception,
        args: tuple | None,
        kwargs: dict | None,
        einfo: Any,
    ) -> Any:
        """Capture forensic context for a failed task."""
        try:
            from baldur.services.forensic_context import (
                capture_forensic_context,  # type: ignore[import-not-found]  # noqa: E501 — optional module, protected by ImportError fallback
            )

            context = capture_forensic_context(
                task_id=task_id,
                task_name=task_name,
            )

            logger.debug(
                "baldur_forensics.context_captured",
                task_name=task_name,
            )

            self._record_to_audit(
                exception=exception,
                einfo=einfo,
                context=context,
                task_id=task_id,
            )

            return context

        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "baldur_forensics.capture_failed",
                error=e,
            )
            return None

    @staticmethod
    def _record_to_audit(
        *,
        exception: Exception,
        einfo: Any,
        context: dict[str, Any] | None,
        task_id: str,
    ) -> None:
        """Forward the captured forensic context to the audit log.

        Fail-open: any failure inside this path is swallowed so it cannot
        destabilise the upstream Celery failure handler.
        """
        try:
            from baldur.audit.forensic_recorder import record_forensic_capture

            stack_trace = getattr(einfo, "traceback", None) or ""
            record_forensic_capture(
                exception=exception,
                stack_trace=str(stack_trace),
                context=context,
                target_type="celery_task",
                target_id=task_id,
            )
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "baldur_forensics.audit_record_failed",
                error=e,
            )
