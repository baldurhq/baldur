"""G30 — operator-tunable env-var allowlist MUST resolve to real settings fields.

Impl doc 568. The operator-tunable ``BALDUR_*`` allowlist is published on two
gated surfaces: ``docs/reference/env-vars.md`` (the canonical allowlist) and the
Configuration tables of the public concept guides under ``docs/concepts/``. A
phantom entry — a documented ``BALDUR_*`` var that maps to no Pydantic settings
field — is an operator-facing silent no-op: the operator sets it believing they
are protected, but the value is ignored (every settings class uses
``extra="ignore"``). 568 fixed five such entries by hand; this rule keeps them
from re-appearing.

**Resolution algorithm (longest-prefix match).** Every ``BaseSettings`` subclass
declares an ``env_prefix`` (``BALDUR_DLQ_`` etc.). A documented var ``V`` resolves
iff: take the LONGEST registered prefix ``P`` that ``V`` starts with, drop it, and
the remainder (lowercased) is a field on some class with prefix ``P``. The
longest-prefix rule is load-bearing for nested prefixes — ``BALDUR_DLQ_OUTBOX_``
must win over ``BALDUR_DLQ_`` for ``BALDUR_DLQ_OUTBOX_ENABLED``, and
``BALDUR_AUDIT_WATCHDOG_`` over ``BALDUR_AUDIT_``.

**Two surfaces, two strictnesses (568 D7).**

* ``env-vars.md`` fenced-``bash`` blocks — NAME-ONLY resolution. The values there
  are examples / placeholders (``<base64>``, ``...``) or set-to-enable guidance
  (``BALDUR_AUDIT_ENABLED=true`` while the field default is ``False``), so value
  parity is inapplicable.
* concept-guide Configuration tables (a ``Default`` column + a first cell holding
  an inline-code ``BALDUR_*``) — NAME + DEFAULT-value parity. The table asserts the
  field default, so the cell MUST equal ``model_fields[field].default`` (scalars
  only; a non-scalar default skips parity).

**Module loading.** ``baldur.settings``'s package ``__init__`` does NOT eagerly
import every settings submodule (e.g. ``dlq_outbox.py`` stays unloaded), so
``BaseSettings.__subclasses__()`` is incomplete after a bare ``import
baldur.settings``. The gate force-imports every ``baldur.settings.*`` submodule
first so the prefix index covers the whole OSS settings surface. All allowlist
vars resolve via ``baldur.settings.*`` classes (incl. ``MetaWatchdogSettings`` in
the OSS tree), so the gate passes in an OSS-only checkout with no ``baldur_pro``.

**Scope (568 D8).** ``docs/reference/index.md`` is NOT gated (its Migration Guide
intentionally lists obsolete pre-rename names that must not resolve; 568 deduped
its parallel allowlist away). ``docs/getting-started/**`` is NOT gated (it carries
``BALDUR_LOG_LEVEL``, a real ``os.environ`` var with no Pydantic field).

**Baseline granularity** — ENFORCED-EMPTY (``env_vars_allowlist_resolves: []``). A
phantom var is FIXED (renamed to the real var or removed), never baselined.

Rule registry: ``ARCHITECTURE.md#g30-env-vars-allowlist-resolves``
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from baldur.settings.introspection import (
    collect_baldur_settings,
    force_load_settings_modules,
)
from tests.architecture.conftest import (
    PROJECT_ROOT,
    REFERENCE_DIR,
    collect_violations,
)

_RULE_KEY = "env_vars_allowlist_resolves"
_RULE_ANCHOR = "#g30-env-vars-allowlist-resolves"

# Gated surfaces.
_ENV_VARS_DOC = REFERENCE_DIR / "env-vars.md"
_CONCEPTS_DIR = PROJECT_ROOT / "docs" / "concepts"

# Fenced-code language tags whose ``KEY=value`` lines are extracted name-only.
_BASH_FENCE_LANGS = frozenset({"bash", "sh", "shell", "console"})

_FENCE_RE = re.compile(r"^\s*```+\s*(\w*)")
# Line-anchored so a commented example (`# BALDUR_...=`) inside a bash block is
# ignored — a commented line documents a value, it does not advertise an entry.
_BASH_VAR_RE = re.compile(r"^\s*(BALDUR_[A-Z0-9_]+)=")
# Inline-code ``BALDUR_*`` token inside a markdown table cell.
_INLINE_VAR_RE = re.compile(r"`\s*(BALDUR_[A-Z0-9_]+)\s*`")
# A markdown table separator cell: ``---`` / ``:---:`` / ``---:``.
_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")

_SCALAR_TYPES: tuple[type, ...] = (bool, int, float, str)


# ---------------------------------------------------------------------------
# Settings prefix index (force-load → reflect).
# ---------------------------------------------------------------------------
def _build_prefix_index() -> dict[str, dict[str, Any]]:
    """Return ``{env_prefix: {field_name: field_default}}`` for the OSS surface.

    Reuses the production ``collect_baldur_settings()`` after force-loading
    every settings submodule (``force_load_settings_modules()``) so the index is
    complete. Field defaults carry the ``FieldInfo.default`` value
    (``PydanticUndefined`` for required fields), consumed by the default-parity
    check. (G30 keeps its own field-default index — the runtime scan's
    ``build_prefix_index`` carries field names only.)
    """
    force_load_settings_modules()
    index: dict[str, dict[str, Any]] = {}
    for cls, prefix in collect_baldur_settings():
        bucket = index.setdefault(prefix, {})
        for field_name, field_info in cls.model_fields.items():
            bucket.setdefault(field_name, field_info.default)
    return index


def _longest_prefix(var: str, prefixes: Iterable[str]) -> str | None:
    """Return the longest registered prefix that ``var`` starts with, or None."""
    best: str | None = None
    for prefix in prefixes:
        if var.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return best


def _resolve(var: str, index: dict[str, dict[str, Any]]) -> bool:
    """True iff ``var`` maps to a real ``(env_prefix, field)`` (longest-prefix)."""
    prefix = _longest_prefix(var, index)
    if prefix is None:
        return False
    field = var[len(prefix) :].lower()
    return field in index[prefix]


def _field_default(var: str, index: dict[str, dict[str, Any]]) -> Any:
    """Return the resolved field default for ``var`` (caller pre-checks resolve)."""
    prefix = _longest_prefix(var, index)
    assert prefix is not None
    return index[prefix][var[len(prefix) :].lower()]


def _known_var_names(index: dict[str, dict[str, Any]]) -> list[str]:
    """Every ``PREFIX + FIELD`` env-var name, for ``difflib`` near-match hints."""
    return [
        prefix + field.upper() for prefix, fields in index.items() for field in fields
    ]


def _default_matches(field_default: Any, cell_text: str) -> bool:
    """True iff a concept-guide ``Default`` cell equals the field default.

    Scalars only (``int``/``float``/``bool``/``str``). A non-scalar default
    (``list``/``dict``/``None``/``PydanticUndefined``) skips parity — returns
    True — rather than attempting a brittle serialized compare. ``bool`` is
    checked before ``int`` (``bool`` is an ``int`` subclass).
    """
    if not isinstance(field_default, _SCALAR_TYPES):
        return True
    normalized = cell_text.strip().strip("`").strip()
    if isinstance(field_default, bool):
        return str(field_default).lower() == normalized.lower()
    if isinstance(field_default, (int, float)):
        try:
            return float(field_default) == float(normalized)
        except ValueError:
            return False
    return str(field_default).strip().lower() == normalized.lower()


# ---------------------------------------------------------------------------
# Surface extraction.
# ---------------------------------------------------------------------------
def _split_table_row(line: str) -> list[str]:
    """Split a markdown table row into its cells (outer pipes stripped)."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return stripped.split("|")


