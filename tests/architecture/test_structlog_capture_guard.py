"""Architectural fitness function (G34) — structlog ``capture_logs`` determinism.

Impl docs 578 + 584. The full ``-n6`` suite used to break a different set of
log-assertion tests every run: production ``configure_structlog()`` freezes a
module-level ``logger = structlog.get_logger()`` proxy via
``cache_logger_on_first_use=True``, and a frozen proxy is invisible to
``structlog.testing.capture_logs()`` (it silently returns an empty list). The
root conftest now contains the flake suite-wide with a single
function-scope autouse guard (``_restore_canonical_structlog_config``) that, at
every test boundary, re-asserts ``cache_logger_on_first_use=False`` + the
runtime ``configured`` flag via ``_apply_canonical_test_structlog_config()``
(prevents *new* freezes) and runs ``_unfreeze_module_loggers`` (584) to *un*-freeze
any ``baldur*`` proxy frozen earlier in the worker's life.

This gate keeps that centralization from rotting. It has three halves:

* **(a) No re-scattered guard.** Zero ``<module>.logger = structlog.get_logger()``
  rebinds remain anywhere under ``tests/`` — a module-logger reassignment has no
  legitimate use outside a per-file flake guard, so the false-positive rate is
  zero (the scanner skips its OWN file so its positive-case sample is not
  counted). Enforced-empty.
* **(b) Helper still works.** A functional pin of
  ``_apply_canonical_test_structlog_config()``: from a poisoned global config
  (``cache=True`` + ``configured=False``) the helper restores ``cache=False`` +
  ``configured=True`` and a freshly-created proxy's emit is capturable again.
* **(c) Un-freeze pin (584 D6).** A deterministic in-process proof that
  ``_unfreeze_module_loggers`` repairs an *already-frozen* proxy: freeze a proxy
  attached under a non-``logger`` name (proving the walk is name-independent),
  replace ``_CONFIG.default_processors`` with a fresh list so the freeze becomes
  uncapturable (578's exact residual), assert ``capture_logs`` is empty, run the
  un-freeze, then assert capture intercepts again. G34(b) only proves a *fresh*
  proxy is capturable — trivially true and silent about un-freezing.

Rule registry:
``ARCHITECTURE.md#g34-structlog-capture-guard``
"""

from __future__ import annotations

import ast
import logging
import types
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from tests.architecture._helpers import PROJECT_ROOT
from tests.architecture.conftest import (
    collect_violations,
    parse_ast,
    symbol_of,
)

_RULE_KEY = "structlog_capture_guard"
_RULE_ANCHOR = "#g34-structlog-capture-guard"

_TESTS_ROOT = PROJECT_ROOT / "tests"
_SELF_PATH = Path(__file__).resolve()


def _is_module_logger_rebind(node: ast.Assign) -> bool:
    """True iff ``node`` is ``<expr>.logger = structlog.get_logger(...)``.

    Matches the exact shape the removed per-file flake guards used: a single
    ``Attribute`` target named ``logger`` reassigned to a ``structlog.get_logger``
    call. A plain module-level binding ``logger = structlog.get_logger()`` (a
    ``Name`` target, present in every production module) is NOT matched — only an
    attribute reassignment is a rebind.
    """
    if len(node.targets) != 1:
        return False
    target = node.targets[0]
    if not isinstance(target, ast.Attribute) or target.attr != "logger":
        return False
    value = node.value
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "get_logger"
        and isinstance(func.value, ast.Name)
        and func.value.id == "structlog"
    )


