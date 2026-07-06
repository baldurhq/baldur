"""Impl 575 D1/D2/D2-exists — G31 assurance-claim guard.

A strong-guarantee docstring keyword (delivery term-of-art ``exactly-once`` /
``at-least-once`` / ``at-most-once``, or durability/consistency term
``zero[- ]data[- ]loss`` / ``linearizable`` / ``wait-free``, matched
case-insensitively) MUST carry an adjacent ``# verified-by: <test_ref>`` comment
whose ``<test_ref>`` resolves to a real ``def`` under ``tests/``. The link lives
in a ``#`` comment, never in the docstring (mkdocstrings publishes docstrings).
G31 checks presence + well-formedness + name-existence (the typo guard); whether
the resolved test actually *proves* the claim is the semantic half, owned by
OOS #506(c).

Rule registry:
``ARCHITECTURE.md#g31-assurance-claim``
"""

from __future__ import annotations

import ast

import pytest

from tests.architecture._helpers import (
    ASSURANCE_KEYWORDS,
    assurance_symbol_span,
    collect_test_def_names,
    collect_violations,
    find_assurance_claims,
    has_verified_by_link,
    iter_docstring_nodes,
    resolve_test_ref_name,
    verified_by_ref,
)
from tests.architecture.conftest import (
    DEFAULT_SRC_ROOTS,
    parse_ast,
    symbol_of,
    walk_src,
)

_RULE_KEY = "assurance_claim"
_RULE_ANCHOR = "#g31-assurance-claim"


def _span_ref(raw_lines: list[str], node: ast.AST) -> str | None:
    """Return the ``<test_ref>`` of the first ``# verified-by:`` in ``node``'s span."""
    start, end = assurance_symbol_span(node)
    for lineno in range(start, end + 1):
        if 1 <= lineno <= len(raw_lines):
            ref = verified_by_ref(raw_lines[lineno - 1])
            if ref:
                return ref
    return None


class TestAssuranceClaimGuard:
    """575 D1 — every strong-guarantee docstring keyword links a resolvable test."""

    def test_strong_guarantee_docstrings_link_a_real_test(self):
        """Each claim carries `# verified-by: <ref>` and `<ref>` resolves under tests/."""
        test_defs = collect_test_def_names()
        raw: list[tuple] = []
        for path in walk_src(DEFAULT_SRC_ROOTS):
            tree = parse_ast(path)
            if tree is None:
                continue
            try:
                raw_lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for node, doc in iter_docstring_nodes(tree):
                claims = find_assurance_claims(doc)
                if not claims:
                    continue
                symbol = symbol_of(tree, node)
                line = getattr(node, "lineno", 1)
                if not has_verified_by_link(raw_lines, node):
                    raw.append(
                        (
                            path,
                            line,
                            symbol,
                            f"strong-guarantee claim {claims} lacks an adjacent "
                            f"`# verified-by: <test_ref>` comment (link a proving "
                            f"test, soften the claim, or baseline)",
                        )
                    )
                    continue
                ref = _span_ref(raw_lines, node)
                if ref and resolve_test_ref_name(ref) not in test_defs:
                    raw.append(
                        (
                            path,
                            line,
                            symbol,
                            f"`# verified-by: {ref}` resolves to no `def "
                            f"{resolve_test_ref_name(ref)}` under tests/ (typo?)",
                        )
                    )
        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"{len(violations)} assurance-claim violation(s) — a strong-guarantee "
            f"docstring keyword without a resolvable `# verified-by:` link "
            f"(claim-wiring bug class):\n" + "\n".join(violations)
        )


def _func_node(source: str, name: str = "f") -> tuple[ast.AST, list[str]]:
    tree = ast.parse(source)
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    return node, source.splitlines()