def _is_table_separator(line: str) -> bool:
    """True iff ``line`` is a markdown table separator (``|---|:--:|``)."""
    if "|" not in line or "-" not in line:
        return False
    cells = [c.strip() for c in _split_table_row(line)]
    nonempty = [c for c in cells if c]
    return bool(nonempty) and all(_SEP_CELL_RE.match(c) for c in nonempty)


def _extract_env_var_specs(path: Path) -> list[tuple[str, int, str | None]]:
    """Return ``[(var, line, default_cell_or_None), ...]`` for one doc file.

    Two extraction shapes over the same file:

    * fenced-``bash`` blocks → ``^\\s*BALDUR_<NAME>=`` lines, ``default=None``
      (name-only resolution);
    * markdown tables carrying a ``Default`` column → rows whose first cell holds
      an inline-code ``BALDUR_*``, ``default`` = that row's ``Default`` cell.

    Pure: the path is the only input and the returned list is fully inspectable.
    """
    specs: list[tuple[str, int, str | None]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return specs

    in_fence = False
    in_bash = False
    in_table = False
    default_col: int | None = None

    for i, line in enumerate(lines):
        lineno = i + 1

        fence = _FENCE_RE.match(line)
        if fence:
            if in_fence:
                in_fence = in_bash = False
            else:
                in_fence = True
                in_bash = fence.group(1).lower() in _BASH_FENCE_LANGS
            in_table = False
            default_col = None
            continue

        if in_fence:
            if in_bash:
                match = _BASH_VAR_RE.match(line)
                if match:
                    specs.append((match.group(1), lineno, None))
            continue

        if "|" in line:
            if _is_table_separator(line):
                # The header is the preceding line; locate its Default column.
                header = _split_table_row(lines[i - 1]) if i > 0 else []
                default_col = next(
                    (
                        idx
                        for idx, cell in enumerate(header)
                        if cell.strip().strip("`").strip().lower() == "default"
                    ),
                    None,
                )
                in_table = True
                continue
            if in_table:
                cells = _split_table_row(line)
                match = _INLINE_VAR_RE.search(cells[0]) if cells else None
                if match is not None:
                    default_cell = (
                        cells[default_col].strip()
                        if default_col is not None and default_col < len(cells)
                        else None
                    )
                    specs.append((match.group(1), lineno, default_cell))
            continue

        # A non-table, non-fence line ends any in-progress table.
        in_table = False
        default_col = None

    return specs


def _concept_guide_paths() -> list[Path]:
    """Published concept guides — ``docs/concepts/**/*.md`` minus ``_``-prefixed.

    A path component starting with ``_`` (``_TEMPLATE.md``, an ``_drafts/`` dir)
    is unpublished by mkdocs convention and excluded — ``_TEMPLATE.md`` in
    particular would carry instructional examples.
    """
    if not _CONCEPTS_DIR.exists():
        return []
    paths: list[Path] = []
    for path in sorted(_CONCEPTS_DIR.rglob("*.md")):
        rel = path.relative_to(_CONCEPTS_DIR)
        if any(part.startswith("_") for part in rel.parts):
            continue
        paths.append(path)
    return paths


def _gated_doc_paths() -> list[Path]:
    """All surfaces G30 resolves names over: ``env-vars.md`` + concept guides."""
    paths: list[Path] = []
    if _ENV_VARS_DOC.exists():
        paths.append(_ENV_VARS_DOC)
    paths.extend(_concept_guide_paths())
    return paths


class TestEnvVarsAllowlistResolves:
    """G30 — the operator-tunable allowlist stays in sync with real fields."""

    def test_every_allowlist_var_resolves(self):
        """Every documented ``BALDUR_*`` resolves to a real ``(prefix, field)``."""
        index = _build_prefix_index()
        known = _known_var_names(index)

        # Anti-vacuous guard: env-vars.md is the canonical, always-populated
        # allowlist — empty specs mean extraction broke, not a clean surface.
        assert _extract_env_var_specs(_ENV_VARS_DOC), (
            "G30: env-vars.md yielded no BALDUR_* specs — extraction is broken"
        )

        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in _gated_doc_paths():
            for var, line, _default in _extract_env_var_specs(path):
                if _resolve(var, index):
                    continue
                suggestion = difflib.get_close_matches(var, known, n=1)
                hint = f" — did you mean {suggestion[0]}?" if suggestion else ""
                raw.append(
                    (
                        path,
                        line,
                        None,
                        f"{var} resolves to no (env_prefix, field){hint}",
                    )
                )

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G30: phantom env-var allowlist entries ({len(violations)}). "
            "Rename the var to the real BALDUR_<PREFIX>_<FIELD> or remove it — "
            "a documented var with no backing field is a silent operator no-op.\n"
            + "\n".join(violations)
        )

    def test_concept_guide_defaults_match_fields(self):
        """Each concept-guide ``Default`` cell equals the field's default."""
        index = _build_prefix_index()

        checked = 0
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in _concept_guide_paths():
            for var, line, default_cell in _extract_env_var_specs(path):
                if default_cell is None or not _resolve(var, index):
                    # Unresolved names are owned by the resolution test above.
                    continue
                checked += 1
                field_default = _field_default(var, index)
                if _default_matches(field_default, default_cell):
                    continue
                raw.append(
                    (
                        path,
                        line,
                        None,
                        f"{var} Default cell {default_cell!r} != field default "
                        f"{field_default!r}",
                    )
                )

        # Anti-vacuous guard: the published concept guides carry Default-column
        # Configuration tables, so at least one parity check must have run.
        assert checked, (
            "G30: no concept-guide Default cells were checked — table extraction "
            "is broken or the Configuration tables vanished"
        )

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"G30: concept-guide Default cells drifted from field defaults "
            f"({len(violations)}). Update the guide cell to the real "
            "model_fields[field].default (or fix the field).\n" + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# Synthetic-input unit fixtures for the pure helpers (568 Test Assessment).
