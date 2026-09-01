"""G86 — every baseline exemption carries an expiry, and expiries are honoured.

The SOFT strategy in the rule registry's Operating Model buys a gate its landing
by allowlisting the violations that already exist. That trade only pays if the
allowlist shrinks. It did not: the schema has carried an optional
``target_remove_by`` since it was written, exactly one entry ever used it, and
the list sat flat for months while gates ran on every commit — a majority
excused, a minority policed. This meta-gate makes the expiry slot load-bearing.

Three contracts:

- **Undated ratchet** — the number of entries with no machine-checkable
  ``target_remove_by`` is exact-matched against ``dateless_baseline_budget`` in
  ``baseline.yaml``. Above it: new undated debt, date it instead of raising the
  budget. Below it: a reduction that did not ratchet, lower the budget in the
  same change. Exact match (G41/G67 idiom, not G17b's ``<=``) is what stops the
  freed slack from being silently reclaimed by the next undated entry.
- **Expiry** — an entry whose ISO ``target_remove_by`` is in the past fails.
  Fix the violation, or move the date with a rationale at the diff.
- **Liveness** — an entry naming a file that does not exist, in a checkout whose
  root for that file IS present, fails. Nothing ever removed an exemption when
  its file was deleted or moved, so the list accumulated permanently-matching
  nothing rows (Kafka and Kubernetes adapters that had moved tiers, and
  ``baldur_dormant`` paths for rules whose scope never covered them).

``target_remove_by`` is honoured only in ISO ``YYYY-MM-DD`` form. A milestone
string (``post-v1.1``) stays legal in the file but counts as undated: it cannot
expire mechanically, which is how the field became decorative in the first
place, and letting it count as dated would reopen that hole.

Root resolution is checkout-aware: every root resolves under this repo, and an
entry whose root is absent from this checkout is skipped by the liveness check
rather than reported — the repo that ships that tier judges it.

Baseline is enforced-empty — this rule governs the baseline and may not exempt
itself.

Rule registry: ``ARCHITECTURE.md#g86-baseline-expiry-discipline``
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from tests.architecture._helpers import PROJECT_ROOT, _load_baseline_document

_RULE_ANCHOR = "#g86-baseline-expiry-discipline"
_BUDGET_KEY = "dateless_baseline_budget"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Path prefix -> the root that prefix names in THIS checkout. `src/baldur/` is
# resolved through the import system because the OSS core is an installed
# sibling here and in-tree in the public repo.
_ROOT_PREFIXES: tuple[str, ...] = (
    "src/baldur/",
    "src/baldur_pro/",
    "src/baldur_dormant/",
)


def _root_for(prefix: str) -> Path:
    return PROJECT_ROOT / prefix.rstrip("/")


def _iter_entries() -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(rule_key, entry)`` for every mapping entry in the baseline."""
    document = _load_baseline_document()
    entries: list[tuple[str, dict[str, Any]]] = []
    for rule_key, value in document.items():
        if not isinstance(value, list):
            continue
        for entry in value:
            if isinstance(entry, dict):
                entries.append((rule_key, entry))
    return entries


def _expiry(entry: dict[str, Any]) -> dt.date | None:
    """Return the entry's ISO expiry date, or None when it has no usable one."""
    value = entry.get("target_remove_by")
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and _ISO_DATE.match(value):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


class TestBaselineExpiryDiscipline:
    """G86 — baseline exemptions expire, and expired ones fail the suite."""

    def test_baseline_is_not_vacuous(self):
        entries = _iter_entries()
        assert entries, (
            f"G86 ({_RULE_ANCHOR}): the baseline parsed to zero entries — the "
            "document moved or failed to load, and the three contracts below "
            "would pass vacuously."
        )

    def test_undated_entry_count_matches_budget(self):
        undated = [
            (rule_key, entry.get("file"))
            for rule_key, entry in _iter_entries()
            if _expiry(entry) is None
        ]
        observed = len(undated)

        document = _load_baseline_document()
        budget = document.get(_BUDGET_KEY)
        assert isinstance(budget, int), (
            f"G86 ({_RULE_ANCHOR}): baseline.yaml must define an integer "
            f"`{_BUDGET_KEY}`. Add `{_BUDGET_KEY}: {observed}`."
        )

        assert observed <= budget, (
            f"G86 ({_RULE_ANCHOR}): undated baseline entries grew from "
            f"{budget} to {observed}. A new exemption needs an ISO "
            "`target_remove_by: YYYY-MM-DD` — the date is what makes the "
            "allowlist shrink. Raising the budget instead reopens the leak "
            "this rule exists to close.\n"
            + "\n".join(f"  {key}: {file}" for key, file in undated[-8:])
        )
        assert observed >= budget, (
            f"G86 ({_RULE_ANCHOR}): undated baseline entries dropped from "
            f"{budget} to {observed} without ratcheting the budget. Set "
            f"`{_BUDGET_KEY}: {observed}` in the same change, so the freed "
            "slack cannot be silently reclaimed by the next undated entry."
        )

    def test_no_expired_exemption(self):
        today = dt.date.today()
        expired = [
            f"  {rule_key}: {entry.get('file')} (due {expiry.isoformat()})"
            for rule_key, entry in _iter_entries()
            if (expiry := _expiry(entry)) is not None and expiry < today
        ]
        assert not expired, (
            f"G86 ({_RULE_ANCHOR}): {len(expired)} baseline exemption(s) are "
            "past their `target_remove_by`. Fix the underlying violation and "
            "delete the entry, or move the date with a rationale reviewed at "
            "the diff.\n" + "\n".join(expired)
        )

    def test_no_entry_for_a_file_that_does_not_exist(self):
        dead: list[str] = []
        for rule_key, entry in _iter_entries():
            file_value = entry.get("file")
            if not isinstance(file_value, str):
                continue
            posix = file_value.replace("\\", "/")
            prefix = next((p for p in _ROOT_PREFIXES if posix.startswith(p)), None)
            if prefix is None:
                continue
            root = _root_for(prefix)
            if not root.is_dir():
                # Root absent from this checkout — the other repo's copy judges it.
                continue
            if not (root / posix[len(prefix) :]).exists():
                dead.append(f"  {rule_key}: {posix}")
        assert not dead, (
            f"G86 ({_RULE_ANCHOR}): {len(dead)} baseline entry(ies) name a file "
            "that no longer exists. An exemption outlives its file silently and "
            "forever — delete the row when the file is deleted, moved, or "
            "retiered.\n" + "\n".join(dead)
        )
