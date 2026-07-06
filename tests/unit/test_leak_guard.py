"""Unit tests for the public-repo leak guard (``scripts/check_leak_guard.py``).

The leak-guard scan predicate is a pure ``(text) -> hits`` function over a
private-only token set, so it is exercised here on planted strings. Positive
samples are assembled with ``+`` concatenation (which the formatter does not
fold) so this test file does not itself carry a contiguous private token — the
guard self-excludes this path, but keeping the source clean is belt-and-braces.
"""

from __future__ import annotations

import pytest

from scripts.check_leak_guard import (
    _PRIVATE_REPO_NAME,
    find_content_leaks,
    find_message_leaks,
    is_private_monorepo,
)

_DOCS = "docs/"

# Each MUST be flagged by the content scan (zero legitimate use in public OSS).
_CONTENT_POSITIVES = [
    _PRIVATE_REPO_NAME,
    "see " + _DOCS + "laws/SOME_LAW.md for the rule",
    "per " + _DOCS + "impl/512_thing.md",
    _DOCS + "maintainer/runbook.md",
    _DOCS + "self_healing/01_overview.md",
    "ADR-" + "008 governs this",
    "decided in ADR-" + "6",
]

# Each MUST NOT be flagged by the content scan (public-repo norms).
_CONTENT_NEGATIVES = [
    "pip install baldur-pro",  # bare product name
    "baldur_pro.services.bulkhead",  # dotted import path, not a src/ path
    "see #123 for context",  # public issue ref
    "enforced by G19",  # public gate number
    "landed in Wave 6A",  # not scanned
    "src/baldur" + "_pro/startup.py",  # private SOURCE path: content-allowed
    "import baldur",  # bare package
    "HTTP 429 throttling",  # number, no anchor
    "ADDRESS-1 line",  # ADR look-alike, no digit boundary match
]

# The commit-message scan adds the private source-tree paths to the content set.
_MESSAGE_ONLY_POSITIVES = [
    "src/baldur" + "_pro/startup.py",
    "src/baldur" + "_dormant/adapters/kafka.py",
]


class TestContentScan:
    @pytest.mark.parametrize("text", _CONTENT_POSITIVES)
    def test_positive_flagged(self, text: str) -> None:
        assert find_content_leaks(text), f"expected a content leak in {text!r}"

    @pytest.mark.parametrize("text", _CONTENT_NEGATIVES)
    def test_negative_clean(self, text: str) -> None:
        assert not find_content_leaks(text), f"false positive on {text!r}"


class TestMessageScan:
    @pytest.mark.parametrize("text", _MESSAGE_ONLY_POSITIVES)
    def test_source_path_flagged_in_message(self, text: str) -> None:
        assert find_message_leaks(text), f"expected a message leak in {text!r}"

    @pytest.mark.parametrize("text", _MESSAGE_ONLY_POSITIVES)
    def test_source_path_allowed_in_content(self, text: str) -> None:
        # Private source paths are legitimate in boundary-test CONTENT; the scan
        # treats them as a leak only in commit messages.
        assert not find_content_leaks(text), f"should be content-allowed: {text!r}"

    @pytest.mark.parametrize("text", _CONTENT_POSITIVES)
    def test_content_positive_also_flagged_in_message(self, text: str) -> None:
        assert find_message_leaks(text), f"message scan must be a superset: {text!r}"


class TestSelfDisable:
    def test_inert_in_monorepo(self, tmp_path) -> None:
        (tmp_path / "src" / "baldur_pro").mkdir(parents=True)
        assert is_private_monorepo(tmp_path) is True

    def test_active_in_public_repo(self, tmp_path) -> None:
        (tmp_path / "src" / "baldur").mkdir(parents=True)
        assert is_private_monorepo(tmp_path) is False
