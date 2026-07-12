"""G64 — the AI-discovery surface must not teach a stale API.

The AI-discovery surface ships two artifacts that *teach* Baldur's API to LLMs:

1. ``mkdocs.yml``'s ``llmstxt`` per-link descriptions + ``markdown_description``
   (rendered into the public ``site/llms.txt`` / ``llms-full.txt``).
2. ``AGENTS_GUIDANCE`` — the constant ``baldur init-ai`` writes into a user's
   ``AGENTS.md``.

Both name concrete public symbols (``@protected``, ``BaldurError``, decorator
keywords). Because the artifacts are *consumed by AI assistants* rather than
executed, ``mkdocs build --strict`` and the grep-based success criteria pass
even when a name goes stale — the artifact then confidently teaches a
non-existent symbol (``except SelfHealingError`` after the base class was
renamed to ``BaldurError``) or a PRO-only ``@bulkhead`` as if it were OSS. A
reader who is an LLM cannot tell the difference, so the drift is invisible to
every other gate.

This fitness function asserts that every public-API name the two artifacts
mention still resolves against the live OSS surface. A future rename then fails
the suite instead of silently shipping a misleading discovery surface.

Scope is OSS-only: it imports ``baldur`` (never ``baldur_pro``), so a PRO-only
symbol named in an OSS description is correctly flagged as unresolved — the OSS
docs site must not advertise PRO-tier symbols as part of the OSS API.

Rule registry: ``ARCHITECTURE.md#g64-ai-discovery-surface-drift``
"""

from __future__ import annotations

import inspect
import re
import types

import baldur
from baldur import decorators as _decorators
from baldur.cli.commands.init_ai import AGENTS_GUIDANCE
from baldur.core import exceptions as _exceptions
from tests.architecture.conftest import PROJECT_ROOT, mkdocs_safe_load

_MKDOCS_YML = PROJECT_ROOT / "mkdocs.yml"

# A ``@name`` mention in a description, capturing dotted forms so
# ``@baldur.protected`` reduces to its final segment ``protected``. The token
# must end on a word char, so a sentence-final period (``@domain_tag.``) is not
# swallowed into the name.
_DECORATOR_TOKEN = re.compile(r"@([A-Za-z_](?:[\w.]*[A-Za-z0-9_])?)")
# A CamelCase exception-class mention (``BaldurError``, ``SelfHealingError``).
_EXCEPTION_TOKEN = re.compile(r"\b([A-Z][A-Za-z0-9]*Error)\b")
# A ``baldur.<call>(`` reference in the AGENTS.md template. The trailing ``(``
# distinguishes a real API call from the ``https://baldur.sh/`` URL.
_BALDUR_CALL = re.compile(r"\bbaldur\.([a-z_]\w*)\s*\(")
# A backtick-anchored, lowercase ``kw=`` decorator keyword (``fallback=``).
# Anchoring on the backtick + lowercase excludes ``BALDUR_REDIS_URL=``.
_DECORATOR_KWARG = re.compile(r"`([a-z_]+)=")


# --- live OSS public surface -------------------------------------------------


def _oss_decorator_names() -> frozenset[str]:
    """Public names a ``@decorator`` mention may legitimately use.

    Top-level marquee API (``baldur.__all__`` — where ``@protected`` lives, and
    which is the authoritative listing because PEP 562 lazy attributes are
    absent from ``dir(baldur)``) plus the ``baldur.decorators`` preset/gate
    decorators. A PRO-only decorator (``@bulkhead``) is absent by construction.
    """
    return frozenset(getattr(baldur, "__all__", ())) | frozenset(
        getattr(_decorators, "__all__", ())
    )


def _oss_exception_names() -> frozenset[str]:
    """Public exception class names reachable on the OSS surface."""
    names = {
        name
        for name in dir(_exceptions)
        if not name.startswith("_")
        and isinstance(getattr(_exceptions, name), type)
        and issubclass(getattr(_exceptions, name), BaseException)
    }
    names |= {name for name in getattr(baldur, "__all__", ()) if name.endswith("Error")}
    return frozenset(names)


# --- artifact extraction -----------------------------------------------------


def _llmstxt_descriptions() -> list[tuple[str, str]]:
    """Return ``(location, text)`` for every rendered llmstxt description.

    Covers ``markdown_description`` and each ``{path: description}`` section
    item. A bare glob (``reference/baldur/*.md``) is a plain string with no
    description and is skipped — it carries no API claim of its own.
    """
    data = mkdocs_safe_load(_MKDOCS_YML.read_text(encoding="utf-8"))
    plugins = data.get("plugins", []) if isinstance(data, dict) else []
    block = None
    for entry in plugins:
        if isinstance(entry, dict) and "llmstxt" in entry:
            block = entry["llmstxt"]
            break
    assert block is not None, "llmstxt plugin block not found in mkdocs.yml"

    out: list[tuple[str, str]] = []
    markdown_description = block.get("markdown_description")
    if isinstance(markdown_description, str):
        out.append(("markdown_description", markdown_description))
    sections = block.get("sections", {})
    if isinstance(sections, dict):
        for section, items in sections.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                for path, desc in item.items():
                    if isinstance(desc, str):
                        out.append((f"{section}:{path}", desc))
    return out