class TestAssuranceClaimGuardAntiSilentPass:
    """G24-precedent inline fixtures: prove the matcher + link checks behave."""

    def test_hyphenated_claim_matched(self):
        for keyword in (
            "exactly-once",
            "at-least-once",
            "at-most-once",
            "zero-data-loss",
            "zero data loss",
            "linearizable",
            "wait-free",
        ):
            assert find_assurance_claims(f"Provides {keyword} delivery.") == [keyword]

    def test_case_variant_matched(self):
        """`re.IGNORECASE` catches sentence-initial / heading-cased variants (D1)."""
        assert find_assurance_claims("Exactly-Once dedup.") == ["exactly-once"]
        assert find_assurance_claims("ZERO-DATA-LOSS guarantee.") == ["zero-data-loss"]

    def test_spaced_usage_instruction_not_matched(self):
        """The spaced `exactly once` is a usage instruction, not a guarantee (D1)."""
        assert find_assurance_claims("Invoke init exactly once at startup.") == []

    def test_missing_link_flagged(self):
        node, lines = _func_node('def f():\n    """Dedup is exactly-once here."""\n')
        assert find_assurance_claims("Dedup is exactly-once here.")
        assert not has_verified_by_link(lines, node)

    def test_present_link_clean(self):
        source = (
            "def f():  # verified-by: test_dedup_exactly_once\n"
            '    """Dedup is exactly-once here."""\n'
        )
        node, lines = _func_node(source)
        assert has_verified_by_link(lines, node)
        assert _span_ref(lines, node) == "test_dedup_exactly_once"

    def test_link_after_docstring_in_span(self):
        """A `# verified-by:` on the line just after the docstring is still adjacent (D2)."""
        source = (
            "def f():\n"
            '    """Provides zero-data-loss."""\n'
            "    # verified-by: test_zero_loss\n"
        )
        node, lines = _func_node(source)
        assert has_verified_by_link(lines, node)

    def test_sibling_comment_out_of_span(self):
        """A `# verified-by:` belonging to a sibling symbol is NOT adjacent (D2)."""
        source = (
            "def f():\n"
            '    """Provides zero-data-loss."""\n'
            "    return 1\n"
            "\n"
            "\n"
            "# verified-by: test_other\n"
            "def g():\n"
            "    pass\n"
        )
        node, lines = _func_node(source)
        assert not has_verified_by_link(lines, node)

    def test_nonexistent_ref_does_not_resolve(self):
        """A typo'd `<test_ref>` resolves to no `def` under tests/ (D2-exists typo guard)."""
        test_defs = collect_test_def_names()
        assert resolve_test_ref_name("test_nonexistent_name_xyz") not in test_defs
        # This very test function is a real def under tests/ — it must resolve.
        assert "test_nonexistent_ref_does_not_resolve" in test_defs

    def test_resolve_test_ref_name_strips_nodeid_and_param(self):
        assert resolve_test_ref_name("path/to/test_x.py::test_dedup") == "test_dedup"
        assert resolve_test_ref_name("test_dedup[case-1]") == "test_dedup"
        assert resolve_test_ref_name("test_dedup") == "test_dedup"

    def test_module_docstring_span_starts_at_line_one(self):
        """A module-level claim's adjacency window starts at line 1 (D2)."""
        source = '"""Delivery guarantee: at-most-once."""\n# verified-by: test_atmost\n'
        tree = ast.parse(source)
        assert find_assurance_claims(ast.get_docstring(tree))
        assert has_verified_by_link(source.splitlines(), tree)


# D1's mechanized keyword set, hardcoded here as the Contract anchor: this list
# is the published spec (575 D1), so a drift in `ASSURANCE_KEYWORDS` must break a
# test, not silently change the gate's reach.
_D1_KEYWORDS = (
    "exactly-once",
    "at-least-once",
    "at-most-once",
    "zero data loss",
    "zero-data-loss",
    "linearizable",
    "wait-free",
)


class TestAssuranceMatcherContract:
    """575 D1 — the mechanized keyword set and its boundary behavior are pinned."""

    def test_assurance_keyword_set_is_exactly_the_d1_terms(self):
        # Then: the shipped matcher set equals D1's seven strong-guarantee terms
        assert set(ASSURANCE_KEYWORDS) == set(_D1_KEYWORDS)

    @pytest.mark.parametrize("keyword", _D1_KEYWORDS)
    def test_each_d1_keyword_matches_in_a_guarantee_context(self, keyword):
        assert find_assurance_claims(f"Provides {keyword} delivery.") == [keyword]

    @pytest.mark.parametrize("keyword", _D1_KEYWORDS)
    def test_each_d1_keyword_matched_case_insensitively(self, keyword):
        """`re.IGNORECASE` catches heading-/sentence-cased variants (D1)."""
        assert find_assurance_claims(f"{keyword.upper()} here.") == [keyword]

    @pytest.mark.parametrize(
        "text",
        [
            "Invoke init exactly once at startup.",  # spaced usage-instruction
            "Process at least once per cycle.",  # spaced (only hyphenated is the term)
            "Deliver at most once per request.",  # spaced
            "A linearization point exists.",  # shared-prefix word, not the term
            "Stores data with no loss.",  # paraphrase, not the keyword
        ],
    )
    def test_non_term_phrasings_not_matched(self, text):
        assert find_assurance_claims(text) == []

    def test_hyphen_boundary_excludes_compound_suffix(self):
        """A trailing hyphen breaks the boundary: `exactly-once-delivery` is NOT matched.

        Documents the implemented `(?![\\w-])` boundary (575 D1). This is a known
        recall edge — a hyphen-compounded form escapes the gate — surfaced in the
        /test report, not changed here.
        """
        assert find_assurance_claims("Provides exactly-once-delivery.") == []


class TestVerifiedByLinkBehavior:
    """575 D2 / D2-exists — link extraction, malformed-token rejection, adjacency."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("def f():  # verified-by: test_x", "test_x"),
            ("    # verified-by: tests/x.py::test_y", "tests/x.py::test_y"),
            ("# verified-by:test_z", "test_z"),  # no space after colon
            ("code  # verified-by:", None),  # malformed: no token
            ("code  # verified-by:    ", None),  # malformed: whitespace only
            ("def f():  # ordinary comment", None),  # not a verified-by comment
        ],
    )
    def test_verified_by_ref_extracts_or_rejects(self, line, expected):
        assert verified_by_ref(line) == expected

    def test_decorator_line_is_in_adjacency_span(self):
        """A decorated symbol's span starts at the first decorator line (D2)."""
        # Given: the `# verified-by:` link rides on the decorator line
        source = (
            "@deco  # verified-by: test_decorated\n"
            "def f():\n"
            '    """Provides linearizable reads."""\n'
        )
        node, lines = _func_node(source)
        # Then: the decorator line is inside the adjacency window
        assert find_assurance_claims(ast.get_docstring(node))
        assert has_verified_by_link(lines, node)
        assert _span_ref(lines, node) == "test_decorated"
