"""G15 — Env-var prefix naming contract for `BaseSettings` subclasses.

Per impl doc 508 (Wave 6A API surface freeze) the operator-facing env-var
surface is locked: every `pydantic_settings.BaseSettings` subclass under
`src/baldur/settings/` MUST satisfy three rules.

1. **Unique `env_prefix`** (G1) — two classes cannot share the same prefix.
2. **No bare `BALDUR_`** (G2 + G16) — the top-level namespace is reserved.
3. **Prefix equals uppercase module name** (G8 + G9) — the only allowed
   token substitutions are the industry-standard abbreviations `CB` and
   `DLQ`. The optional `settings_` filename infix is stripped before the
   uppercase comparison so `settings_dependency.py` may keep
   `BALDUR_DEPENDENCY_`. Trailing underscores on both sides are normalised
   before equality.

Detection is runtime reflection — import `baldur.settings` (which eagerly
collects every settings module), walk `BaseSettings.__subclasses__()`
transitively, and read each class's ``model_config['env_prefix']``. This
covers both `make_settings_config(...)` callers and direct
`SettingsConfigDict(env_prefix=...)` users via a single mechanism.

Rule registry: ``ARCHITECTURE.md#g15-env-prefix-naming``
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from pathlib import Path

from baldur.settings.introspection import collect_baldur_settings
from tests.architecture.conftest import (
    PROJECT_ROOT,
    collect_violations,
)

_RULE_KEY = "env_prefix_naming"
_RULE_ANCHOR = "#g15-env-prefix-naming"

_ALLOWED_ABBREVIATIONS = frozenset({"CB", "DLQ"})
_BALDUR_NAMESPACE = "BALDUR_"
_SETTINGS_INFIX = "SETTINGS_"


def _class_relpath(cls: type) -> str:
    try:
        source_file = inspect.getsourcefile(cls)
    except TypeError:
        return cls.__module__
    if source_file is None:
        return cls.__module__
    try:
        return Path(source_file).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return Path(source_file).as_posix()


def _class_lineno(cls: type) -> int | None:
    try:
        _, lineno = inspect.getsourcelines(cls)
        return lineno
    except (OSError, TypeError):
        return None


def _expected_prefix_from_module(module_name: str) -> str:
    """Compute the expected ``BALDUR_<MODULE>_`` prefix from a module path.

    Strips the leading ``settings_`` filename infix when present so
    `settings_dependency.py` produces `BALDUR_DEPENDENCY_`.
    """
    leaf = module_name.rsplit(".", 1)[-1]
    upper = leaf.upper() + "_"
    if upper.startswith(_SETTINGS_INFIX):
        upper = upper[len(_SETTINGS_INFIX) :]
    return _BALDUR_NAMESPACE + upper


_ABBREVIATION_EXPANSIONS = {
    "CB": ("CIRCUIT", "BREAKER"),
    "DLQ": ("DEAD", "LETTER", "QUEUE"),
}


def _tokens_match(actual: str, expected: str) -> bool:
    """Compare two ``BALDUR_<...>_`` prefixes token-by-token.

    Tokens match exactly OR pair across the abbreviation whitelist
    (``CB`` ↔ ``CIRCUIT_BREAKER`` is the only legal substitution per D8).
    The whitelist lets prefixes that use the industry-standard short form
    survive the strict-equality check when the module name expands the
    abbreviation, and vice versa.
    """
    actual_tokens = [t for t in actual.strip("_").split("_") if t]
    expected_tokens = [t for t in expected.strip("_").split("_") if t]
    a, e = 0, 0
    while a < len(actual_tokens) and e < len(expected_tokens):
        if actual_tokens[a] == expected_tokens[e]:
            a += 1
            e += 1
            continue
        if actual_tokens[a] in _ABBREVIATION_EXPANSIONS:
            expansion = _ABBREVIATION_EXPANSIONS[actual_tokens[a]]
            if tuple(expected_tokens[e : e + len(expansion)]) == expansion:
                a += 1
                e += len(expansion)
                continue
        if expected_tokens[e] in _ABBREVIATION_EXPANSIONS:
            expansion = _ABBREVIATION_EXPANSIONS[expected_tokens[e]]
            if tuple(actual_tokens[a : a + len(expansion)]) == expansion:
                a += len(expansion)
                e += 1
                continue
        return False
    return a == len(actual_tokens) and e == len(expected_tokens)


class TestEnvPrefixNamingContract:
    """G15 — operator-facing env-var prefix surface is locked."""

    def test_env_prefix_unique(self):
        """G1 — two settings classes MUST NOT share the same env_prefix."""
        by_prefix: dict[str, list[type]] = defaultdict(list)
        for cls, prefix in collect_baldur_settings():
            by_prefix[prefix].append(cls)

        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for prefix, classes in by_prefix.items():
            if len(classes) <= 1:
                continue
            for cls in classes:
                others = ", ".join(
                    f"{c.__module__}.{c.__name__}" for c in classes if c is not cls
                )
                raw.append(
                    (
                        Path(_class_relpath(cls)),
                        _class_lineno(cls),
                        None,
                        f"{cls.__name__} shares env_prefix={prefix!r} with: {others}",
                    )
                )

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G15: env_prefix collisions ({len(violations)}). "
            "Rename one of the colliding classes' env_prefix or add a baseline "
            "entry under `env_prefix_naming:` with reason+ticket.\n"
            + "\n".join(violations)
        )

    def test_no_bare_baldur_prefix(self):
        """G2 + G16 — env_prefix MUST NOT be the bare ``BALDUR_`` top-level namespace."""
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for cls, prefix in collect_baldur_settings():
            if prefix == _BALDUR_NAMESPACE:
                raw.append(
                    (
                        Path(_class_relpath(cls)),
                        _class_lineno(cls),
                        None,
                        f"{cls.__name__} uses bare env_prefix={prefix!r} — "
                        "pick a scoped two-part prefix",
                    )
                )

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G15: bare BALDUR_ prefix ({len(violations)}). "
            "Pick a scoped two-part prefix (e.g., BALDUR_LICENSE_) so the "
            "top-level BALDUR_ namespace stays reserved for cross-cutting vars.\n"
            + "\n".join(violations)
        )

    def test_module_prefix_alignment(self):
        """G9 — env_prefix MUST equal uppercase module name (CB/DLQ whitelist aside).

        The optional ``settings_`` filename infix is stripped before the
        comparison. Token-level equality applies; the abbreviation whitelist
        from D8 permits ``CB`` / ``DLQ`` tokens to stand in place of expanded
        forms when the module name itself uses the abbreviation.
        """
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for cls, prefix in collect_baldur_settings():
            expected = _expected_prefix_from_module(cls.__module__)
            if _tokens_match(prefix, expected):
                continue
            raw.append(
                (
                    Path(_class_relpath(cls)),
                    _class_lineno(cls),
                    None,
                    f"{cls.__name__} env_prefix={prefix!r} does not match "
                    f"module-derived expectation {expected!r}",
                )
            )

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G15: module/prefix divergence ({len(violations)}). "
            "Rename the prefix to match the uppercase module name, rename the "
            "module file to match the prefix, or add a baseline entry under "
            "`env_prefix_naming:` with reason+ticket.\n" + "\n".join(violations)
        )
