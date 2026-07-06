"""Tests for pytest marker hooks (436 — Dormant Test Isolation)."""

import importlib.util
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.factories import repo_root

_FakeMarker = namedtuple("FakeMarker", ["name", "kwargs"], defaults=(None,))

_PROJECT_ROOT = repo_root()


class _FakeItem:
    """Minimal pytest.Item stand-in for hook testing."""

    def __init__(self, path, marker_names=None):
        self.path = Path(path)
        self._markers = [_FakeMarker(n) for n in (marker_names or [])]
        self.added_markers = []

    def iter_markers(self):
        return iter(self._markers)

    def add_marker(self, marker):
        self.added_markers.append(marker)


def _load_conftest(conftest_path):
    """Import a conftest module by file path without requiring __init__.py."""
    spec = importlib.util.spec_from_file_location(
        f"_conftest_{conftest_path.parent.name}",
        conftest_path,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDormantAutoMarkBehavior:
    """Verify dormant conftest hooks mark only items within their directory."""

    @pytest.fixture
    def gd_conftest(self):
        path = _PROJECT_ROOT / "tests/unit/audit/graceful_degradation/conftest.py"
        return _load_conftest(path)

    @pytest.fixture
    def perf_conftest(self):
        path = _PROJECT_ROOT / "tests/unit/audit/performance/conftest.py"
        return _load_conftest(path)

    def test_item_inside_graceful_degradation_gets_dormant(self, gd_conftest):
        """Items under graceful_degradation/ receive dormant marker."""
        item = _FakeItem(gd_conftest._THIS_DIR / "test_cb.py")

        gd_conftest.pytest_collection_modifyitems(MagicMock(), [item])

        assert len(item.added_markers) == 1
        assert item.added_markers[0].name == "dormant"

    def test_item_outside_graceful_degradation_not_marked(self, gd_conftest):
        """Items outside graceful_degradation/ are not touched."""
        item = _FakeItem(Path(__file__).parent / "test_unrelated.py")

        gd_conftest.pytest_collection_modifyitems(MagicMock(), [item])

        assert len(item.added_markers) == 0

    def test_item_inside_performance_gets_dormant(self, perf_conftest):
        """Items under performance/ receive dormant marker."""
        item = _FakeItem(perf_conftest._THIS_DIR / "test_resilient.py")

        perf_conftest.pytest_collection_modifyitems(MagicMock(), [item])

        assert len(item.added_markers) == 1
        assert item.added_markers[0].name == "dormant"

    def test_mixed_items_only_target_directory_marked(self, gd_conftest):
        """In a mixed list, only items in the target directory get marked."""
        inside = _FakeItem(gd_conftest._THIS_DIR / "test_a.py")
        outside = _FakeItem(Path(__file__).parent / "test_b.py")

        gd_conftest.pytest_collection_modifyitems(MagicMock(), [inside, outside])

        assert len(inside.added_markers) == 1
        assert len(outside.added_markers) == 0

    def test_sibling_directory_not_marked(self, gd_conftest):
        """Items in a sibling directory (same parent) are not marked."""
        sibling = gd_conftest._THIS_DIR.parent / "buffer" / "test_x.py"
        item = _FakeItem(sibling)

        gd_conftest.pytest_collection_modifyitems(MagicMock(), [item])

        assert len(item.added_markers) == 0


class TestBrokenRefValidatorBehavior:
    """Verify broken ref-validator rejects markers without ref: comment."""

    @staticmethod
    def _get_root_hook():
        from tests.conftest import pytest_collection_modifyitems

        return pytest_collection_modifyitems

    def test_broken_item_with_ref_passes_validation(self, tmp_path):
        """Broken-marked item with ref: comment passes validation."""
        hook = self._get_root_hook()
        test_file = tmp_path / "test_ok.py"
        test_file.write_text(
            "import pytest\npytestmark = pytest.mark.broken  # ref: 436\n"
        )
        item = _FakeItem(test_file, marker_names=["broken"])

        hook(MagicMock(), [item])

    def test_broken_item_without_ref_raises_usage_error(self, tmp_path):
        """Broken-marked item without ref: comment raises UsageError."""
        hook = self._get_root_hook()
        test_file = tmp_path / "test_bad.py"
        test_file.write_text("import pytest\npytestmark = pytest.mark.broken\n")
        item = _FakeItem(test_file, marker_names=["broken"])

        with pytest.raises(pytest.UsageError, match="ref:"):
            hook(MagicMock(), [item])

    def test_non_broken_item_skips_ref_validation(self, tmp_path):
        """Non-broken items bypass ref validation even if file lacks ref:."""
        hook = self._get_root_hook()
        test_file = tmp_path / "test_normal.py"
        test_file.write_text("# no ref: anywhere\n")
        item = _FakeItem(test_file, marker_names=[])

        hook(MagicMock(), [item])

    def test_ref_anywhere_in_file_satisfies_validator(self, tmp_path):
        """ref: can appear anywhere in the file, not just next to pytestmark."""
        hook = self._get_root_hook()
        test_file = tmp_path / "test_ref_elsewhere.py"
        test_file.write_text(
            "import pytest\n"
            "pytestmark = pytest.mark.broken\n"
            "# Tracking ref: 436 - BackoffCalculator removed\n",
            encoding="utf-8",
        )
        item = _FakeItem(test_file, marker_names=["broken"])

        hook(MagicMock(), [item])

    def test_multiple_broken_items_same_path_validated_once(self, tmp_path):
        """Duplicate paths are deduplicated — file read only once."""
        hook = self._get_root_hook()
        test_file = tmp_path / "test_dup.py"
        test_file.write_text(
            "import pytest\npytestmark = pytest.mark.broken  # ref: 436\n"
        )
        item1 = _FakeItem(test_file, marker_names=["broken"])
        item2 = _FakeItem(test_file, marker_names=["broken"])

        original_read = Path.read_text
        read_calls = []

        def tracking_read(self, *args, **kwargs):
            read_calls.append(self)
            return original_read(self, *args, **kwargs)

        from unittest.mock import patch

        with patch.object(Path, "read_text", tracking_read):
            hook(MagicMock(), [item1, item2])

        broken_reads = [p for p in read_calls if p == test_file]
        assert len(broken_reads) == 1


class TestSessionStartContract:
    """453 D7: ``pytest_sessionstart`` enforces ``BALDUR_TEST_MODE=true``.

    Turning silent drift (env override at the parent process / CI level)
    into an immediate hard error is the contract — without it, a single
    flipped variable would silently switch the framework into production
    mode and let bootstrap-driven cluster_identity validation flip
    quarantine globally for every test.
    """

    @staticmethod
    def _get_session_start_hook():
        from tests.conftest import pytest_sessionstart

        return pytest_sessionstart

    def test_session_start_passes_when_test_mode_true(self, monkeypatch):
        """``BALDUR_TEST_MODE=true`` satisfies the contract — no exception."""
        hook = self._get_session_start_hook()
        monkeypatch.setenv("BALDUR_TEST_MODE", "true")

        # Must not raise.
        hook(MagicMock())

    def test_session_start_passes_for_case_insensitive_true(self, monkeypatch):
        """Mixed case ``True`` / ``TRUE`` resolves to true via ``.lower()``."""
        hook = self._get_session_start_hook()
        monkeypatch.setenv("BALDUR_TEST_MODE", "TRUE")
        hook(MagicMock())

        monkeypatch.setenv("BALDUR_TEST_MODE", "True")
        hook(MagicMock())

    @pytest.mark.parametrize("env_value", ["false", "False", "0", "", "anything"])
    def test_session_start_raises_usage_error_when_not_true(
        self, monkeypatch, env_value
    ):
        """Any value that does not lower-case to ``"true"`` raises UsageError."""
        hook = self._get_session_start_hook()
        monkeypatch.setenv("BALDUR_TEST_MODE", env_value)

        with pytest.raises(pytest.UsageError, match="BALDUR_TEST_MODE"):
            hook(MagicMock())

    def test_session_start_raises_usage_error_when_unset(self, monkeypatch):
        """Missing ``BALDUR_TEST_MODE`` is a contract violation."""
        hook = self._get_session_start_hook()
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)

        with pytest.raises(pytest.UsageError, match="BALDUR_TEST_MODE"):
            hook(MagicMock())


