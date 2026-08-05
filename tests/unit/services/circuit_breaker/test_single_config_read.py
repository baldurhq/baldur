"""Source-level gate: one configuration read per breaker method (744 D15/G19).

The configuration a breaker reads is process-shared and swapped in place by a
runtime invalidation. A method that resolves it twice can therefore decide on
one value and report — or audit, or consume budget against — another, with no
exception and no log to show for it. The rule is that each public method
resolves the configuration once and threads it to its helpers, and that helpers
never resolve one of their own.

This is asserted on the source rather than on behavior: reproducing the race
needs an invalidation landing between two specific reads inside one method,
which is neither deterministic nor exhaustive. The AST walk is both.

Verification technique (§8): a structural contract over the three modules that
carry breaker methods.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from baldur.services.circuit_breaker import manual_control, protection, service

# The three modules whose classes hold breaker methods.
_SCANNED_MODULES = (service, protection, manual_control)

# The only members allowed to resolve the configuration.
#
# ``get_effective_config`` IS the resolver (it reads the base config and applies
# any mesh override); ``config`` is the property that returns it; ``__init__``
# predates any resolution. Anything else added here widens the rule, which is
# why the set is asserted below to be exactly these three — a silent addition
# fails the gate rather than quietly exempting a new method.
_EXEMPT_MEMBERS = frozenset({"get_effective_config", "config", "__init__"})


def _config_reads(node: ast.AST) -> int:
    """Count ``self.config`` reads and ``self.get_effective_config(...)`` calls."""
    count = 0
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and child.attr == "config"
            and isinstance(child.ctx, ast.Load)
            and isinstance(child.value, ast.Name)
            and child.value.id == "self"
        ):
            count += 1
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "get_effective_config"
        ):
            count += 1
    return count


def _methods_with_reads() -> list[tuple[str, str, str, int]]:
    """Return ``(module, class, method, read_count)`` for every class method."""
    rows: list[tuple[str, str, str, int]] = []
    for module in _SCANNED_MODULES:
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_name = module.__name__.rsplit(".", 1)[-1]
        for class_def in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for member in class_def.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                rows.append(
                    (
                        module_name,
                        class_def.name,
                        member.name,
                        _config_reads(member),
                    )
                )
    return rows


class TestSingleConfigReadPerMethod:
    """Each breaker method resolves the configuration at most once."""

    def test_exempt_member_set_is_exactly_the_three_resolvers(self):
        """A widening of the exemption set is a visible diff AND a failure —
        without this, exempting a new method would silently retire the rule."""
        assert _EXEMPT_MEMBERS == {"get_effective_config", "config", "__init__"}

    def test_no_method_resolves_the_configuration_more_than_once(self):
        offenders = [
            f"{module}::{cls}.{method} ({count} reads)"
            for module, cls, method, count in _methods_with_reads()
            if method not in _EXEMPT_MEMBERS and count > 1
        ]

        assert not offenders, (
            "A method that resolves the shared configuration twice can decide "
            "on one value and report another after an invalidation lands "
            "between the two reads. Resolve once and pass it down: "
            f"{offenders}"
        )

    def test_no_private_helper_resolves_a_configuration_of_its_own(self):
        """Helpers receive the config the caller decided against. One that
        resolves its own is exactly how a decision and its audit reason come
        to disagree."""
        offenders = [
            f"{module}::{cls}.{method} ({count} reads)"
            for module, cls, method, count in _methods_with_reads()
            if method.startswith("_") and method not in _EXEMPT_MEMBERS and count > 0
        ]

        assert not offenders, (
            "A private helper must take the effective config as an argument "
            f"rather than resolving one: {offenders}"
        )

    @pytest.mark.parametrize(
        "module_name",
        [module.__name__.rsplit(".", 1)[-1] for module in _SCANNED_MODULES],
    )
    def test_every_scanned_module_actually_contributed_methods(self, module_name):
        """Guards the walk itself: a renamed or relocated module would make the
        two assertions above pass vacuously."""
        assert [row for row in _methods_with_reads() if row[0] == module_name]

    def test_the_scan_finds_the_reads_it_is_supposed_to_find(self):
        """Negative control: the counter is not returning zero everywhere. The
        public admission methods do resolve the configuration — exactly once."""
        counted = {
            (cls, method): count
            for _module, cls, method, count in _methods_with_reads()
            if count > 0
        }

        assert counted, "the AST walk found no configuration reads at all"
        assert counted[("CircuitBreakerService", "should_allow")] == 1
