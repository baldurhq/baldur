"""Unit tests for the ``baldur init-ai`` CLI subcommand (552 D5/D6, 565).

``init_ai`` writes two instruction files teaching AI assistants the
``@baldur.protected("name")`` idiom: ``AGENTS.md`` (the guidance body, read by
Codex/Cursor/Copilot) and ``CLAUDE.md`` (a single ``@AGENTS.md`` import for
Claude Code). Its only collaborators are the filesystem (``pathlib.Path``) and
``typer``, so every behavior is observable without infra: file existence, the
canonical-idiom substring, single marker-block idempotency, the plan/apply
classification, the two-file atomic ``--force`` gate, and the ``SKIP`` no-op.

Verification techniques (UNIT_TEST_GUIDELINES.md §8):
- Contract: the guidance constants carry the canonical idiom / mental model,
  ``CLAUDE_GUIDANCE`` is the exact bare ``@AGENTS.md`` import, ``_PlanAction``
  enumerates the five outcomes, and the marker strings match the published
  ``grep`` contract.
- Behavior (state transition): no-file -> marker-file write; ``_plan`` maps a
  target to one of WRITE/REFRESH/SKIP/APPEND/REFUSE.
- Behavior (idempotency): re-run keeps exactly one marker block and plans
  ``SKIP`` (no filesystem mutation) for an already-current repo.
- Behavior (boundary): non-marker file refused without ``--force`` vs appended
  with ``--force``; the SKIP-vs-REFRESH discriminator; trailing-newline branch.
- Behavior (atomicity): a non-marker blocker refuses the whole command and the
  absent sibling stays absent (565 D4).
- Behavior (invariant): ``_refresh_marker_block`` / ``_apply`` preserve content
  outside the markers verbatim.
"""

from __future__ import annotations

from enum import Enum

import pytest
import typer
from typer.testing import CliRunner

import baldur.cli.commands.init_ai as init_ai_mod
from baldur.cli.app import app
from baldur.cli.commands.init_ai import (
    AGENTS_GUIDANCE,
    CLAUDE_GUIDANCE,
    MARKER_END,
    MARKER_START,
    _apply,
    _plan,
    _PlanAction,
    _refresh_marker_block,
    _render_block,
    init_ai,
)

runner = CliRunner()


def _agents_path(tmp_path):
    return tmp_path / "AGENTS.md"


def _read_agents(tmp_path) -> str:
    return _agents_path(tmp_path).read_text(encoding="utf-8")


def _claude_path(tmp_path):
    return tmp_path / "CLAUDE.md"


def _read_claude(tmp_path) -> str:
    return _claude_path(tmp_path).read_text(encoding="utf-8")


# =============================================================================
# Contract — the guidance template + marker constants
# =============================================================================


class TestAgentsTemplateContract:
    """AGENTS_GUIDANCE teaches the canonical idiom; markers match the grep contract."""

    def test_template_teaches_the_protected_decorator_idiom(self):
        """The single canonical idiom @baldur.protected is present."""
        assert "@baldur.protected" in AGENTS_GUIDANCE

    def test_template_documents_inline_protect_helper(self):
        """The non-decorator inline form baldur.protect(...) is documented."""
        assert "baldur.protect(" in AGENTS_GUIDANCE

    def test_template_documents_startup_and_env_config(self):
        """The mental model covers baldur.init() startup and BALDUR_* config."""
        assert "baldur.init()" in AGENTS_GUIDANCE
        assert "BALDUR_" in AGENTS_GUIDANCE

    def test_marker_constants_match_published_grep_contract(self):
        """Marker literals match the success-criteria grep (`baldur:start`/`baldur:end`)."""
        assert MARKER_START == "<!-- baldur:start -->"
        assert MARKER_END == "<!-- baldur:end -->"

    def test_guidance_constant_carries_no_marker_so_blocks_dont_nest(self):
        """The constant is marker-free; markers are added only by _render_block()."""
        assert MARKER_START not in AGENTS_GUIDANCE
        assert MARKER_END not in AGENTS_GUIDANCE


# =============================================================================
# Behavior — first write (no existing file -> marker file)
# =============================================================================