#
# The live-surface methods above ARE the regression gate; these guard the
# resolver / parity / extractor logic against a SILENT PASS from a helper bug
# (a broken matcher would let a phantom slip through unnoticed). They mirror
# G29's TestProseLeakMatcher precedent in the same gate file: synthetic inputs,
# no live docs, no baseline. Type: Behavior — they exercise the helpers'
# branches directly rather than asserting design-doc constants.
# ---------------------------------------------------------------------------

# A synthetic prefix index whose two overlapping prefix pairs mirror the real
# nested-prefix shape (BALDUR_DLQ_ vs BALDUR_DLQ_OUTBOX_, BALDUR_AUDIT_ vs
# BALDUR_AUDIT_WATCHDOG_) that makes longest-prefix resolution load-bearing.
# BALDUR_AUDIT_ deliberately carries `enabled` while BALDUR_AUDIT_WATCHDOG_
# does NOT — the exact shape that would false-resolve the G3 phantom
# BALDUR_AUDIT_WATCHDOG_ENABLED if the resolver fell back to a shorter prefix.
_SYNTHETIC_INDEX: dict[str, dict[str, Any]] = {
    "BALDUR_DLQ_": {"max_size": 100_000, "enabled": True},
    "BALDUR_DLQ_OUTBOX_": {"enabled": True},
    "BALDUR_AUDIT_": {"enabled": False},
    "BALDUR_AUDIT_WATCHDOG_": {"missed_threshold": 3},
}


