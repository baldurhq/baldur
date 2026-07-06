"""
Hash Chain Verifier.

Contains:
- HashChainVerifier: Verifies the integrity of a hash chain
- verify_audit_log_integrity: Helper function to verify audit log files
"""

from __future__ import annotations

import hmac
import json
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.integrity.models import compute_hash

logger = structlog.get_logger()


class HashChainVerifier:  # verified-by: test_forge_without_key_fails
    """
    Verifies the integrity of a hash chain.

    Can detect:
    - Deleted entries (missing sequence numbers)
    - Modified entries (hash mismatch)
    - Reordered entries (previous_hash mismatch)
    """

    GENESIS_HASH = "GENESIS"

    def verify_chain(self, entries: list[dict[str, Any]]) -> tuple[bool, str | None]:
        """
        Verify the integrity of an audit log chain.

        Args:
            entries: List of log entries with integrity fields

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not entries:
            return True, None

        previous_hash = self.GENESIS_HASH
        expected_sequence = 1

        for _i, entry in enumerate(entries):
            # Check sequence continuity
            seq = entry.get("integrity", {}).get("sequence", 0)
            if seq != expected_sequence:
                return (
                    False,
                    f"Missing entry: expected sequence {expected_sequence}, found {seq}",
                )

            # Check previous hash linkage
            prev_hash = entry.get("integrity", {}).get("previous_hash", "")
            if prev_hash != previous_hash:
                return False, f"Chain broken at sequence {seq}: previous_hash mismatch"

            # Verify current hash
            stored_hash = entry.get("integrity", {}).get("current_hash", "")
            entry_copy = self._remove_current_hash(entry)
            computed_hash = compute_hash(entry_copy)

            if not hmac.compare_digest(
                stored_hash.encode("utf-8"), computed_hash.encode("utf-8")
            ):
                return False, f"Entry modified at sequence {seq}: hash mismatch"

            previous_hash = stored_hash
            expected_sequence += 1

        return True, None

    def _remove_current_hash(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Remove current_hash from entry for hash verification."""
        entry_copy = json.loads(json.dumps(entry))  # Deep copy
        if "integrity" in entry_copy and "current_hash" in entry_copy["integrity"]:
            del entry_copy["integrity"]["current_hash"]
        return entry_copy

    def find_tampering(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Find all tampered or missing entries in a chain.

        Returns:
            List of issues found
        """
        issues = []

        if not entries:
            return issues

        previous_hash = self.GENESIS_HASH
        expected_sequence = 1

        for entry in entries:
            seq = entry.get("integrity", {}).get("sequence", 0)

            # Check for missing entries
            while expected_sequence < seq:
                issues.append(
                    {
                        "type": "missing_entry",
                        "sequence": expected_sequence,
                        "message": f"Entry {expected_sequence} is missing from the chain",
                    }
                )
                expected_sequence += 1

            # Check previous hash
            prev_hash = entry.get("integrity", {}).get("previous_hash", "")
            if prev_hash != previous_hash:
                issues.append(
                    {
                        "type": "chain_broken",
                        "sequence": seq,
                        "expected_previous_hash": previous_hash,
                        "found_previous_hash": prev_hash,
                        "message": f"Chain broken at entry {seq}",
                    }
                )

            # Verify current hash
            stored_hash = entry.get("integrity", {}).get("current_hash", "")
            entry_copy = self._remove_current_hash(entry)
            computed_hash = compute_hash(entry_copy)

            if not hmac.compare_digest(
                stored_hash.encode("utf-8"), computed_hash.encode("utf-8")
            ):
                issues.append(
                    {
                        "type": "entry_modified",
                        "sequence": seq,
                        "stored_hash": stored_hash[:16] + "...",
                        "computed_hash": computed_hash[:16] + "...",
                        "message": f"Entry {seq} has been modified",
                    }
                )

            previous_hash = stored_hash
            expected_sequence = seq + 1

        return issues


def verify_audit_log_integrity(log_file: Path) -> tuple[bool, list[dict[str, Any]]]:
    """
    Verify the integrity of an audit log file.

    Args:
        log_file: Path to the JSON Lines audit log file

    Returns:
        Tuple of (is_valid, issues_list)
    """
    if not log_file.exists():
        return True, []

    entries = []
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except Exception as e:
        return False, [{"type": "read_error", "message": str(e)}]

    verifier = HashChainVerifier()
    issues = verifier.find_tampering(entries)

    return len(issues) == 0, issues


__all__ = ["HashChainVerifier", "verify_audit_log_integrity"]