class TestInitAiWrite:
    """init_ai writes a fresh, marker-wrapped AGENTS.md when none exists."""

    def test_init_ai_no_existing_file_writes_rendered_block(self, tmp_path):
        # Given an empty target directory
        assert not _agents_path(tmp_path).exists()

        # When init-ai runs against it
        init_ai(dir=str(tmp_path), force=False)

        # Then the file is exactly the rendered managed block
        content = _read_agents(tmp_path)
        assert content == _render_block(AGENTS_GUIDANCE)

    def test_init_ai_written_file_contains_idiom_between_markers(self, tmp_path):
        # When the file is written from scratch
        init_ai(dir=str(tmp_path), force=False)

        # Then the canonical idiom sits inside a single marker block
        content = _read_agents(tmp_path)
        assert "@baldur.protected" in content
        assert content.count(MARKER_START) == 1
        assert content.count(MARKER_END) == 1
        start = content.index(MARKER_START)
        end = content.index(MARKER_END)
        assert AGENTS_GUIDANCE in content[start:end]


# =============================================================================
# Behavior — idempotency (re-run refreshes the same single block)
# =============================================================================


class TestInitAiIdempotency:
    """Re-running init_ai keeps exactly one marker block and stable content."""

    def test_init_ai_rerun_keeps_single_marker_block(self, tmp_path):
        # Given an AGENTS.md already written by init-ai
        init_ai(dir=str(tmp_path), force=False)
        after_first = _read_agents(tmp_path)

        # When init-ai runs again (and a third time)
        init_ai(dir=str(tmp_path), force=False)
        init_ai(dir=str(tmp_path), force=False)
        after_third = _read_agents(tmp_path)

        # Then exactly one marker pair survives and the content is unchanged
        assert after_third.count(MARKER_START) == 1
        assert after_third.count(MARKER_END) == 1
        assert after_third == after_first

    def test_init_ai_refresh_does_not_accumulate_trailing_newlines(self, tmp_path):
        # Given a freshly written file
        init_ai(dir=str(tmp_path), force=False)
        baseline_len = len(_read_agents(tmp_path))

        # When refreshed repeatedly
        for _ in range(3):
            init_ai(dir=str(tmp_path), force=False)

        # Then length is stable (no newline build-up after the end marker)
        assert len(_read_agents(tmp_path)) == baseline_len


# =============================================================================
# Behavior — non-marker file: refuse without --force, append with --force
# =============================================================================


class TestInitAiForce:
    """A pre-existing non-marker AGENTS.md is protected behind --force."""

    def test_non_marker_file_refused_without_force(self, tmp_path):
        # Given a user-authored AGENTS.md with no Baldur marker block
        original = "# Project agents\n\nUser-authored guidance.\n"
        _agents_path(tmp_path).write_text(original, encoding="utf-8")

        # When init-ai runs without --force
        with pytest.raises(typer.Exit) as exc_info:
            init_ai(dir=str(tmp_path), force=False)

        # Then it exits 1 and leaves the file untouched
        assert exc_info.value.exit_code == 1
        assert _read_agents(tmp_path) == original

    def test_non_marker_file_appended_with_force_preserves_existing(self, tmp_path):
        # Given a user-authored AGENTS.md ending in a newline
        original = "# Project agents\n\nUser-authored guidance.\n"
        _agents_path(tmp_path).write_text(original, encoding="utf-8")

        # When init-ai runs with --force
        init_ai(dir=str(tmp_path), force=True)

        # Then the original content is preserved and the managed block is appended
        content = _read_agents(tmp_path)
        assert content.startswith(original)
        assert content.count(MARKER_START) == 1
        assert "@baldur.protected" in content

    def test_non_marker_file_without_trailing_newline_appended_with_force(
        self, tmp_path
    ):
        # Given a non-marker file that does NOT end in a newline (separator branch)
        original = "# Project agents\n\nNo trailing newline"
        _agents_path(tmp_path).write_text(original, encoding="utf-8")

        # When init-ai appends under --force
        init_ai(dir=str(tmp_path), force=True)

        # Then the original survives verbatim and exactly one block is appended
        content = _read_agents(tmp_path)
        assert content.startswith(original)
        assert content.count(MARKER_START) == 1
        assert _render_block(AGENTS_GUIDANCE) in content


# =============================================================================
# Behavior — _refresh_marker_block helper (region swap invariant)
# =============================================================================


