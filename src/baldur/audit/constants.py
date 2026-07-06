"""Shared constants for audit module."""

FIXED_AUDIT_FIELDS: list[str] = [
    "timestamp",
    "action",
    "actor_id",
    "actor_type",
    "target_type",
    "target_id",
    "service_name",
    "reason",
    "success",
]