class TestEnvVarResolution:
    """``_longest_prefix`` / ``_resolve`` — nested-prefix resolution is exact."""

    def test_longest_prefix_wins_over_shorter_overlapping_prefix(self):
        # BALDUR_DLQ_OUTBOX_ shares the BALDUR_DLQ_ stem; the longer must win.
        assert (
            _longest_prefix("BALDUR_DLQ_OUTBOX_ENABLED", _SYNTHETIC_INDEX)
            == "BALDUR_DLQ_OUTBOX_"
        )
        assert (
            _longest_prefix("BALDUR_AUDIT_WATCHDOG_MISSED_THRESHOLD", _SYNTHETIC_INDEX)
            == "BALDUR_AUDIT_WATCHDOG_"
        )

    def test_longest_prefix_unregistered_var_returns_none(self):
        assert _longest_prefix("BALDUR_UNKNOWN_FOO", _SYNTHETIC_INDEX) is None

    def test_longest_prefix_empty_index_returns_none(self):
        assert _longest_prefix("BALDUR_DLQ_MAX_SIZE", {}) is None

    def test_resolve_field_under_longest_prefix_is_true(self):
        # Resolves ONLY because longest-prefix selects BALDUR_DLQ_OUTBOX_; the
        # remainder `enabled` is a field there. Under BALDUR_DLQ_ the remainder
        # would be `outbox_enabled` (absent) — the false-phantom the rule avoids.
        assert _resolve("BALDUR_DLQ_OUTBOX_ENABLED", _SYNTHETIC_INDEX) is True

    def test_resolve_does_not_fall_back_to_shorter_prefix_field(self):
        # The G3 phantom: BALDUR_AUDIT_WATCHDOG_ has no `enabled`, and the rule
        # MUST NOT fall back to BALDUR_AUDIT_.enabled (which DOES exist).
        # Longest-prefix is load-bearing for rejecting this exact var.
        assert "enabled" in _SYNTHETIC_INDEX["BALDUR_AUDIT_"]
        assert _resolve("BALDUR_AUDIT_WATCHDOG_ENABLED", _SYNTHETIC_INDEX) is False

    def test_resolve_unknown_field_under_correct_prefix_is_false(self):
        # The G1 phantom: max_entries is not a field under BALDUR_DLQ_.
        assert _resolve("BALDUR_DLQ_MAX_ENTRIES", _SYNTHETIC_INDEX) is False

    def test_resolve_unregistered_prefix_is_false(self):
        assert _resolve("BALDUR_NOPE_FIELD", _SYNTHETIC_INDEX) is False


