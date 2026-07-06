"""Unit tests for the 533 import-graph PRO-leak classifier.

``scripts/classify_pro_importing_tests.py`` is the single source of truth for
the 533 move plan AND the G21 fitness gate (533 D12 — G21 reuses
``classify_file``). These tests cover the pure functions enumerated in impl doc
533's ``Test Assessment`` section:

- ``classify_source`` count-based verdict over the 4 buckets + none
  (TestClassifySourceVerdict)
- ``classify_source`` private-symbol / private-module / wildcard fields
  (TestClassifySourcePrivateLeak)
- ``classify_tree`` move/stay/private-leak bucketing over a ``tmp_path`` tree
  (TestClassifyTreeBuckets)

All targets are stdlib-``ast``/string pure functions — no DI, no infra — so the
mock point is the file system via ``tmp_path`` (precedent:
``test_conftest_helpers.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.classify_pro_importing_tests import (
    NONE,
    PRO_DOMINANT,
    PURE_PRO,
    STAY_BASENAMES,
    SUPPORT_ONLY,
    TRUE_BOUNDARY,
    classify_source,
    classify_tree,
)


class TestClassifySourceVerdict:
    """`classify_source` count-based SUT verdict over the 4 buckets + none (533 D1)."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            pytest.param("import os\n", NONE, id="no-pro-import-none"),
            pytest.param(
                "from baldur.services.cb import CB\n", NONE, id="oss-only-none"
            ),
            pytest.param(
                "from baldur_pro.services.x import Foo\n",
                PURE_PRO,
                id="pro-only-pure_pro",
            ),
            pytest.param(
                "from baldur_pro.x import Foo\nfrom baldur.settings.base import S\n",
                SUPPORT_ONLY,
                id="pro-plus-all-support-support_only",
            ),
            pytest.param(
                "from baldur_pro.x import F\nfrom baldur.core.exceptions import E\n",
                SUPPORT_ONLY,
                id="support-exact-match",
            ),
            pytest.param(
                "from baldur_pro.a import A\n"
                "from baldur_pro.b import B\n"
                "from baldur.services.cb import CB\n",
                PRO_DOMINANT,
                id="pro2-oss1-pro_dominant",
            ),
            pytest.param(
                "from baldur_pro.a import A\n"
                "from baldur.services.cb import CB\n"
                "from baldur.services.dlq import DLQ\n",
                TRUE_BOUNDARY,
                id="pro1-oss2-true_boundary",
            ),
        ],
    )
    def test_verdict_partitions_by_import_graph(self, source: str, expected: str):
        result = classify_source(source)
        assert result is not None
        assert result.verdict == expected

    def test_equal_pro_oss_counts_classify_as_true_boundary_not_dominant(self):
        # Boundary: `pro_count > oss_count` is strict — at oss == pro the file is
        # true_boundary (stays); one above the boundary (pro=2, oss=1) is
        # pro_dominant.
        equal = classify_source(
            "from baldur_pro.a import A\nfrom baldur_pro.b import B\n"
            "from baldur.services.cb import C\nfrom baldur.services.dlq import D\n"
        )
        dominant = classify_source(
            "from baldur_pro.a import A\nfrom baldur_pro.b import B\n"
            "from baldur.services.cb import C\n"
        )
        assert equal is not None
        assert dominant is not None
        assert equal.verdict == TRUE_BOUNDARY  # pro=2 == oss=2
        assert dominant.verdict == PRO_DOMINANT  # pro=2 > oss=1

    def test_single_nonsupport_oss_import_flips_support_only_off(self):
        # support_only requires EVERY oss import in the support set. One
        # non-support import (baldur.core.circuit_breaker is not the support
        # baldur.core.exceptions) flips the verdict off support_only.
        all_support = classify_source(
            "from baldur_pro.x import F\nfrom baldur.core.exceptions import E\n"
        )
        one_nonsupport = classify_source(
            "from baldur_pro.x import F\nfrom baldur.core.exceptions import E\n"
            "from baldur.core.circuit_breaker import CB\n"
        )
        assert all_support is not None
        assert one_nonsupport is not None
        assert all_support.verdict == SUPPORT_ONLY
        assert one_nonsupport.verdict == TRUE_BOUNDARY  # pro=1, oss=2, not all-support

    def test_support_prefix_requires_dot_boundary_not_string_prefix(self):
        # _is_support matches `module == p` or `module.startswith(p + ".")`, NOT a
        # bare string prefix: `baldur.settings_private` string-starts-with
        # `baldur.settings` yet is NOT support.
        result = classify_source(
            "from baldur_pro.x import F\nfrom baldur.settings_private import X\n"
        )
        assert result is not None
        # settings_private is non-support → not support_only; pro=1, oss=1 → boundary
        assert result.verdict == TRUE_BOUNDARY

    def test_unparseable_source_returns_none(self):
        assert classify_source("def f(:\n") is None