class TestRefreshMarkerBlock:
    """_refresh_marker_block swaps only the marker region, preserving surroundings."""

    def _existing_with_old_block(self) -> tuple[str, str, str]:
        preamble = "# Project agents\n\nIntro the assistant should keep.\n\n"
        old_block = f"{MARKER_START}\nSTALE BALDUR GUIDANCE\n{MARKER_END}\n"
        trailer = "\nUser notes after the block.\n"
        return preamble, old_block, trailer

    def test_refresh_preserves_content_outside_markers(self):
        # Given content with a managed block between user-authored text
        preamble, old_block, trailer = self._existing_with_old_block()
        existing = preamble + old_block + trailer
        new_block = _render_block(AGENTS_GUIDANCE)

        # When the marker region is refreshed
        result = _refresh_marker_block(existing, new_block)

        # Then the surrounding text is preserved and the old block is gone
        assert result.startswith(preamble)
        assert result.endswith(trailer)
        assert "STALE BALDUR GUIDANCE" not in result
        assert AGENTS_GUIDANCE in result
        assert result.count(MARKER_START) == 1

    def test_refresh_is_idempotent_on_double_apply(self):
        # Given content with a managed block
        preamble, old_block, trailer = self._existing_with_old_block()
        existing = preamble + old_block + trailer
        new_block = _render_block(AGENTS_GUIDANCE)

        # When refreshed once vs twice
        once = _refresh_marker_block(existing, new_block)
        twice = _refresh_marker_block(once, new_block)

        # Then a second refresh is a no-op
        assert twice == once


# =============================================================================
# Behavior — CLI wiring (command registered + driven through the Typer app)
# =============================================================================


class TestInitAiCliWiring:
    """`init-ai` is registered on the app and works through the Typer runner."""

    def test_help_lists_init_ai_subcommand(self):
        # When the top-level help is rendered
        result = runner.invoke(app, ["--help"])

        # Then init-ai is listed as a subcommand
        assert result.exit_code == 0
        assert "init-ai" in result.output

    def test_cli_invocation_writes_agents_md_in_cwd(self, tmp_path, monkeypatch):
        # Given an empty cwd with no AGENTS.md
        monkeypatch.chdir(tmp_path)

        # When `baldur init-ai` is driven through the app (default --dir = cwd)
        result = runner.invoke(app, ["init-ai"])

        # Then it succeeds and writes the idiom into the cwd
        assert result.exit_code == 0, result.output
        content = _read_agents(tmp_path)
        assert "@baldur.protected" in content
        assert content.count(MARKER_START) == 1

    def test_cli_force_flag_appends_to_non_marker_file(self, tmp_path, monkeypatch):
        # Given a non-marker AGENTS.md in cwd
        monkeypatch.chdir(tmp_path)
        original = "# Existing\nuser content\n"
        _agents_path(tmp_path).write_text(original, encoding="utf-8")

        # When invoked without --force, the command refuses (exit 1, file intact)
        refused = runner.invoke(app, ["init-ai"])
        assert refused.exit_code == 1
        assert _read_agents(tmp_path) == original

        # When invoked with --force, it appends while preserving the original
        forced = runner.invoke(app, ["init-ai", "--force"])
        assert forced.exit_code == 0, forced.output
        content = _read_agents(tmp_path)
        assert content.startswith(original)
        assert content.count(MARKER_START) == 1


# =============================================================================
# Contract — CLAUDE_GUIDANCE (565 D2): a single bare @AGENTS.md import
# =============================================================================


class TestClaudeGuidanceContract:
    """CLAUDE_GUIDANCE is the exact @AGENTS.md import body — marker-free, exported."""

    def test_claude_guidance_is_exactly_the_bare_agents_import(self):
        """D2 pins the body to a single bare, newline-terminated @AGENTS.md import."""
        assert CLAUDE_GUIDANCE == "@AGENTS.md\n"

    def test_claude_guidance_uses_the_bare_relative_form_without_dot_slash(self):
        """D2: the bare form (no ./) — Claude Code resolves it against CLAUDE.md."""
        assert "@AGENTS.md" in CLAUDE_GUIDANCE
        assert "./" not in CLAUDE_GUIDANCE

    def test_claude_guidance_is_exported_for_symmetry_with_agents_guidance(self):
        """D2 adds CLAUDE_GUIDANCE to __all__ for symmetry with AGENTS_GUIDANCE."""
        assert "CLAUDE_GUIDANCE" in init_ai_mod.__all__
        assert "AGENTS_GUIDANCE" in init_ai_mod.__all__

    def test_claude_guidance_is_marker_free_so_blocks_dont_nest(self):
        """The body carries no markers; _render_block() adds them."""
        assert MARKER_START not in CLAUDE_GUIDANCE
        assert MARKER_END not in CLAUDE_GUIDANCE