class TestDefaultMatches:
    """``_default_matches`` — scalar parity with bool-before-int dispatch."""

    @pytest.mark.parametrize(
        ("field_default", "cell_text", "expected"),
        [
            pytest.param(3, "3", True, id="int-exact"),
            pytest.param(3, "4", False, id="int-mismatch"),
            pytest.param(3, "`3`", True, id="int-backtick-stripped"),
            pytest.param(60, " 60 ", True, id="int-whitespace-stripped"),
            pytest.param(3, "abc", False, id="int-nonnumeric-cell-no-raise"),
            pytest.param(1.0, "1.0", True, id="float-exact"),
            pytest.param(1.0, "0.5", False, id="float-mismatch-G4-class"),
            pytest.param(1.0, "1", True, id="float-vs-int-cell-equal"),
            pytest.param(1.0, "high", False, id="float-nonnumeric-cell-no-raise"),
            pytest.param(True, "true", True, id="bool-true"),
            pytest.param(False, "false", True, id="bool-false"),
            pytest.param(True, "True", True, id="bool-case-insensitive"),
            pytest.param(True, "false", False, id="bool-mismatch"),
            # Dispatch boundary: True is compared as the string "true", NOT as
            # the int 1 (bool is an int subclass, checked first).
            pytest.param(True, "1", False, id="bool-not-int-1"),
            pytest.param(False, "0", False, id="bool-not-int-0"),
            pytest.param("WARNING", "WARNING", True, id="str-exact"),
            pytest.param("WARNING", "warning", True, id="str-case-insensitive"),
            pytest.param("WARNING", "`WARNING`", True, id="str-backtick-stripped"),
            pytest.param("WARNING", "INFO", False, id="str-mismatch"),
            # Non-scalar defaults skip parity (return True), never a brittle
            # serialized compare.
            pytest.param([1, 2], "anything", True, id="nonscalar-list-skips"),
            pytest.param({"a": 1}, "x", True, id="nonscalar-dict-skips"),
            pytest.param(None, "x", True, id="nonscalar-none-skips"),
        ],
    )
    def test_default_matches(self, field_default, cell_text, expected):
        assert _default_matches(field_default, cell_text) is expected


