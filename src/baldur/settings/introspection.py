"""Settings-class introspection for runtime env-var validation.

Production home for the ``BaseSettings`` reflection helpers that both the
startup unknown-env-var scan (``bootstrap._warn_unknown_env_vars``) and the two
documentation gates (G15 env-prefix naming, G30 allowlist-resolves) consume, so
the longest-prefix resolver has a single source of truth instead of a copy in
the test tree.

The runtime scan classifies every ``BALDUR_*`` key present in ``os.environ`` as
either *known* (it maps to a real Pydantic settings field OR is a catalogued
direct-read var) or *unknown* (a typo / stale var the operator should hear
about). "Known" therefore unions two namespaces:

1. **Pydantic field-level** — the ``(env_prefix, field)`` set reflected from
   every ``BaseSettings`` subclass. ``resolve_env_var`` walks the longest
   registered prefix and checks the remainder is a real field, matching
   Pydantic's own consumption (incl. ``case_sensitive=False`` and
   ``env_nested_delimiter="__"``).
2. **Direct-read registry** — the ``BALDUR_*`` vars that legitimately bypass
   Pydantic and are read straight from ``os.environ`` (the
   :data:`KNOWN_DIRECT_READ_ENV_VARS` committed constant, drift-guarded by the
   direct-read registry fitness function) plus any names contributed at import
   via :func:`register_direct_read_env_vars` (the pro / plugin / computed-read
   seam).

**Import laziness (load-bearing).** Nothing in this module reflects or
force-loads at import time — every settings-tree walk happens inside a function
body. ``core.degraded_mode_handler`` imports :func:`register_direct_read_env_vars`
at its own import, and force-loading the settings tree transitively re-imports
``baldur.core``; reflecting at module load would close a core->settings->core
cycle. Keeping reflection lazy keeps that import edge one-way.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable

from pydantic_settings import BaseSettings

__all__ = [
    "KNOWN_DIRECT_READ_ENV_VARS",
    "build_prefix_index",
    "collect_baldur_settings",
    "force_load_settings_modules",
    "is_known_env_var",
    "known_env_var_names",
    "register_direct_read_env_vars",
    "resolve_env_var",
]


# ---------------------------------------------------------------------------
# Settings-class enumeration (lifted from G15/G30 — single source of truth).
# ---------------------------------------------------------------------------
def _iter_all_subclasses(root: type) -> set[type]:
    """Return every transitive subclass of ``root`` (depth-first, dedup)."""
    seen: set[type] = set()
    stack = [root]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub in seen:
                continue
            seen.add(sub)
            stack.append(sub)
    return seen


def collect_baldur_settings() -> list[tuple[type, str]]:
    """Return ``[(class, env_prefix), ...]`` for every baldur settings class.

    Bare-reflection: imports the ``baldur.settings`` package (so the eagerly
    re-exported subclasses register) and walks ``BaseSettings.__subclasses__()``
    transitively, filtered to classes defined under ``baldur.settings.`` or
    ``baldur_pro.`` (pro classes register only when ``baldur_pro`` is installed).
    Includes private ``_*`` helper settings classes.

    Does NOT force-load lazily-imported settings submodules — a bare
    ``import baldur.settings`` leaves some subclasses (e.g. ``DLQOutboxSettings``)
    unregistered. Callers that need the complete surface use
    :func:`build_prefix_index`, which force-loads first.
    """
    importlib.import_module("baldur.settings")
    result: list[tuple[type, str]] = []
    for cls in _iter_all_subclasses(BaseSettings):
        module_name = cls.__module__ or ""
        if not module_name.startswith(
            "baldur.settings."
        ) and not module_name.startswith("baldur_pro."):
            continue
        prefix = cls.model_config.get("env_prefix") if cls.model_config else None
        if not isinstance(prefix, str):
            continue
        result.append((cls, prefix))
    return result


def force_load_settings_modules() -> None:
    """Import every ``baldur.settings.*`` submodule.

    ``import baldur.settings`` alone is insufficient — the package ``__init__``
    does not eagerly load all submodules, so a bare import leaves some
    ``BaseSettings`` subclasses (e.g. ``DLQOutboxSettings``) unregistered and a
    legitimate var backed by a lazily-loaded module (``BALDUR_DLQ_OUTBOX_ENABLED``)
    would resolve to nothing. A submodule that fails to import (optional heavy
    dep) is skipped; its prefix simply stays unregistered, exactly as a bare
    import would leave it.
    """
    package = importlib.import_module("baldur.settings")
    for module in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        try:
            importlib.import_module(module.name)
        except Exception:
            continue


def build_prefix_index() -> dict[str, set[str]]:
    """Return ``{env_prefix: {field_name, ...}}`` for the whole settings surface.

    Force-loads every settings submodule first so the index is complete (the
    lazy-load gap above), then reflects ``model_fields`` per class. Field names
    are the Python (lower-case snake) field names, matched against the
    lower-cased var remainder by :func:`resolve_env_var`. Built once per scan,
    not per var.
    """
    force_load_settings_modules()
    index: dict[str, set[str]] = {}
    for cls, prefix in collect_baldur_settings():
        index.setdefault(prefix, set()).update(cls.model_fields.keys())
    return index


def _longest_prefix(var_upper: str, prefixes: Iterable[str]) -> str | None:
    """Return the longest registered prefix that ``var_upper`` starts with, or None."""
    best: str | None = None
    for prefix in prefixes:
        if var_upper.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return best


def resolve_env_var(var: str, index: dict[str, set[str]]) -> bool:
    """True iff ``var`` maps to a real ``(env_prefix, field)`` Pydantic sink.

    Models Pydantic's consumption:

    * **case-insensitive** — every settings class uses Pydantic's default
      ``case_sensitive=False``, so ``BALDUR_Dlq_Max_Size`` is consumed
      identically to ``BALDUR_DLQ_MAX_SIZE``; the var is upper-cased before
      prefix/field matching;
    * **longest-prefix** — ``BALDUR_DLQ_OUTBOX_`` must beat ``BALDUR_DLQ_`` for
      ``BALDUR_DLQ_OUTBOX_ENABLED``;
    * **nested delimiter** — with ``env_nested_delimiter="__"`` a var such as
      ``BALDUR_X_SUB__FIELD`` resolves on its first ``__`` segment (the
      sub-config attribute name).

    A bare prefix with no remainder (``BALDUR_DLQ_``) and a degenerate
    triple-delimiter remainder (empty first ``__`` segment) both resolve to
    ``False`` without raising.
    """
    var_upper = var.upper()
    prefix = _longest_prefix(var_upper, index)
    if prefix is None:
        return False
    remainder = var_upper[len(prefix) :].lower()
    if not remainder:
        return False
    field = remainder.split("__", 1)[0] if "__" in remainder else remainder
    if not field:
        return False
    return field in index[prefix]


# ---------------------------------------------------------------------------
# Direct-read registry — BALDUR_* vars consumed straight from os.environ.
# ---------------------------------------------------------------------------
# Channel 1 — committed constant: the OSS ``BALDUR_*`` vars read via an
# ``os.environ.get("BALDUR_…")`` / ``os.getenv(...)`` / ``os.environ[...]``
# STRING LITERAL anywhere under ``src/baldur``. Drift-guarded enforced-equal by
# the direct-read registry fitness function (``test_direct_read_env_registry.py``)
# — on drift the gate prints the exact add/remove diff to paste here. Computed
# reads (``f"BALDUR_{k}"``) are invisible to the literal scan by construction and
# live in Channel 2 instead.
KNOWN_DIRECT_READ_ENV_VARS: frozenset[str] = frozenset(
    {
        # --- bootstrap / runtime identity (read before settings are wired) ---
        "BALDUR_CLUSTER_ID",
        "BALDUR_ENV",
        "BALDUR_ENVIRONMENT",
        "BALDUR_EXECUTION_MODE",
        "BALDUR_FAIL_FAST",
        "BALDUR_RECOVERY_ADAPTER",
        "BALDUR_TEST_MODE",
        "BALDUR_TIER_MANIFEST_PATH",
        "BALDUR_CB_STATE_SEED_AUTOSTART",
        "BALDUR_META_WATCHDOG_AUTOSTART",
        "BALDUR_OTEL_AUTOSTART",
        "BALDUR_PRECOMPUTED_CACHE_AUTOSTART",
        "BALDUR_SCHEDULER_AUTOSTART",
        "BALDUR_SYSTEM_METRICS_CACHE_AUTOSTART",
        # --- diagnostic suppress flags (read by the diagnostics themselves) ---
        "BALDUR_SUPPRESS_TIER_WARNING",
        "BALDUR_SUPPRESS_UNKNOWN_ENV_WARNING",
        # --- logging / observability ---
        "BALDUR_LOG_LEVEL",
        "BALDUR_TEST_LOG_LEVEL",
        "BALDUR_TRACE_URL_TEMPLATE",
        "BALDUR_EMERGENCY_ESCALATION_LOG",
        "BALDUR_EMERGENCY_DUMP_DIR",
        # --- namespace identity (also field-resolvable; read directly too) ---
        "BALDUR_NAMESPACE_ENV",
        "BALDUR_NAMESPACE_REGION",
        # --- storage / connection (Redis / SQL / data dirs) ---
        "BALDUR_REDIS_URL",
        "BALDUR_REDIS_MAX_FALLBACK",
        "BALDUR_SQL_DSN",
        "BALDUR_DATA_DIR",
        "BALDUR_AUDIT_PATH",
        "BALDUR_AUDIT_LOG_DIR",
        "BALDUR_AUDIT_WAL_DIR",
        "BALDUR_MAX_PIPELINE_CHUNK",
        "BALDUR_SAFETY_LTRIM",
        # --- air-gap adapter ---
        "BALDUR_AIRGAP_ENABLED",
        "BALDUR_AIRGAP_PREFIX",
        "BALDUR_AIRGAP_REDIS_URL",
        "BALDUR_AIRGAP_TTL",
        # --- audit checkpoint / buffer ---
        "BALDUR_CHECKPOINT_ENABLE_FILE_BACKUP",
        "BALDUR_CHECKPOINT_ENABLE_NOTIFICATION",
        "BALDUR_CHECKPOINT_STORAGE",
        "BALDUR_CHECKPOINT_USE_DISTRIBUTED_LOCK",
        "BALDUR_BUFFER_CRITICAL",
        "BALDUR_BUFFER_TYPE",
        "BALDUR_BUFFER_WARNING",
        # --- Django middleware enable-toggles ---
        "BALDUR_ACTOR_MIDDLEWARE_ENABLED",
        "BALDUR_POOL_CB_MIDDLEWARE_ENABLED",
        # --- deadline / admission-control context ---
        "BALDUR_DEADLINE_ENABLED",
        "BALDUR_DEADLINE_DEFAULT_ESTIMATED_MS_CRITICAL",
        "BALDUR_DEADLINE_DEFAULT_ESTIMATED_MS_NON_ESSENTIAL",
        "BALDUR_DEADLINE_DEFAULT_ESTIMATED_MS_STANDARD",
        "BALDUR_DEADLINE_MINIMUM_USEFUL_MS",
        "BALDUR_DEADLINE_NETWORK_BUFFER_MS",
        "BALDUR_DEADLINE_RTT_MIN_SAMPLE_MS",
        "BALDUR_DEADLINE_RTT_SAMPLE_RATE",
        "BALDUR_DEADLINE_SAFETY_MARGIN",
        # --- context ---
        # BALDUR_RUNBOOK_ASYNC_EXECUTION / BALDUR_RUNBOOK_SUBSCRIBE_EVENTS
        # moved to Channel 2 territory with the runbook implementation
        # (599 D2 - the os.environ literals now live in baldur_pro, outside
        # this registry's OSS scan scope).
        "BALDUR_STRICT_CELL_CONTEXT",
    }
)

# Channel 2 — runtime contributions whose names are NOT source literals: pro
# vars (contributed at ``baldur_pro`` import), enumerable-but-computed reads
# (e.g. ``DegradedModeHandler``'s ``f"BALDUR_{k}"`` over its closed ``_defaults``
# table), and third-party / in-house plugin direct reads. Name-granular by
# design: a plugin registers each var it reads, NOT a prefix, so a typo inside
# the plugin's own namespace still warns. A plugin that declares a real
# ``BaseSettings`` subclass needs no registration — it resolves field-level.
_EXTENSION_DIRECT_READ_VARS: set[str] = set()


def register_direct_read_env_vars(*names: str) -> None:
    """Register ``BALDUR_*`` vars read directly from ``os.environ`` (Channel 2).

    Call at import time from any module that direct-reads a ``BALDUR_*`` var
    whose name is not a plain source literal (computed names, pro/plugin vars),
    so the startup scan does not false-positive it as unknown. Names are
    upper-cased for case-insensitive membership, matching :func:`resolve_env_var`.
    Idempotent and additive.
    """
    _EXTENSION_DIRECT_READ_VARS.update(name.upper() for name in names)


def _known_direct_read_vars() -> frozenset[str]:
    """Return the union of the committed constant and runtime registrations."""
    return KNOWN_DIRECT_READ_ENV_VARS | _EXTENSION_DIRECT_READ_VARS


def is_known_env_var(var: str, index: dict[str, set[str]]) -> bool:
    """True iff ``var`` is consumable — a Pydantic field OR a direct-read var.

    Upper-cases ``var`` before BOTH the Pydantic resolve and the direct-read
    membership test, so a mixed-case var is matched as readily as a canonical
    upper-case one.
    """
    var_upper = var.upper()
    if resolve_env_var(var_upper, index):
        return True
    return var_upper in _known_direct_read_vars()


def known_env_var_names(index: dict[str, set[str]]) -> list[str]:
    """Every known ``BALDUR_*`` name, for ``difflib`` near-miss hints.

    Unions every ``PREFIX + FIELD`` Pydantic var name with the direct-read
    registry. Consumed by the startup scan to suggest the nearest known var when
    an unknown one is detected; near-miss is enrichment only, never a gate.
    """
    names = [
        prefix + field.upper() for prefix, fields in index.items() for field in fields
    ]
    names.extend(_known_direct_read_vars())
    return names
