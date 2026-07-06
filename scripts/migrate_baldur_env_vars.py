#!/usr/bin/env python3
"""Migrate a .env file from pre-508 Baldur env var names to v1.0.

Applies the rename map from impl doc 508 (Wave 6A API surface freeze)
in-place to the given file. stdlib-only — runs on any Python 3.9+ without
baldur installed.

Renames covered:
  D1   BALDUR_CORRELATION_*  (correlation_engine fields only)
                         -> BALDUR_CORRELATION_ENGINE_*
  D2   BALDUR_LICENSE_*      unchanged (entitlement.py -> license.py
                              rename preserves env var names)
  D8   BALDUR_CANARY_GOV_*   -> BALDUR_CANARY_GOVERNANCE_*
       BALDUR_RECOVERY_COORD_* -> BALDUR_RECOVERY_COORDINATOR_*
       BALDUR_META_*         -> BALDUR_META_WATCHDOG_*
  D9   28 module/prefix renames (see RENAMES below)
  D16  BALDUR_DLQ_LOG_LEVEL  -> BALDUR_EVENT_LOGGING_DLQ_LOG_LEVEL
       BALDUR_CB_LOG_LEVEL   -> BALDUR_EVENT_LOGGING_CB_LOG_LEVEL
       BALDUR_REPLAY_LOG_LEVEL -> BALDUR_EVENT_LOGGING_REPLAY_LOG_LEVEL
       BALDUR_SLA_LOG_LEVEL  -> BALDUR_EVENT_LOGGING_SLA_LOG_LEVEL

Usage:
  python migrate_baldur_env_vars.py path/to/.env [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Order matters: longer prefixes first to avoid partial-match overwrites
# (e.g., BALDUR_AUTO_TUNING_ must NOT be touched by BALDUR_AUTO_ -> BALDUR_AUTO_CONFIG_).
# We use regex with negative lookahead instead of substring replace.
RENAMES: list[tuple[str, str]] = [
    # D16 — event_logging (specific field renames, NOT a prefix sweep)
    (r"^BALDUR_DLQ_LOG_LEVEL=", "BALDUR_EVENT_LOGGING_DLQ_LOG_LEVEL="),
    (r"^BALDUR_CB_LOG_LEVEL=", "BALDUR_EVENT_LOGGING_CB_LOG_LEVEL="),
    (r"^BALDUR_REPLAY_LOG_LEVEL=", "BALDUR_EVENT_LOGGING_REPLAY_LOG_LEVEL="),
    (r"^BALDUR_SLA_LOG_LEVEL=", "BALDUR_EVENT_LOGGING_SLA_LOG_LEVEL="),
    # D8 — abbreviation expansions
    (r"^BALDUR_CANARY_GOV_", "BALDUR_CANARY_GOVERNANCE_"),
    (r"^BALDUR_RECOVERY_COORD_", "BALDUR_RECOVERY_COORDINATOR_"),
    # D9 — long-prefix renames (sorted longest-first to avoid AUTO/STATE traps)
    (
        r"^BALDUR_RATE_LIMIT_THROTTLE_(?!INTEGRATION_)",
        "BALDUR_RATE_LIMIT_THROTTLE_INTEGRATION_",
    ),
    (r"^BALDUR_REGIONAL_RECOVERY_(?!POLICY_)", "BALDUR_REGIONAL_RECOVERY_POLICY_"),
    (r"^BALDUR_REDIS_GUARD_", "BALDUR_REDIS_KEY_GUARD_"),
    (r"^BALDUR_CB_ADV_", "BALDUR_CB_ADVANCED_"),
    (r"^BALDUR_API_RATE_(?!LIMIT_)", "BALDUR_API_RATE_LIMIT_"),
    (r"^BALDUR_EVENTBUS_", "BALDUR_EVENT_BUS_"),
    (r"^BALDUR_SHUTDOWN_", "BALDUR_RECOVERY_SHUTDOWN_"),
    (r"^BALDUR_FORECASTER_", "BALDUR_PREDICTIVE_FORECASTER_"),
    (r"^BALDUR_ROLLBACK_", "BALDUR_AUTO_ROLLBACK_"),
    (r"^BALDUR_DECISION_(?!ENGINE_)", "BALDUR_DECISION_ENGINE_"),
    (r"^BALDUR_RESOURCE_(?!MONITOR_|GUARD_)", "BALDUR_RESOURCE_MONITOR_"),
    (r"^BALDUR_LOGGING_(?!SETTINGS_)", "BALDUR_LOGGING_SETTINGS_"),
    (r"^BALDUR_RUNTIME_(?!FEEDBACK_)", "BALDUR_RUNTIME_FEEDBACK_"),
    (r"^BALDUR_JOURNAL_", "BALDUR_EVENT_JOURNAL_"),
    (r"^BALDUR_REPLAY_(?!AUTOMATION_)", "BALDUR_REPLAY_AUTOMATION_"),
    (r"^BALDUR_CELERY_(?!TASK_)", "BALDUR_CELERY_TASK_"),
    (r"^BALDUR_SECRET_(?!S)", "BALDUR_SECRETS_"),
    (r"^BALDUR_BOUNDS_", "BALDUR_SAFETY_BOUNDS_"),
    (r"^BALDUR_SHADOW_", "BALDUR_CONFIG_SHADOW_"),
    (r"^BALDUR_LEADER_(?!ELECTION_)", "BALDUR_LEADER_ELECTION_"),
    (r"^BALDUR_THREAD_(?!MANAGEMENT_)", "BALDUR_THREAD_MANAGEMENT_"),
    (r"^BALDUR_DRIFT_(?!THRESHOLD_|DETECTION_)", "BALDUR_DRIFT_THRESHOLD_"),
    (r"^BALDUR_STATE_(?!CACHE_)", "BALDUR_SYSTEM_CONTROL_"),
    (r"^BALDUR_APPLY_(?!STRATEGY_)", "BALDUR_APPLY_STRATEGY_"),
    (r"^BALDUR_SLACK_(?!CHANNEL_)", "BALDUR_SLACK_CHANNEL_"),
    (r"^BALDUR_ARQ_(?!TASK_)", "BALDUR_ARQ_TASK_"),
    (r"^BALDUR_AUTO_(?!CONFIG_|TUNING_|ROLLBACK_)", "BALDUR_AUTO_CONFIG_"),
    (r"^BALDUR_ML_(?!MODELS_)", "BALDUR_ML_MODELS_"),
    # D8 — META scope rename (after specific D9 entries to avoid premature match)
    (r"^BALDUR_META_(?!WATCHDOG_)", "BALDUR_META_WATCHDOG_"),
    # D1 — correlation_engine fields move under BALDUR_CORRELATION_ENGINE_.
    # Note: BALDUR_CORRELATION_ (DAG/co-occurrence — settings/correlation.py)
    # is preserved. Only the 9 orchestrator fields move.
    (r"^BALDUR_CORRELATION_ENABLED=", "BALDUR_CORRELATION_ENGINE_ENABLED="),
    (
        r"^BALDUR_CORRELATION_ANALYSIS_INTERVAL_SECONDS=",
        "BALDUR_CORRELATION_ENGINE_ANALYSIS_INTERVAL_SECONDS=",
    ),
    (
        r"^BALDUR_CORRELATION_WEIGHT_TOPOLOGY=",
        "BALDUR_CORRELATION_ENGINE_WEIGHT_TOPOLOGY=",
    ),
    (
        r"^BALDUR_CORRELATION_WEIGHT_TEMPORAL=",
        "BALDUR_CORRELATION_ENGINE_WEIGHT_TEMPORAL=",
    ),
    (
        r"^BALDUR_CORRELATION_WEIGHT_BLAST_RADIUS=",
        "BALDUR_CORRELATION_ENGINE_WEIGHT_BLAST_RADIUS=",
    ),
    (
        r"^BALDUR_CORRELATION_WEIGHT_HISTORICAL=",
        "BALDUR_CORRELATION_ENGINE_WEIGHT_HISTORICAL=",
    ),
    (
        r"^BALDUR_CORRELATION_LEARNING_INTEGRATION_ENABLED=",
        "BALDUR_CORRELATION_ENGINE_LEARNING_INTEGRATION_ENABLED=",
    ),
    (
        r"^BALDUR_CORRELATION_POSTMORTEM_INTEGRATION_ENABLED=",
        "BALDUR_CORRELATION_ENGINE_POSTMORTEM_INTEGRATION_ENABLED=",
    ),
    (
        r"^BALDUR_CORRELATION_STATE_PERSISTENCE_ENABLED=",
        "BALDUR_CORRELATION_ENGINE_STATE_PERSISTENCE_ENABLED=",
    ),
]


def migrate(text: str) -> tuple[str, int]:
    """Return (rewritten_text, replacements_made)."""
    out_lines: list[str] = []
    changes = 0
    for line in text.splitlines(keepends=True):
        original = line
        for pattern, replacement in RENAMES:
            new_line = re.sub(pattern, replacement, line, count=1, flags=re.MULTILINE)
            if new_line != line:
                line = new_line
                break
        if line != original:
            changes += 1
        out_lines.append(line)
    return "".join(out_lines), changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", help="Path to .env file to rewrite in place")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the proposed rewrite to stdout instead of editing the file",
    )
    args = parser.parse_args()

    path = Path(args.env_file)
    if not path.is_file():
        print(f"error: {path} is not a file", file=sys.stderr)
        return 1

    original = path.read_text(encoding="utf-8")
    new_text, changes = migrate(original)
    if changes == 0:
        print(f"{path}: no Baldur env var renames applied (already on v1.0?)")
        return 0
    if args.dry_run:
        sys.stdout.write(new_text)
        print(f"\n# {changes} line(s) would be rewritten", file=sys.stderr)
    else:
        path.write_text(new_text, encoding="utf-8")
        print(f"{path}: {changes} line(s) rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