# =============================================================================
# Contract — _PlanAction (565 D1): the plan -> apply control-flow token
# =============================================================================


class TestPlanActionContract:
    """_PlanAction enumerates the five plan outcomes and stays a private token."""

    def test_plan_action_has_the_five_documented_members(self):
        """D1 names exactly WRITE / REFRESH / SKIP / APPEND / REFUSE."""
        assert {member.name for member in _PlanAction} == {
            "WRITE",
            "REFRESH",
            "SKIP",
            "APPEND",
            "REFUSE",
        }

    def test_plan_action_is_a_str_enum_per_project_convention(self):
        """CLAUDE.md Enum rule: (str, Enum) inheritance."""
        assert issubclass(_PlanAction, str)
        assert issubclass(_PlanAction, Enum)

    def test_plan_action_is_not_exported(self):
        """D1: an internal control-flow token, deliberately absent from __all__."""
        assert "_PlanAction" not in init_ai_mod.__all__


# =============================================================================
# Behavior — _render_block over both guidance bodies (565 D2)
# =============================================================================


class TestRenderBlockBehavior:
    """_render_block wraps either guidance body in one marker-delimited block."""

    @pytest.mark.parametrize("body", [AGENTS_GUIDANCE, CLAUDE_GUIDANCE])
    def test_render_block_wraps_body_in_a_single_marker_pair(self, body):
        # When the body is rendered
        rendered = _render_block(body)

        # Then it is exactly start-marker / body / end-marker, newline-terminated
        assert rendered == f"{MARKER_START}\n{body}{MARKER_END}\n"
        assert rendered.count(MARKER_START) == 1
        assert rendered.count(MARKER_END) == 1
        assert body in rendered


# =============================================================================
# Behavior — _plan classifies a target into one action, without mutation (565 D1)
# =============================================================================

_GUIDANCE_BY_FILENAME = [
    ("AGENTS.md", AGENTS_GUIDANCE),
    ("CLAUDE.md", CLAUDE_GUIDANCE),
]


class TestPlanClassification:
    """_plan maps a target to WRITE/REFRESH/SKIP/APPEND/REFUSE without touching disk."""

    @pytest.mark.parametrize(("filename", "body"), _GUIDANCE_BY_FILENAME)
    def test_plan_absent_file_is_write(self, tmp_path, filename, body):
        target = tmp_path / filename
        assert _plan(target, _render_block(body), force=False) is _PlanAction.WRITE

    @pytest.mark.parametrize(("filename", "body"), _GUIDANCE_BY_FILENAME)
    def test_plan_up_to_date_marker_file_is_skip(self, tmp_path, filename, body):
        # Given a file already byte-identical to the rendered block
        target = tmp_path / filename
        block = _render_block(body)
        target.write_text(block, encoding="utf-8")

        # Then a re-plan is a true no-op (the SKIP side of the discriminator)
        assert _plan(target, block, force=False) is _PlanAction.SKIP

    @pytest.mark.parametrize(("filename", "body"), _GUIDANCE_BY_FILENAME)
    def test_plan_stale_marker_file_is_refresh(self, tmp_path, filename, body):
        # Given a marker file whose block differs from the rendered one
        target = tmp_path / filename
        target.write_text(_render_block("STALE BODY\n"), encoding="utf-8")

        # Then it is classified for refresh (the REFRESH side of the discriminator)
        assert _plan(target, _render_block(body), force=False) is _PlanAction.REFRESH

    @pytest.mark.parametrize(("filename", "body"), _GUIDANCE_BY_FILENAME)
    def test_plan_non_marker_file_without_force_is_refuse(
        self, tmp_path, filename, body
    ):
        target = tmp_path / filename
        target.write_text("# hand authored\n", encoding="utf-8")
        assert _plan(target, _render_block(body), force=False) is _PlanAction.REFUSE

    @pytest.mark.parametrize(("filename", "body"), _GUIDANCE_BY_FILENAME)
    def test_plan_non_marker_file_with_force_is_append(self, tmp_path, filename, body):
        target = tmp_path / filename
        target.write_text("# hand authored\n", encoding="utf-8")
        assert _plan(target, _render_block(body), force=True) is _PlanAction.APPEND

    @pytest.mark.parametrize(("filename", "body"), _GUIDANCE_BY_FILENAME)
    def test_plan_does_not_mutate_the_filesystem(self, tmp_path, filename, body):
        # Given an existing marker file (the branch that reads + computes a refresh)
        target = tmp_path / filename
        original = _render_block("STALE BODY\n")
        target.write_text(original, encoding="utf-8")

        # When it is planned
        _plan(target, _render_block(body), force=False)

        # Then the file on disk is untouched — _plan is pure classification
        assert target.read_text(encoding="utf-8") == original