class TestClassifySourcePrivateLeak:
    """`classify_source` private-symbol / private-module / wildcard fields (533 D1 / G20 axis)."""

    @pytest.mark.parametrize(
        ("source", "private", "wildcard", "has_dormant"),
        [
            pytest.param(
                "from baldur_pro.x import _foo\n",
                ("baldur_pro.x._foo",),
                (),
                False,
                id="private-symbol",
            ),
            pytest.param(
                "from baldur_pro.a._helpers import PUBLIC\n",
                ("baldur_pro.a._helpers",),
                (),
                False,
                id="private-module-path",
            ),
            pytest.param(
                "from baldur_pro.a._helpers import _x\n",
                ("baldur_pro.a._helpers", "baldur_pro.a._helpers._x"),
                (),
                False,
                id="private-module-and-symbol",
            ),
            pytest.param(
                "from baldur_pro.x import *\n",
                (),
                ("baldur_pro.x.*",),
                False,
                id="wildcard",
            ),
            pytest.param(
                "from baldur_pro.x import __all__\n",
                (),
                (),
                False,
                id="dunder-excluded",
            ),
            pytest.param(
                "from baldur_pro.x import Public\n",
                (),
                (),
                False,
                id="public-symbol-public-module",
            ),
            pytest.param(
                "from baldur_dormant.x import _foo\n",
                ("baldur_dormant.x._foo",),
                (),
                True,
                id="dormant-private-symbol",
            ),
            pytest.param(
                "import baldur_pro.a._helpers\n",
                ("baldur_pro.a._helpers",),
                (),
                False,
                id="import-form-private-module",
            ),
            pytest.param(
                "import baldur_pro.x\n",
                (),
                (),
                False,
                id="import-form-public",
            ),
            pytest.param(
                "from baldur.x import _foo\n",
                (),
                (),
                False,
                id="oss-underscore-not-gated",
            ),
        ],
    )
    def test_private_leak_fields_track_import_shape(
        self,
        source: str,
        private: tuple[str, ...],
        wildcard: tuple[str, ...],
        has_dormant: bool,
    ):
        result = classify_source(source)
        assert result is not None
        assert result.private_imports == private
        assert result.wildcard_imports == wildcard
        assert result.has_dormant is has_dormant
        assert result.has_private_leak is (bool(private) or bool(wildcard))

    def test_private_imports_deduplicated_preserving_order(self):
        # dict.fromkeys de-dups repeated imports while preserving first-seen order.
        result = classify_source(
            "from baldur_pro.x import _foo\nfrom baldur_pro.x import _foo\n"
        )
        assert result is not None
        assert result.private_imports == ("baldur_pro.x._foo",)


