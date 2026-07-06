"""Unit tests for the private heuristics in fitness-rule modules.

Two private helpers carry non-trivial classification logic and are called out
explicitly in impl doc 506 ``Test Assessment``:

- ``test_state_backend_ttl._receiver_looks_like_state_backend`` — the
  receiver-name heuristic that powers G11. Per D8.b it is intentionally
  permissive (suffix-based + factory-call) and Risk #2 documents the FP
  trade-off. These tests pin the documented true-positive and false-positive
  shapes so the heuristic cannot silently regress.
- ``test_all_declaration._has_module_getattr`` and ``_module_top_level_names``
  — the PEP 562 lazy-export skip and the conditional-def collection. Per D7
  modules that expose ``__getattr__`` get the ``__all__`` content check
  *skipped* (declaration-only) so re-export hubs don't false-positive.
"""

from __future__ import annotations

import ast
import textwrap

from tests.architecture.test_all_declaration import (
    _has_module_getattr,
    _module_top_level_names,
)
from tests.architecture.test_state_backend_ttl import (
    _receiver_looks_like_state_backend,
)


def _parse_expr(source: str) -> ast.expr:
    """Parse a single expression source into its `ast.expr` node."""
    module = ast.parse(source, mode="exec")
    statement = module.body[0]
    assert isinstance(statement, ast.Expr)
    return statement.value


def _parse_module(source: str) -> ast.Module:
    return ast.parse(textwrap.dedent(source))


class TestStateBackendReceiverBehavior:
    """G11 receiver heuristic — true positives, false positives, and skips."""

    def test_simple_name_with_backend_suffix_is_state_backend(self):
        # Given: variable named *_backend (e.g., `cb_backend.set(...)`)
        receiver = _parse_expr("cb_backend")
        # When/Then: heuristic matches the receiver-name suffix rule
        assert _receiver_looks_like_state_backend(receiver) is True

    def test_simple_name_with_state_suffix_is_state_backend(self):
        # `*_state` is one of the three documented suffixes
        receiver = _parse_expr("breaker_state")
        assert _receiver_looks_like_state_backend(receiver) is True

    def test_simple_name_with_store_suffix_is_state_backend(self):
        # `*_store` is the third documented suffix
        receiver = _parse_expr("cb_store")
        assert _receiver_looks_like_state_backend(receiver) is True

    def test_simple_name_without_any_suffix_is_not_state_backend(self):
        # Bare name `cb` — must NOT match, otherwise the heuristic would flood FPs
        receiver = _parse_expr("cb")
        assert _receiver_looks_like_state_backend(receiver) is False

    def test_attribute_with_backend_suffix_is_state_backend(self):
        # `self.state_backend` — attribute access with suffix match (D8.b)
        receiver = _parse_expr("self.state_backend")
        assert _receiver_looks_like_state_backend(receiver) is True

    def test_attribute_with_store_suffix_is_state_backend(self):
        # `self._cb_store` — leading underscore on attribute name is allowed
        receiver = _parse_expr("self._cb_store")
        assert _receiver_looks_like_state_backend(receiver) is True

    def test_attribute_without_matching_suffix_is_not_state_backend(self):
        # `self.cb` — attribute access without suffix; must NOT match
        receiver = _parse_expr("self.cb")
        assert _receiver_looks_like_state_backend(receiver) is False

    def test_direct_factory_call_get_state_backend_is_state_backend(self):
        # `get_state_backend()` — direct factory invocation per _DIRECT_FACTORIES
        receiver = _parse_expr("get_state_backend()")
        assert _receiver_looks_like_state_backend(receiver) is True

    def test_factory_call_through_module_attribute_is_state_backend(self):
        # `mod.get_state_backend()` — factory called via module alias
        receiver = _parse_expr("mod.get_state_backend()")
        assert _receiver_looks_like_state_backend(receiver) is True

    def test_unrelated_call_returning_dict_is_not_state_backend(self):
        # `dict()` is the canonical false-positive case the heuristic must skip.
        # If this regresses, `.set()` on dict.set / similar will be flagged.
        receiver = _parse_expr("dict()")
        assert _receiver_looks_like_state_backend(receiver) is False

    def test_unrelated_constant_receiver_is_not_state_backend(self):
        # `42` — non-Name/Attribute/Call expression; defensive default
        receiver = _parse_expr("42")
        assert _receiver_looks_like_state_backend(receiver) is False