# =============================================================================
# Behavior — _apply performs the planned write (a no-op for SKIP) (565 D1)
# =============================================================================


class TestApplyBehavior:
    """_apply executes one action and returns its user-facing status line."""

    def test_apply_write_creates_the_file_with_the_block(self, tmp_path):
        target = tmp_path / "AGENTS.md"
        block = _render_block(AGENTS_GUIDANCE)

        message = _apply(target, block, _PlanAction.WRITE)

        assert target.read_text(encoding="utf-8") == block
        assert "Wrote" in message

    def test_apply_skip_is_a_noop_and_reports_up_to_date(self, tmp_path):
        # Given a file already holding the rendered block
        target = tmp_path / "CLAUDE.md"
        block = _render_block(CLAUDE_GUIDANCE)
        target.write_text(block, encoding="utf-8")
        before = target.read_text(encoding="utf-8")
        mtime_before = target.stat().st_mtime_ns

        # When SKIP is applied
        message = _apply(target, block, _PlanAction.SKIP)

        # Then nothing is written (content AND mtime unchanged) and the message says so
        assert target.read_text(encoding="utf-8") == before
        assert target.stat().st_mtime_ns == mtime_before
        assert "up to date" in message

    def test_apply_refresh_swaps_only_the_marker_region(self, tmp_path):
        # Given a marker file with user content around a stale block
        preamble = "# Project\n\nkeep this intro\n\n"
        trailer = "\nand these trailing notes\n"
        target = tmp_path / "AGENTS.md"
        target.write_text(
            preamble + _render_block("STALE GUIDANCE\n") + trailer, encoding="utf-8"
        )
        block = _render_block(AGENTS_GUIDANCE)

        # When refreshed
        message = _apply(target, block, _PlanAction.REFRESH)

        # Then surroundings are preserved verbatim and the stale body is gone
        content = target.read_text(encoding="utf-8")
        assert content.startswith(preamble)
        assert content.endswith(trailer)
        assert "STALE GUIDANCE" not in content
        assert AGENTS_GUIDANCE in content
        assert content.count(MARKER_START) == 1
        assert "Refreshed" in message

    def test_apply_append_preserves_existing_content(self, tmp_path):
        # Given a non-marker file
        target = tmp_path / "CLAUDE.md"
        original = "# my notes\nhand authored\n"
        target.write_text(original, encoding="utf-8")
        block = _render_block(CLAUDE_GUIDANCE)

        # When the block is appended
        message = _apply(target, block, _PlanAction.APPEND)

        # Then the original survives verbatim and the block follows it
        content = target.read_text(encoding="utf-8")
        assert content.startswith(original)
        assert block in content
        assert "Appended" in message


# =============================================================================
# Behavior — init_ai writes CLAUDE.md alongside AGENTS.md by default (565 D1/D2)
# =============================================================================