class TestClassifyTreeBuckets:
    """`classify_tree` move/stay/private-leak bucketing over a `tmp_path` tree (533 D2/D3)."""

    @pytest.fixture
    def oss_tree(self, tmp_path: Path) -> tuple[Path, str]:
        """Build a miniature tests/ tree exercising every classify_tree branch."""
        root = tmp_path / "tests" / "oss"
        unit = root / "unit"
        unit.mkdir(parents=True)
        # pure_pro test → auto_move
        (unit / "test_pure.py").write_text(
            "from baldur_pro.services.x import Foo\n", encoding="utf-8"
        )
        # support_only test → auto_move
        (unit / "test_support.py").write_text(
            "from baldur_pro.services.x import Foo\nfrom baldur.settings.base import S\n",
            encoding="utf-8",
        )
        # pro_dominant non-STAY test → pro_dominant_move
        (unit / "test_dominant.py").write_text(
            "from baldur_pro.a import A\nfrom baldur_pro.b import B\n"
            "from baldur.services.cb import CB\n",
            encoding="utf-8",
        )
        # pro_dominant STAY-basename test → stay (named exception, 533 D3)
        stay_name = sorted(STAY_BASENAMES)[0]
        (unit / stay_name).write_text(
            "from baldur_pro.a import A\nfrom baldur_pro.b import B\n"
            "from baldur.services.cb import CB\n",
            encoding="utf-8",
        )
        # true_boundary test → stay
        (unit / "test_boundary.py").write_text(
            "from baldur_pro.a import A\nfrom baldur.services.cb import CB\n"
            "from baldur.services.dlq import DLQ\n",
            encoding="utf-8",
        )
        # conftest leaking a PRO private symbol → private_leak only (NOT a move bucket)
        (unit / "conftest.py").write_text(
            "from baldur_pro.x import _secret\n", encoding="utf-8"
        )
        # test leaking a PRO private symbol → private_leak AND auto_move (pure_pro)
        (unit / "test_priv.py").write_text(
            "from baldur_pro.x import _secret\n", encoding="utf-8"
        )
        # NONE file (only baldur) → no bucket, no private_leak
        (unit / "test_none.py").write_text(
            "from baldur.services.cb import CB\n", encoding="utf-8"
        )
        return root, stay_name

    @staticmethod
    def _names(paths: list[Path]) -> set[str]:
        return {p.name for p in paths}

    def test_auto_move_holds_pure_pro_and_support_only_test_files(
        self, oss_tree: tuple[Path, str]
    ):
        root, _ = oss_tree
        result = classify_tree(root)
        assert self._names(result.auto_move) == {
            "test_pure.py",
            "test_support.py",
            "test_priv.py",
        }

    def test_pro_dominant_move_excludes_named_stay_basename(
        self, oss_tree: tuple[Path, str]
    ):
        root, stay_name = oss_tree
        result = classify_tree(root)
        move_names = self._names(result.pro_dominant_move)
        assert move_names == {"test_dominant.py"}
        assert stay_name not in move_names

    def test_stay_holds_named_pro_dominant_and_true_boundary(
        self, oss_tree: tuple[Path, str]
    ):
        root, stay_name = oss_tree
        result = classify_tree(root)
        stay_names = self._names(result.stay)
        assert stay_name in stay_names
        assert "test_boundary.py" in stay_names

    def test_private_leak_covers_all_py_including_conftest(
        self, oss_tree: tuple[Path, str]
    ):
        root, _ = oss_tree
        result = classify_tree(root)
        leak_names = self._names(result.private_leak)
        # conftest.py is directory infra (never a move bucket) but its PRO private
        # import is still a real leak the report must surface.
        assert leak_names == {"conftest.py", "test_priv.py"}

    def test_conftest_is_directory_infra_not_a_move_bucket(
        self, oss_tree: tuple[Path, str]
    ):
        root, _ = oss_tree
        result = classify_tree(root)
        for bucket in (result.auto_move, result.pro_dominant_move, result.stay):
            assert "conftest.py" not in self._names(bucket)

    def test_none_verdict_file_is_in_no_bucket(self, oss_tree: tuple[Path, str]):
        root, _ = oss_tree
        result = classify_tree(root)
        none_path = root / "unit" / "test_none.py"
        assert result.verdicts[none_path].verdict == NONE
        for bucket in (
            result.auto_move,
            result.pro_dominant_move,
            result.stay,
            result.private_leak,
        ):
            assert "test_none.py" not in self._names(bucket)


__all__ = [
    "TestClassifySourcePrivateLeak",
    "TestClassifySourceVerdict",
    "TestClassifyTreeBuckets",
]
