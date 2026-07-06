"""pytest conftest for architectural fitness function tests.

The shared helpers live in ``_helpers.py`` to keep conftest.py under the
UNIT_TEST_GUIDELINES §5.3 size limit. This module re-exports the public API so
test files can still do ``from tests.architecture.conftest import ...``.

Monkeypatching the module-level state (``BASELINE_PATH``, ``_load_baseline_document``,
``_pyproject_data``, etc.) MUST target ``_helpers`` directly — the canonical
location where the closures read from.
"""

from __future__ import annotations

import pytest

from tests.architecture._helpers import (
    BASELINE_PATH,
    DEFAULT_SRC_ROOTS,
    DOC_ID_ALLOWLIST,
    DOC_ID_PATTERNS,
    KOREAN_RE,
    MODULE_SYMBOL,
    OSS_TESTS_ROOT,
    PROJECT_ROOT,
    REFERENCE_DIR,
    RULE_REGISTRY_DOC,
    baselined_count,
    collect_violations,
    core_dependency_modules,
    directive_targets,
    find_doc_ids,
    find_prose_leaks,
    format_violation,
    iter_docstrings,
    iter_inline_code_spans,
    load_baseline,
    mkdocs_safe_load,
    optional_extras_modules,
    parse_ast,
    resolve_all_chain_files,
    resolve_callsites,
    symbol_of,
    walk_src,
)


def pytest_collection_modifyitems(config, items):
    """Pin every architecture test to one xdist worker via ``loadgroup``.

    Each architecture rule walks the full ``src/baldur`` AST. ``parse_ast``
    caches AST per file (lru_cache), but the cache is process-local — so
    when xdist's default ``loadfile`` scatters rules across workers, each
    worker re-parses ~1,300 files. Tagging every test in this directory
    with the same ``xdist_group`` makes ``--dist=loadgroup`` route them all
    to one worker, letting later rules reuse the cache populated by the
    first one.
    """
    group_marker = pytest.mark.xdist_group("architecture_ast_cache")
    for item in items:
        nodeid = item.nodeid.replace("\\", "/")
        if "tests/architecture/" in nodeid:
            item.add_marker(group_marker)


__all__ = [
    "BASELINE_PATH",
    "DEFAULT_SRC_ROOTS",
    "DOC_ID_ALLOWLIST",
    "DOC_ID_PATTERNS",
    "KOREAN_RE",
    "MODULE_SYMBOL",
    "OSS_TESTS_ROOT",
    "PROJECT_ROOT",
    "REFERENCE_DIR",
    "RULE_REGISTRY_DOC",
    "baselined_count",
    "collect_violations",
    "core_dependency_modules",
    "directive_targets",
    "find_doc_ids",
    "find_prose_leaks",
    "format_violation",
    "iter_docstrings",
    "iter_inline_code_spans",
    "load_baseline",
    "mkdocs_safe_load",
    "optional_extras_modules",
    "parse_ast",
    "resolve_all_chain_files",
    "resolve_callsites",
    "symbol_of",
    "walk_src",
]