class TestInitAiWritesClaudeMd:
    """init_ai writes CLAUDE.md (a @AGENTS.md import) next to AGENTS.md by default."""

    def test_init_ai_default_writes_both_files(self, tmp_path):
        # Given an empty directory
        assert not _agents_path(tmp_path).exists()
        assert not _claude_path(tmp_path).exists()

        # When init-ai runs with no flags
        init_ai(dir=str(tmp_path), force=False)

        # Then both instruction files exist
        assert _agents_path(tmp_path).exists()
        assert _claude_path(tmp_path).exists()

    def test_init_ai_claude_md_is_a_single_agents_import_block(self, tmp_path):
        # When init-ai writes CLAUDE.md from scratch
        init_ai(dir=str(tmp_path), force=False)

        # Then it is exactly the rendered @AGENTS.md import in one marker block
        content = _read_claude(tmp_path)
        assert content == _render_block(CLAUDE_GUIDANCE)
        assert "@AGENTS.md" in content
        assert content.count(MARKER_START) == 1
        assert content.count(MARKER_END) == 1

    def test_init_ai_rerun_plans_skip_for_both_targets(self, tmp_path):
        # Given both files already written by a first run
        init_ai(dir=str(tmp_path), force=False)

        # Then a second run plans SKIP (a true no-op) for both targets — the
        # idempotency success criterion, asserted at the _plan boundary
        assert (
            _plan(_agents_path(tmp_path), _render_block(AGENTS_GUIDANCE), force=False)
            is _PlanAction.SKIP
        )
        assert (
            _plan(_claude_path(tmp_path), _render_block(CLAUDE_GUIDANCE), force=False)
            is _PlanAction.SKIP
        )

    def test_init_ai_rerun_keeps_single_block_in_claude_md(self, tmp_path):
        # Given a first run
        init_ai(dir=str(tmp_path), force=False)
        after_first = _read_claude(tmp_path)

        # When re-run twice more
        init_ai(dir=str(tmp_path), force=False)
        init_ai(dir=str(tmp_path), force=False)

        # Then CLAUDE.md is byte-stable with exactly one marker block
        after_third = _read_claude(tmp_path)
        assert after_third == after_first
        assert after_third.count(MARKER_START) == 1

    def test_cli_invocation_writes_both_files_in_cwd(self, tmp_path, monkeypatch):
        # Given an empty cwd
        monkeypatch.chdir(tmp_path)

        # When `baldur init-ai` runs through the Typer app (default --dir = cwd)
        result = runner.invoke(app, ["init-ai"])

        # Then it succeeds and both files land in the cwd
        assert result.exit_code == 0, result.output
        assert _read_agents(tmp_path).count(MARKER_START) == 1
        assert "@AGENTS.md" in _read_claude(tmp_path)


# =============================================================================
# Behavior — the two-file --force gate is atomic (565 D4)
# =============================================================================


class TestInitAiAtomicRefuse:
    """A non-marker target blocks the whole command; nothing is written (565 D4)."""

    @pytest.mark.parametrize(
        ("blocker", "absent"),
        [("CLAUDE.md", "AGENTS.md"), ("AGENTS.md", "CLAUDE.md")],
    )
    def test_non_marker_blocker_refuses_atomically_and_leaves_sibling_absent(
        self, tmp_path, blocker, absent
    ):
        # Given one pre-existing non-marker file and an absent sibling
        original = "# hand-authored\nproject guidance\n"
        (tmp_path / blocker).write_text(original, encoding="utf-8")
        assert not (tmp_path / absent).exists()

        # When init-ai runs without --force
        with pytest.raises(typer.Exit) as exc_info:
            init_ai(dir=str(tmp_path), force=False)

        # Then it exits 1, the blocker is untouched, and NOTHING else is written:
        # the absent sibling must still be absent (atomic — no half-init repo)
        assert exc_info.value.exit_code == 1
        assert (tmp_path / blocker).read_text(encoding="utf-8") == original
        assert not (tmp_path / absent).exists()

    def test_force_rerun_after_block_writes_fresh_file_and_appends_to_existing(
        self, tmp_path
    ):
        # Given a hand-authored CLAUDE.md and no AGENTS.md (the common first run)
        original = "# my project\nhand-authored Claude guidance\n"
        _claude_path(tmp_path).write_text(original, encoding="utf-8")

        # When the user re-runs with --force
        init_ai(dir=str(tmp_path), force=True)

        # Then AGENTS.md is created fresh as the exact rendered guidance block
        assert _read_agents(tmp_path) == _render_block(AGENTS_GUIDANCE)

        # And CLAUDE.md keeps its original content with the @AGENTS.md block appended
        claude = _read_claude(tmp_path)
        assert claude.startswith(original)
        assert "@AGENTS.md" in claude
        assert claude.count(MARKER_START) == 1