class TestEnvVarExtraction:
    """``_extract_env_var_specs`` — two shapes, comment exclusion, fence/table state."""

    @staticmethod
    def _write(tmp_path: Path, text: str) -> Path:
        path = tmp_path / "sample.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_bash_fence_key_value_is_name_only_spec(self, tmp_path):
        path = self._write(tmp_path, "```bash\nBALDUR_DLQ_MAX_SIZE=100000\n```\n")
        assert _extract_env_var_specs(path) == [("BALDUR_DLQ_MAX_SIZE", 2, None)]

    def test_commented_bash_line_is_excluded(self, tmp_path):
        # Line-anchored regex requires the var at column start; a `# ...` example
        # documents a value, it does not advertise an allowlist entry.
        path = self._write(tmp_path, "```bash\n# BALDUR_DLQ_MAX_SIZE=100000\n```\n")
        assert _extract_env_var_specs(path) == []

    @pytest.mark.parametrize(
        ("lang", "extracted"),
        [
            ("bash", True),
            ("sh", True),
            ("shell", True),
            ("console", True),
            ("python", False),
            ("yaml", False),
            ("", False),
        ],
    )
    def test_fence_language_gates_extraction(self, tmp_path, lang, extracted):
        path = self._write(tmp_path, f"```{lang}\nBALDUR_DLQ_MAX_SIZE=100000\n```\n")
        assert bool(_extract_env_var_specs(path)) is extracted

    def test_table_row_captures_default_cell(self, tmp_path):
        text = (
            "| Env Var | Default |\n"
            "| --- | --- |\n"
            "| `BALDUR_RETRY_BASE_DELAY` | 1.0 |\n"
        )
        path = self._write(tmp_path, text)
        assert _extract_env_var_specs(path) == [("BALDUR_RETRY_BASE_DELAY", 3, "1.0")]

    def test_table_without_default_column_yields_none_default(self, tmp_path):
        text = (
            "| Env Var | Description |\n"
            "| --- | --- |\n"
            "| `BALDUR_RETRY_BASE_DELAY` | base delay |\n"
        )
        path = self._write(tmp_path, text)
        assert _extract_env_var_specs(path) == [("BALDUR_RETRY_BASE_DELAY", 3, None)]

    def test_two_shapes_in_one_file(self, tmp_path):
        text = (
            "```bash\n"
            "BALDUR_DLQ_MAX_SIZE=100000\n"
            "```\n"
            "\n"
            "| Env Var | Default |\n"
            "| --- | --- |\n"
            "| `BALDUR_RETRY_BASE_DELAY` | 1.0 |\n"
        )
        path = self._write(tmp_path, text)
        specs = _extract_env_var_specs(path)
        assert {(var, default) for var, _line, default in specs} == {
            ("BALDUR_DLQ_MAX_SIZE", None),
            ("BALDUR_RETRY_BASE_DELAY", "1.0"),
        }

    def test_inline_var_in_prose_after_table_is_not_a_row(self, tmp_path):
        # A blank line resets table state, so a later inline-code BALDUR_* in
        # prose is not mis-read as a table row.
        text = (
            "| Env Var | Default |\n"
            "| --- | --- |\n"
            "| `BALDUR_RETRY_BASE_DELAY` | 1.0 |\n"
            "\n"
            "See `BALDUR_DLQ_MAX_SIZE` for the cap.\n"
        )
        path = self._write(tmp_path, text)
        specs = _extract_env_var_specs(path)
        assert [var for var, _line, _default in specs] == ["BALDUR_RETRY_BASE_DELAY"]

    def test_missing_file_returns_empty(self, tmp_path):
        assert _extract_env_var_specs(tmp_path / "does_not_exist.md") == []


class TestTableParsing:
    """``_split_table_row`` / ``_is_table_separator`` — cell + separator boundaries."""

    def test_split_strips_outer_pipes(self):
        assert _split_table_row("| a | b | c |") == [" a ", " b ", " c "]

    def test_split_without_outer_pipes(self):
        assert _split_table_row("a | b") == ["a ", " b"]

    @pytest.mark.parametrize(
        "line",
        [
            "| --- | --- |",
            "| :--- | ---: |",
            "| :---: | --- |",
            "|----|----|",
        ],
    )
    def test_separator_rows_recognized(self, line):
        assert _is_table_separator(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "| Env Var | Default |",  # header
            "| `BALDUR_X` | 1.0 |",  # data row
            "no pipes at all",  # not a table line
            "| - |",  # single dash — below the {2,} floor
        ],
    )
    def test_non_separator_rows_rejected(self, line):
        assert _is_table_separator(line) is False


class TestPrefixIndex:
    """``_build_prefix_index`` — force-load makes nested prefixes complete."""

    def test_force_load_registers_nested_dlq_outbox_prefix(self):
        # Regression guard for the lazy-load gap (568 Implementation Notes): a
        # bare `import baldur.settings` leaves dlq_outbox unloaded, so
        # BALDUR_DLQ_OUTBOX_ would be absent and BALDUR_DLQ_OUTBOX_ENABLED would
        # false-phantom against BALDUR_DLQ_. Force-load fixes that.
        index = _build_prefix_index()
        assert "BALDUR_DLQ_OUTBOX_" in index
        assert "enabled" in index["BALDUR_DLQ_OUTBOX_"]

    def test_index_exposes_scalar_defaults_for_parity(self):
        # Defaults must flow through as real values so the parity check has data
        # to compare (not all None).
        index = _build_prefix_index()
        assert isinstance(index["BALDUR_RETRY_"]["base_delay"], float)

    def test_nested_prefixes_resolve_against_live_index(self):
        # The longest-prefix rule applied to the LIVE index: the deep var
        # resolves, the phantom watchdog toggle does not.
        index = _build_prefix_index()
        assert _resolve("BALDUR_DLQ_OUTBOX_ENABLED", index) is True
        assert _resolve("BALDUR_AUDIT_WATCHDOG_ENABLED", index) is False
