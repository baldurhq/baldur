"""
Framework-agnostic Continuous Audit handlers.

Extracted from api/django/views/continuous_audit.py (Phase 2b).

Endpoints:
    GET  /audit/logs                  Query audit logs
    GET  /audit/logs/{log_id}         Audit log detail
    GET  /audit/auto-tuning           Auto tuning history
    GET  /audit/drift                 DNA drift history
    GET  /audit/compliance            Compliance check history
    GET  /audit/integrity/verify      Integrity verification
    GET  /audit/integrity/state       Hash chain state
    GET  /audit/export/jsonl          JSONL streaming export
    GET  /audit/export/csv            CSV streaming export
    GET  /audit/config                Audit configuration
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.duration import parse_iso_timestamp
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "continuous_audit_query",
    "continuous_audit_detail",
    "continuous_audit_auto_tuning",
    "continuous_audit_drift_history",
    "continuous_audit_compliance_history",
    "continuous_audit_integrity_verify",
    "continuous_audit_chain_state",
    "continuous_audit_export_jsonl",
    "continuous_audit_export_csv",
    "continuous_audit_config",
]


def _recorder():
    from baldur.audit.continuous_audit import get_continuous_audit_recorder

    return get_continuous_audit_recorder()


def continuous_audit_query(ctx: RequestContext) -> ResponseContext:
    """GET /audit/logs — query audit logs (viewer)."""
    try:
        recorder = _recorder()

        action = ctx.get_query("action")
        target_type = ctx.get_query("target_type")
        target_id = ctx.get_query("target_id")
        start_time = parse_iso_timestamp(ctx.get_query("start_time"))
        end_time = parse_iso_timestamp(ctx.get_query("end_time"))

        try:
            limit = int(ctx.get_query("limit", 100))
        except (TypeError, ValueError):
            limit = 100

        action_enum = None
        if action:
            try:
                from baldur.interfaces.audit_adapter import AuditAction

                action_enum = AuditAction(action)
            except ValueError:
                pass

        entries = recorder.query(
            action=action_enum,
            target_type=target_type,
            target_id=target_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

        return ResponseContext.json(
            {
                "entries": entries,
                "count": len(entries),
                "filters": {
                    "action": action,
                    "target_type": target_type,
                    "target_id": target_id,
                    "start_time": (start_time.isoformat() if start_time else None),
                    "end_time": end_time.isoformat() if end_time else None,
                    "limit": limit,
                },
            }
        )
    except Exception as e:
        logger.exception("continuous_audit.query_failed", error=e)
        return ResponseContext.server_error(str(e))


def continuous_audit_detail(ctx: RequestContext) -> ResponseContext:
    """GET /audit/logs/{log_id} — audit log detail (viewer)."""
    log_id = ctx.get_path_param("log_id")
    try:
        recorder = _recorder()

        parts = log_id.split("-")
        if len(parts) != 3 or parts[0] != "audit":
            return ResponseContext.bad_request("Invalid log ID format")

        try:
            ts_str = parts[1]
            timestamp = datetime.strptime(ts_str, "%Y%m%d%H%M%S")
            timestamp = timestamp.replace(tzinfo=UTC)
        except ValueError:
            return ResponseContext.bad_request("Invalid timestamp in log ID")

        sequence = int(parts[2])

        entries = recorder.query(start_time=timestamp, limit=100)
        for entry in entries:
            integrity = entry.get("details", {}).get("integrity", {})
            if integrity.get("sequence") == sequence:
                return ResponseContext.json({"entry": entry})

        return ResponseContext.not_found(f"Log entry '{log_id}' not found")
    except Exception as e:
        logger.exception("continuous_audit.detail_failed", error=e)
        return ResponseContext.server_error(str(e))


def continuous_audit_auto_tuning(ctx: RequestContext) -> ResponseContext:
    """GET /audit/auto-tuning — auto tuning history (viewer)."""
    try:
        recorder = _recorder()

        parameter = ctx.get_query("parameter")
        start_time = parse_iso_timestamp(ctx.get_query("start_time"))
        end_time = parse_iso_timestamp(ctx.get_query("end_time"))

        try:
            limit = int(ctx.get_query("limit", 100))
        except (TypeError, ValueError):
            limit = 100

        entries = recorder.query_auto_tuning_history(
            parameter=parameter,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        return ResponseContext.json({"entries": entries, "count": len(entries)})
    except Exception as e:
        logger.exception("continuous_audit.auto_tuning_failed", error=e)
        return ResponseContext.server_error(str(e))


def continuous_audit_drift_history(ctx: RequestContext) -> ResponseContext:
    """GET /audit/drift — DNA drift history (viewer)."""
    try:
        recorder = _recorder()

        resource_id = ctx.get_query("resource_id")
        start_time = parse_iso_timestamp(ctx.get_query("start_time"))
        end_time = parse_iso_timestamp(ctx.get_query("end_time"))

        try:
            limit = int(ctx.get_query("limit", 100))
        except (TypeError, ValueError):
            limit = 100

        entries = recorder.query_drift_history(
            resource_id=resource_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        return ResponseContext.json({"entries": entries, "count": len(entries)})
    except Exception as e:
        logger.exception("continuous_audit.drift_history_failed", error=e)
        return ResponseContext.server_error(str(e))


def continuous_audit_compliance_history(ctx: RequestContext) -> ResponseContext:
    """GET /audit/compliance — compliance check history (viewer)."""
    try:
        recorder = _recorder()

        standard = ctx.get_query("standard")
        start_time = parse_iso_timestamp(ctx.get_query("start_time"))
        end_time = parse_iso_timestamp(ctx.get_query("end_time"))

        try:
            limit = int(ctx.get_query("limit", 100))
        except (TypeError, ValueError):
            limit = 100

        entries = recorder.query_compliance_history(
            standard=standard,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        return ResponseContext.json({"entries": entries, "count": len(entries)})
    except Exception as e:
        logger.exception("continuous_audit.compliance_history_failed", error=e)
        return ResponseContext.server_error(str(e))


def continuous_audit_integrity_verify(ctx: RequestContext) -> ResponseContext:
    """GET /audit/integrity/verify — integrity verification (viewer)."""
    try:
        recorder = _recorder()
        result = recorder.verify_integrity()
        status_code = 200 if result.get("verified", False) else 400
        return ResponseContext.json(result, status_code=status_code)
    except Exception as e:
        logger.exception("continuous_audit.integrity_verify_failed", error=e)
        return ResponseContext.server_error(str(e))


def continuous_audit_chain_state(ctx: RequestContext) -> ResponseContext:
    """GET /audit/integrity/state — hash chain state (viewer)."""
    try:
        recorder = _recorder()
        state = recorder.get_chain_state()
        return ResponseContext.json(
            {"chain_state": state, "timestamp": utc_now().isoformat()}
        )
    except Exception as e:
        logger.exception("continuous_audit.chain_state_failed", error=e)
        return ResponseContext.server_error(str(e))


def continuous_audit_export_jsonl(ctx: RequestContext) -> ResponseContext:
    """GET /audit/export/jsonl — JSONL streaming export (viewer)."""
    try:
        recorder = _recorder()

        start_time = parse_iso_timestamp(ctx.get_query("start_time"))
        end_time = parse_iso_timestamp(ctx.get_query("end_time"))

        action_filter = None
        actions_str = ctx.get_query("actions")
        if actions_str:
            from baldur.interfaces.audit_adapter import AuditAction

            action_filter = []
            for action_name in actions_str.split(","):
                try:
                    action_filter.append(AuditAction(action_name.strip()))
                except ValueError:
                    pass

        def generate():
            for line in recorder.export_jsonl(
                start_time=start_time,
                end_time=end_time,
                action_filter=action_filter,
            ):
                yield line + "\n"

        return ResponseContext.streaming(
            generate(),
            content_type="application/x-ndjson",
            filename="audit_export.jsonl",
        )
    except Exception as e:
        logger.exception("continuous_audit.export_jsonl_failed", error=e)
        return ResponseContext.server_error(str(e))


def continuous_audit_export_csv(ctx: RequestContext) -> ResponseContext:
    """GET /audit/export/csv — CSV streaming export (viewer)."""
    try:
        from baldur.audit.constants import FIXED_AUDIT_FIELDS

        recorder = _recorder()

        start_time = parse_iso_timestamp(ctx.get_query("start_time"))
        end_time = parse_iso_timestamp(ctx.get_query("end_time"))

        def generate_csv():
            import csv
            from io import StringIO

            buf = StringIO()
            writer = csv.DictWriter(buf, fieldnames=FIXED_AUDIT_FIELDS)
            writer.writeheader()
            yield buf.getvalue()

            for row in recorder.export_csv_compatible(
                start_time=start_time,
                end_time=end_time,
            ):
                buf = StringIO()
                writer = csv.DictWriter(
                    buf,
                    fieldnames=FIXED_AUDIT_FIELDS,
                    extrasaction="ignore",
                )
                writer.writerow(
                    {k: str(v) if v is not None else "" for k, v in row.items()}
                )
                yield buf.getvalue()

        return ResponseContext.streaming(
            generate_csv(),
            content_type="text/csv",
            filename="audit_export.csv",
        )
    except Exception as e:
        logger.exception("continuous_audit.export_csv_failed", error=e)
        return ResponseContext.server_error(str(e))


def continuous_audit_config(ctx: RequestContext) -> ResponseContext:
    """GET /audit/config — current audit configuration (viewer)."""
    try:
        recorder = _recorder()
        config = recorder.config.to_dict()
        return ResponseContext.json(
            {"config": config, "timestamp": utc_now().isoformat()}
        )
    except Exception as e:
        logger.exception("continuous_audit.config_failed", error=e)
        return ResponseContext.server_error(str(e))