class TestPep562LazyExportBehavior:
    """G9 module-`__getattr__` detection + conditional-def name collection."""

    def test_module_with_top_level_getattr_returns_true(self):
        tree = _parse_module(
            """
            def __getattr__(name):
                raise AttributeError(name)
            """
        )
        # PEP 562 lazy loader present → content check MUST be skipped (D7)
        assert _has_module_getattr(tree) is True

    def test_module_without_getattr_returns_false(self):
        tree = _parse_module(
            """
            def regular_function():
                return 1

            class Sample:
                pass
            """
        )
        assert _has_module_getattr(tree) is False

    def test_class_level_getattr_does_not_count(self):
        # `Cls.__getattr__` is instance attribute resolution, NOT module-level
        # PEP 562 — it MUST NOT trigger the skip.
        tree = _parse_module(
            """
            class Holder:
                def __getattr__(self, name):
                    raise AttributeError(name)
            """
        )
        assert _has_module_getattr(tree) is False

    def test_async_def_getattr_is_treated_as_module_getattr(self):
        # The collector accepts `FunctionDef` and `AsyncFunctionDef` (rare but
        # legitimate shape — symmetry with `_collect_names`).
        tree = _parse_module(
            """
            async def __getattr__(name):
                raise AttributeError(name)
            """
        )
        assert _has_module_getattr(tree) is True

    def test_collect_names_includes_top_level_def_and_class(self):
        tree = _parse_module(
            """
            def foo():
                pass

            class Bar:
                pass

            VALUE = 1
            """
        )
        names = _module_top_level_names(tree)
        assert {"foo", "Bar", "VALUE"} <= names

    def test_collect_names_walks_into_try_body_handler_orelse_finally(self):
        # Conditional definitions inside try/except MUST be collected so that
        # `__all__` referencing them does not false-positive.
        tree = _parse_module(
            """
            try:
                from optional_lib import RealThing
            except ImportError:
                class RealThing:
                    pass
            else:
                ELSE_FLAG = True
            finally:
                FINALLY_FLAG = True
            """
        )
        names = _module_top_level_names(tree)
        assert {"RealThing", "ELSE_FLAG", "FINALLY_FLAG"} <= names

    def test_collect_names_walks_into_if_branches(self):
        # `if HAS_FEATURE: def ... else: def ...` shape — both branches collect
        tree = _parse_module(
            """
            HAS_FEATURE = True
            if HAS_FEATURE:
                def feature_impl():
                    return 1
            else:
                def feature_impl():
                    return None
            """
        )
        names = _module_top_level_names(tree)
        assert "feature_impl" in names

    def test_collect_names_includes_re_imports(self):
        # `from x import name` re-exports — appear in __all__, must be collected
        tree = _parse_module(
            """
            from other import RealClass
            from other import original as alias
            """
        )
        names = _module_top_level_names(tree)
        # Re-imports: alias name when `as`, original name otherwise
        assert "RealClass" in names
        assert "alias" in names

    def test_collect_names_includes_annassign_target(self):
        # `NAME: int = 1` — AnnAssign with Name target is collected
        tree = _parse_module(
            """
            COUNT: int = 1
            label: str = "x"
            """
        )
        names = _module_top_level_names(tree)
        assert {"COUNT", "label"} <= names


__all__ = [
    "TestPep562LazyExportBehavior",
    "TestStateBackendReceiverBehavior",
]