class TestFlakyCategoriesContract:
    """455 D7: ``FLAKY_CATEGORIES`` is the single source of truth shared by
    the marker validator and any tooling that introspects ``category=``.

    Hardcoded against UNIT_TEST_GUIDELINES.md §6.6.3 — adding or renaming a
    category is a deliberate vocabulary change that must update this test,
    the runbook, and §6.6.3 in lockstep.
    """

    def test_flaky_categories_exact_seven_members(self):
        from tests.conftest import FLAKY_CATEGORIES

        assert FLAKY_CATEGORIES == frozenset(
            {
                "state_leak",
                "timing",
                "race_condition",
                "external_dep",
                "mock_leak",
                "env_isolation",
                "unknown",
            }
        )

    def test_flaky_categories_is_frozenset(self):
        from tests.conftest import FLAKY_CATEGORIES

        # frozenset prevents accidental mutation by importing tooling.
        assert isinstance(FLAKY_CATEGORIES, frozenset)


class TestFlakyQuarantineValidatorBehavior:
    """455 D2/D4: ``pytest_collection_modifyitems`` validates the
    ``flaky_quarantine`` marker schema — required ``issue``/``first_seen``,
    ISO-date format, and optional ``category`` membership in
    ``FLAKY_CATEGORIES``.
    """

    @staticmethod
    def _get_root_hook():
        from tests.conftest import pytest_collection_modifyitems

        return pytest_collection_modifyitems

    @staticmethod
    def _make_quarantined_item(tmp_path, kwargs):
        # File contents are irrelevant — the flaky_quarantine branch only
        # inspects marker kwargs (unlike the broken-validator which reads
        # the file for a `ref:` comment).
        test_file = tmp_path / "test_flaky.py"
        test_file.write_text("# placeholder\n", encoding="utf-8")
        item = _FakeItem(test_file, marker_names=[])
        item._markers = [_FakeMarker("flaky_quarantine", kwargs)]
        return item

    def test_valid_marker_with_all_fields_passes(self, tmp_path):
        # Given a marker with required + optional fields all valid
        hook = self._get_root_hook()
        item = self._make_quarantined_item(
            tmp_path,
            {
                "issue": "GH-477",
                "first_seen": "2026-04-26",
                "category": "state_leak",
                "notes": "transient under xdist",
            },
        )

        # When/Then: validator does not raise
        hook(MagicMock(), [item])

    def test_valid_marker_without_category_passes(self, tmp_path):
        """category is optional — omitting it must not trip the validator."""
        hook = self._get_root_hook()
        item = self._make_quarantined_item(
            tmp_path,
            {"issue": "455", "first_seen": "2026-01-15"},
        )

        hook(MagicMock(), [item])

    def test_non_quarantined_item_skips_validation(self, tmp_path):
        """Items without the marker bypass schema validation entirely
        (parallel to ``test_non_broken_item_skips_ref_validation``)."""
        hook = self._get_root_hook()
        test_file = tmp_path / "test_normal.py"
        test_file.write_text("# nothing special\n", encoding="utf-8")
        item = _FakeItem(test_file, marker_names=[])

        hook(MagicMock(), [item])

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            pytest.param({"first_seen": "2026-04-26"}, "issue=", id="missing_issue"),
            pytest.param(
                {"issue": "", "first_seen": "2026-04-26"},
                "issue=",
                id="empty_issue",
            ),
            pytest.param(
                {"issue": 123, "first_seen": "2026-04-26"},
                "issue=",
                id="non_string_issue",
            ),
            pytest.param({"issue": "GH-477"}, "first_seen=", id="missing_first_seen"),
            pytest.param(
                {"issue": "GH-477", "first_seen": ""},
                "first_seen=",
                id="empty_first_seen",
            ),
            pytest.param(
                {"issue": "GH-477", "first_seen": "04/26/2026"},
                "ISO date",
                id="bad_format_first_seen",
            ),
            pytest.param(
                {"issue": "GH-477", "first_seen": "2026-13-99"},
                "ISO date",
                id="invalid_calendar_first_seen",
            ),
            pytest.param(
                {
                    "issue": "GH-477",
                    "first_seen": "2026-04-26",
                    "category": "made_up",
                },
                "category=",
                id="bad_category",
            ),
        ],
    )
    def test_invalid_marker_kwargs_raise_usage_error(self, tmp_path, kwargs, match):
        hook = self._get_root_hook()
        item = self._make_quarantined_item(tmp_path, kwargs)

        with pytest.raises(pytest.UsageError, match=match):
            hook(MagicMock(), [item])

    def test_bad_category_error_lists_allowed_set(self, tmp_path):
        """Error message exposes the full allowed enum so authors can
        self-correct without reading the validator source."""
        hook = self._get_root_hook()
        item = self._make_quarantined_item(
            tmp_path,
            {
                "issue": "GH-477",
                "first_seen": "2026-04-26",
                "category": "wrong_category",
            },
        )

        with pytest.raises(pytest.UsageError, match="state_leak"):
            hook(MagicMock(), [item])

    def test_bad_date_chains_original_value_error(self, tmp_path):
        """``raise ... from exc`` preserves the strptime ``ValueError`` —
        per LOGGING_STANDARDS exception chaining rule."""
        hook = self._get_root_hook()
        item = self._make_quarantined_item(
            tmp_path,
            {"issue": "GH-477", "first_seen": "04/26/2026"},
        )

        with pytest.raises(pytest.UsageError) as exc_info:
            hook(MagicMock(), [item])

        assert isinstance(exc_info.value.__cause__, ValueError)