def _scan(path: Path) -> list[tuple[Path, int, str, str]]:
    tree = parse_ast(path)
    if tree is None:
        return []
    violations: list[tuple[Path, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_module_logger_rebind(node):
            violations.append(
                (
                    path,
                    node.lineno,
                    symbol_of(tree, node),
                    "module-`logger` rebind (re-scattered structlog flake guard)",
                )
            )
    return violations


class TestStructlogCaptureGuard:
    """578 D4(a) — no test re-implements a module-`logger` rebind flake guard."""

    def test_no_module_logger_rebind_in_tests(self):
        raw: list[tuple[Path, int | None, str | None, str | None]] = []
        for path in _TESTS_ROOT.rglob("*.py"):
            if path.resolve() == _SELF_PATH:
                continue  # self-exclude (positive-case sample lives here)
            for offender in _scan(path):
                raw.append(offender)

        violations = collect_violations(_RULE_KEY, raw, _RULE_ANCHOR)
        assert not violations, (
            f"{len(violations)} module-`logger` rebind(s) found under tests/. "
            "The structlog capture flake is contained suite-wide by the root "
            "conftest `_restore_canonical_structlog_config` autouse fixture "
            "(578 D2) — delete the per-file rebind/`reset_defaults` guard "
            "instead of re-scattering it.\n" + "\n".join(violations)
        )


class TestStructlogCaptureGuardScannerAntiSilentPass:
    """G24-precedent — prove the scanner flags a rebind and clears the benign shapes.

    Inline-fixture coverage so part (a) cannot silently pass even though the live
    population is enforced-empty. The positive sample uses non-canonical
    whitespace so SC2's repo-wide grep (single-space literal, gate file
    ``--exclude``d) never matches this gate's own source.
    """

    @staticmethod
    def _rebinds(source: str) -> list[ast.Assign]:
        tree = ast.parse(source)
        return [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and _is_module_logger_rebind(n)
        ]

    def test_scanner_detects_module_logger_rebind(self):
        sample = "import structlog\nmod.logger  =  structlog.get_logger()\n"
        assert len(self._rebinds(sample)) == 1

    def test_scanner_ignores_plain_module_logger_binding(self):
        """A bare ``logger = structlog.get_logger()`` (Name target) is not a rebind."""
        sample = "import structlog\nlogger = structlog.get_logger()\n"
        assert self._rebinds(sample) == []

    def test_scanner_ignores_non_structlog_logger_attr(self):
        """``obj.logger = something_else.get_logger()`` is not the structlog shape."""
        sample = "obj.logger = factory.get_logger()\n"
        assert self._rebinds(sample) == []


class TestCanonicalStructlogConfigHelper:
    """578 D4(b) — `_apply_canonical_test_structlog_config` repairs a poisoned config."""

    def test_helper_restores_capture_after_poison(self):

        from baldur.observability.structlog_config import _structlog_state
        from tests.conftest import _apply_canonical_test_structlog_config

        # Poison: cache=True (re-arms the proxy freeze) + configured=False
        # (re-opens the door for production configure_structlog()).
        structlog.configure(cache_logger_on_first_use=True)
        _structlog_state().configured = False

        _apply_canonical_test_structlog_config()

        # The invariant is restored.
        assert structlog.get_config()["cache_logger_on_first_use"] is False
        assert _structlog_state().configured is True

        # A freshly-created proxy's emit is capturable again.
        logger = structlog.get_logger("g34.functional_pin")
        with capture_logs() as cap_logs:
            logger.info("g34.capture_probe")
        assert any(entry["event"] == "g34.capture_probe" for entry in cap_logs)


# =============================================================================
# 584 D6 — frozen-proxy un-freeze pin helpers
# =============================================================================


def _configure_structlog_canonical(*, cache: bool) -> None:
    """Configure structlog with the canonical stdlib chain and a FRESH processors
    list literal, mirroring the conftest per-setup reconfigure.

    Each call installs a NEW ``_CONFIG.default_processors`` list object. The 584
    un-freeze pin relies on that replacement to make a frozen proxy's cached list
    stale: structlog 25.5.0 ``capture_logs`` mutates the processors list in place,
    so a frozen proxy whose list was NOT replaced stays capturable and the pin
    would be a false canary.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=cache,
    )


def _freeze_then_stale(name: str):
    """Return a proxy in 578's exact residual state — frozen AND uncapturable.

    A ``cache=True`` first-emit freezes the proxy (the cache lands at
    ``proxy.__dict__["bind"]``); a subsequent fresh-list ``cache=False``
    reconfigure makes that frozen logger's processor list stale so
    ``capture_logs()`` no longer mutates it. On return both
    ``"bind" in vars(proxy)`` and an empty ``capture_logs()`` hold;
    ``_unfreeze_module_loggers`` must repair both.
    """
    _configure_structlog_canonical(cache=True)
    proxy = structlog.get_logger(name)
    proxy.info("g34.freeze_emit")  # first-emit freezes the proxy
    _configure_structlog_canonical(cache=False)  # fresh list → frozen list stale
    return proxy


class TestUnfreezeModuleLoggersPin:
    """584 D6 — deterministic in-process proof the un-freeze repairs a frozen proxy.

    G34(b) only proves a FRESH proxy is capturable after the helper (trivially
    true). This reproduces 578's residual — a proxy frozen earlier, made
    uncapturable by a later reconfigure — and proves ``_unfreeze_module_loggers``
    restores capture. Each assert names ``_unfreeze_module_loggers`` as the code
    to re-audit, so a future structlog bump that changes the cache representation
    or ``capture_logs``'s in-place-mutation semantics fails LOUD, not as a silent
    flake.
    """

    def test_unfreeze_repairs_frozen_non_logger_named_proxy(self):
        from tests.conftest import _unfreeze_module_loggers

        # Attach the proxy under a NON-`logger` name: dodges G34(a)'s matcher /
        # the SC grep AND proves the walk is name-independent (D5 — a name-keyed
        # predecessor would miss `_logger`, e.g. _import_policy._logger).
        fake_mod = types.ModuleType("g34_unfreeze_pin_fake")
        fake_mod._logger = _freeze_then_stale("g34.unfreeze_pin")

        assert "bind" in vars(fake_mod._logger), (
            "structlog 25.5.0 stores the cache freeze at proxy.__dict__['bind'] "
            "after a cache=True first-emit; it was not set. structlog's cache "
            "representation changed — re-audit _unfreeze_module_loggers (584 D1)."
        )

        # Residual reproduced: the still-frozen proxy is invisible to capture now
        # that _CONFIG.default_processors is a fresh list.
        with capture_logs() as before:
            fake_mod._logger.info("g34.unfreeze_pin.residual_probe")
        assert before == [], (
            "578's residual did not reproduce: a frozen proxy must be "
            "uncapturable after _CONFIG.default_processors is replaced. "
            "capture_logs's in-place-mutation semantics changed — re-audit "
            "_unfreeze_module_loggers (584 D6); without a reproduced residual "
            "this pin is a false canary."
        )

        _unfreeze_module_loggers([fake_mod])

        assert "bind" not in vars(fake_mod._logger), (
            "_unfreeze_module_loggers did not pop the 'bind' instance override "
            "(584 D1/D5)."
        )
        with capture_logs() as after:
            fake_mod._logger.info("g34.unfreeze_pin.repaired_probe")
        assert any(e["event"] == "g34.unfreeze_pin.repaired_probe" for e in after), (
            "un-freeze did not restore capture: popping 'bind' must restore "
            "class-method resolution that re-reads live _CONFIG. structlog's "
            "freeze/capture mechanism changed — re-audit _unfreeze_module_loggers "
            "(584 D6)."
        )


class TestUnfreezeModuleLoggersWalk:
    """584 D5 — per-module branch behavior of `_unfreeze_module_loggers`."""

    @pytest.mark.parametrize("attr_name", ["logger", "_logger"])
    def test_pops_frozen_proxy_by_type_regardless_of_attr_name(self, attr_name):
        """Type-scan pops a frozen proxy under ANY attribute name (D5).

        The name-keyed predecessor would have missed a non-`logger` proxy such as
        ``_import_policy._logger`` — a live `capture_logs` target.
        """
        from tests.conftest import _unfreeze_module_loggers

        mod = types.ModuleType(f"g34_walk_{attr_name}")
        setattr(mod, attr_name, _freeze_then_stale(f"g34.walk.{attr_name}"))
        proxy = getattr(mod, attr_name)
        assert "bind" in vars(proxy)
        _unfreeze_module_loggers([mod])
        assert "bind" not in vars(proxy)

    @pytest.mark.parametrize(
        ("make_value", "has_dict"),
        [
            pytest.param(
                lambda: structlog.get_logger("g34.walk.unfrozen"),
                True,
                id="unfrozen_proxy",
            ),
            pytest.param(
                lambda: logging.getLogger("g34.walk.stdlib"), True, id="stdlib_logger"
            ),
            pytest.param(lambda: 42, False, id="non_logger_object"),
        ],
    )
    def test_skips_non_frozen_globals_without_error(self, make_value, has_dict):
        """An unfrozen proxy, a stdlib logger, and a plain object are all left
        alone — the type-identity + ``"bind" in vars`` gates never pop them, and a
        module carrying only non-frozen globals is a safe no-op.
        """
        from tests.conftest import _unfreeze_module_loggers

        _configure_structlog_canonical(cache=False)
        mod = types.ModuleType("g34_walk_skip")
        mod.value = make_value()
        _unfreeze_module_loggers([mod])  # must not raise
        if has_dict:
            assert "bind" not in vars(mod.value)

    def test_none_and_non_module_entries_skipped(self):
        from tests.conftest import _unfreeze_module_loggers

        # A None placeholder / a non-module object have no __dict__; the narrow
        # guard skips them so one malformed sys.modules entry cannot error setup.
        _unfreeze_module_loggers([None, 42, "not-a-module"])  # must not raise

    def test_reads_vars_directly_not_getattr_pep562(self):
        """PEP 562: a module `__getattr__` must NOT be triggered (D5).

        The walk reads ``vars(mod)`` directly, so a frozen proxy in the real
        namespace is still repaired while a raising ``__getattr__`` is never
        consulted. A name-lookup refactor (``getattr(mod, "logger")``) would trip
        the trap and fail this pin.
        """
        from tests.conftest import _unfreeze_module_loggers

        mod = types.ModuleType("g34_walk_pep562")

        def _raising_getattr(name):
            raise AssertionError(
                f"the walk triggered PEP 562 __getattr__ for {name!r} — it must "
                "read vars(mod) directly, not getattr (584 D5)."
            )

        mod.__getattr__ = _raising_getattr
        mod.logger = _freeze_then_stale("g34.walk.pep562")
        _unfreeze_module_loggers([mod])  # reads vars(mod); __getattr__ untouched
        assert "bind" not in vars(mod.logger)
