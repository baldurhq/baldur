"""Runtime-Config Domain Value Types.

OSS-tier value types for runtime-configuration revision metadata. Pure
enum with no PRO runtime dependency.
"""

from __future__ import annotations

from enum import Enum


class RevisionChangeType(str, Enum):
    """Revision change type for runtime-config history."""

    INITIAL = "initial"
    """First revision (creation)."""

    ANALYSIS_UPDATE = "analysis_update"
    """Analysis content updated."""

    TIMELINE_CORRECTION = "timeline_correction"
    """Timeline corrected."""

    IMPROVEMENT_ADDED = "improvement_added"
    """Improvement / action item added."""

    ANNOTATION = "annotation"
    """Annotation / comment added."""

    CORRECTION = "correction"
    """Error correction."""

    SEALED = "sealed"
    """Final seal — no further modifications."""

    ROLLBACK = "rollback"
    """Rollback to an earlier revision."""


__all__ = ["RevisionChangeType"]