def _decorator_mentions(text: str) -> set[str]:
    return {match.group(1).split(".")[-1] for match in _DECORATOR_TOKEN.finditer(text)}


def _exception_mentions(text: str) -> set[str]:
    return {match.group(1) for match in _EXCEPTION_TOKEN.finditer(text)}


# --- gates -------------------------------------------------------------------


class TestLlmsTxtDescriptionsTeachLiveApi:
    """Every symbol the llms.txt descriptions name resolves on the OSS surface."""

    def test_decorator_mentions_are_oss_public_symbols(self):
        legit = _oss_decorator_names()
        offenders: dict[str, set[str]] = {}
        for where, desc in _llmstxt_descriptions():
            for name in _decorator_mentions(desc):
                if name not in legit:
                    offenders.setdefault(where, set()).add(name)
        assert not offenders, (
            "llms.txt descriptions name @decorators absent from the OSS public "
            f"surface (stale or PRO-only — they misdirect an LLM reader): {offenders}"
        )

    def test_exception_mentions_are_oss_public_classes(self):
        legit = _oss_exception_names()
        offenders: dict[str, set[str]] = {}
        for where, desc in _llmstxt_descriptions():
            for name in _exception_mentions(desc):
                if name not in legit:
                    offenders.setdefault(where, set()).add(name)
        assert not offenders, (
            "llms.txt descriptions name exception classes that do not exist on "
            f"the OSS surface (an LLM would emit a non-importable `except`): {offenders}"
        )


class TestAgentsTemplateTeachesLiveApi:
    """``AGENTS_GUIDANCE`` (written into user repos) teaches only resolvable API."""

    def test_baldur_dotted_calls_resolve(self):
        # Reproduce the import order the AGENTS.md template teaches: a reader
        # touches the marquee decorator before the inline call. Pre-555 this
        # access loaded the ``baldur.protect`` submodule, which bound
        # ``globals()["protect"]`` to the *module* and permanently shadowed the
        # function — so ``baldur.protect`` resolved to a module and the inline
        # ``baldur.protect("name", fn)`` raised ``TypeError: 'module' object is
        # not callable``. ``hasattr`` could not catch this (the attribute
        # exists either way); a ``callable`` + non-module check can.
        _ = baldur.protected
        calls = set(_BALDUR_CALL.findall(AGENTS_GUIDANCE))
        assert calls, "expected baldur.<call>() references in AGENTS_GUIDANCE"
        shadowed = {
            call: type(getattr(baldur, call, None)).__name__
            for call in calls
            if not callable(getattr(baldur, call, None))
            or isinstance(getattr(baldur, call, None), types.ModuleType)
        }
        assert not shadowed, (
            "AGENTS_GUIDANCE teaches baldur.<x>() calls that do not resolve to a "
            "non-module callable on the top-level package — a submodule shadowing "
            f"the function makes the inline idiom raise TypeError: {shadowed}"
        )

    def test_decorator_keywords_are_real_protected_params(self):
        kwargs = set(_DECORATOR_KWARG.findall(AGENTS_GUIDANCE))
        assert kwargs, "expected `kw=` decorator keywords in AGENTS_GUIDANCE"
        params = set(inspect.signature(baldur.protected).parameters)
        unknown = kwargs - params
        assert not unknown, (
            "AGENTS_GUIDANCE teaches @protected keywords that are not parameters "
            f"of baldur.protected: {unknown} (real params: {sorted(params)})"
        )

    def test_decorator_mentions_are_oss_public_symbols(self):
        # Symmetry with the llms.txt decorator gate, applied to the template too.
        # The template's marquee mention is ``@baldur.protected`` (a dotted call,
        # already covered above), but a bare ``@decorator`` — a PRO-only
        # ``@bulkhead``, or ``@dlq_protect`` were it ever to leave the OSS surface —
        # is matched by neither _BALDUR_CALL nor _DECORATOR_KWARG, so without this
        # gate it would teach an LLM a name that does not resolve on OSS.
        legit = _oss_decorator_names()
        mentions = _decorator_mentions(AGENTS_GUIDANCE)
        assert mentions, (
            "expected at least the `@baldur.protected` mention in AGENTS_GUIDANCE"
        )
        unknown = mentions - legit
        assert not unknown, (
            "AGENTS_GUIDANCE names @decorators absent from the OSS public surface "
            f"(stale or PRO-only — they misdirect an LLM reader): {unknown}"
        )

    def test_exception_mentions_are_oss_public_classes(self):
        # AGENTS_GUIDANCE names no exception class today, so this gate is latent —
        # but a resilience guide is the artifact most likely to grow error-handling
        # advice ("let `CircuitBreakerError` propagate", "catch `BaldurError`"). It
        # guards that future addition against the 552 failure mode (a renamed or
        # PRO-only class like ``SelfHealingError``), which was previously caught
        # only when it appeared in the llms.txt descriptions. No non-empty assert:
        # zero mentions is the correct steady state.
        legit = _oss_exception_names()
        unknown = _exception_mentions(AGENTS_GUIDANCE) - legit
        assert not unknown, (
            "AGENTS_GUIDANCE names exception classes that do not exist on the OSS "
            f"surface (an LLM would emit a non-importable `except`): {unknown}"
        )
