"""
Sampling Verifier (O(n) → O(k)).

Provides probabilistic chain verification using sampling.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.settings.sampling import SamplingSettings

logger = structlog.get_logger()


@dataclass
class SamplingConfig:
    """Configuration for sampling verification."""

    sample_rate: float = 0.1  # 10% sampling
    min_samples: int = 10
    max_samples: int = 1000
    full_verify_on_failure: bool = True

    @classmethod
    def from_settings(
        cls,
        settings: SamplingSettings | None = None,
        **overrides,
    ) -> SamplingConfig:
        """
        Settings에서 SamplingConfig 인스턴스 생성.

        Args:
            settings: SamplingSettings 인스턴스 (없으면 싱글톤 사용)
            **overrides: 개별 필드 오버라이드

        Returns:
            SamplingConfig: Settings 기반 인스턴스
        """
        from baldur.settings.sampling import get_sampling_settings

        s = settings or get_sampling_settings()
        return cls(
            sample_rate=overrides.get("sample_rate", s.sample_rate),
            min_samples=overrides.get("min_samples", s.min_samples),
            max_samples=overrides.get("max_samples", s.max_samples),
            full_verify_on_failure=overrides.get(
                "full_verify_on_failure", s.full_verify_on_failure
            ),
        )


class SamplingVerifier:
    """
    Probabilistic chain verification using sampling.

    Problem:
        Full chain verification is O(n) for n entries.
        Millions of entries = seconds/minutes of CPU time.

    Solution:
        Random sampling reduces to O(k) where k = n × sample_rate.
        If sample passes, chain is likely valid.
        If sample fails, fall back to full verification.

    Pattern source:
        throttle/config.py#L26 (sample_rate concept)
        runtime_config/core_configs.py#L215 (sampling intervals)

    Usage:
        verifier = SamplingVerifier(SamplingConfig(sample_rate=0.1))
        is_valid, issues = verifier.verify_sampled(entries)
    """

    GENESIS_HASH = "GENESIS"

    def __init__(self, config: SamplingConfig | None = None):
        """
        Initialize sampling verifier.

        Args:
            config: Sampling configuration
        """
        self._config = config or SamplingConfig()

    def verify_sampled(
        self,
        entries: list[dict[str, Any]],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """
        Verify chain using probabilistic sampling.

        Args:
            entries: List of log entries to verify

        Returns:
            Tuple of (is_valid, issues_found)
        """
        if not entries:
            return True, []

        # Calculate sample size
        n = len(entries)
        sample_size = max(
            self._config.min_samples,
            min(
                self._config.max_samples,
                int(n * self._config.sample_rate),
            ),
        )

        # Can't sample more than we have
        sample_size = min(sample_size, n)

        # Select random sample indices
        if sample_size >= n:
            sample_indices = list(range(n))
        else:
            sample_indices = sorted(random.sample(range(n), sample_size))

        logger.debug(
            "sampling_verifier.sampling_entries",
            sample_size=sample_size,
            population_count=n,
            sample_rate_pct=sample_size / n * 100,
        )

        # Verify sampled entries
        issues = self._verify_indices(entries, sample_indices)

        if issues and self._config.full_verify_on_failure:
            # Sample failed - do full verification
            logger.warning("sampling_verifier.sample_verification_failed_performing")
            return self._verify_full(entries)

        return len(issues) == 0, issues

    def _verify_indices(
        self,
        entries: list[dict[str, Any]],
        indices: list[int],
    ) -> list[dict[str, Any]]:
        """Verify specific indices in the chain."""
        issues = []

        for i in indices:
            entry = entries[i]
            integrity = entry.get("integrity", {})

            # Verify hash computation
            stored_hash = integrity.get("current_hash", "")
            entry_copy = self._remove_current_hash(entry)
            computed_hash = self._compute_hash(entry_copy)

            if stored_hash != computed_hash:
                issues.append(
                    {
                        "type": "hash_mismatch",
                        "index": i,
                        "sequence": integrity.get("sequence"),
                        "stored_hash": stored_hash[:16] + "...",
                        "computed_hash": computed_hash[:16] + "...",
                    }
                )

            # Verify chain linkage (if not first entry)
            if i > 0:
                prev_entry = entries[i - 1]
                expected_prev = prev_entry.get("integrity", {}).get("current_hash", "")
                actual_prev = integrity.get("previous_hash", "")

                if expected_prev and actual_prev != expected_prev:
                    issues.append(
                        {
                            "type": "chain_broken",
                            "index": i,
                            "sequence": integrity.get("sequence"),
                            "expected_prev": expected_prev[:16] + "...",
                            "actual_prev": actual_prev[:16] + "...",
                        }
                    )

        return issues

    def _verify_full(
        self,
        entries: list[dict[str, Any]],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Full chain verification."""
        issues = []
        previous_hash = self.GENESIS_HASH

        for i, entry in enumerate(entries):
            integrity = entry.get("integrity", {})

            # Check previous hash linkage
            prev_hash = integrity.get("previous_hash", "")
            if prev_hash != previous_hash:
                issues.append(
                    {
                        "type": "chain_broken",
                        "index": i,
                        "sequence": integrity.get("sequence"),
                        "expected_prev": previous_hash[:16] + "...",
                        "actual_prev": prev_hash[:16] + "...",
                    }
                )

            # Verify hash
            stored_hash = integrity.get("current_hash", "")
            entry_copy = self._remove_current_hash(entry)
            computed_hash = self._compute_hash(entry_copy)

            if stored_hash != computed_hash:
                issues.append(
                    {
                        "type": "hash_mismatch",
                        "index": i,
                        "sequence": integrity.get("sequence"),
                    }
                )

            previous_hash = stored_hash

        return len(issues) == 0, issues

    def _remove_current_hash(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Remove current_hash for verification."""
        entry_copy = json.loads(json.dumps(entry))
        if "integrity" in entry_copy and "current_hash" in entry_copy["integrity"]:
            del entry_copy["integrity"]["current_hash"]
        return entry_copy

    def _compute_hash(self, data: dict[str, Any]) -> str:
        """Delegate to the single keyed-aware chain hash (integrity/models).

        The caller strips ``current_hash`` before passing ``data`` in, so this
        only forwards. Routing through ``compute_hash`` keeps the verify-side
        recompute coherent with the keyed write path.
        """
        from baldur.audit.integrity.models import compute_hash

        return compute_hash(data)
