"""G49 — every registered framework metric series MUST be ``baldur_``-prefixed.

The framework migrated to a ``baldur_``-prefixed per-domain recorder layer
(``metrics/recorders/*``, ``PREFIX="baldur"``), but the older bare-name
registrations in ``services/metrics/definitions.py`` lingered as dead duplicates
shadowed by their prefixed twins — a consumer could name a
registered-but-never-populated bare series (``circuit_breaker_state``,
``dlq_pending_count``, ...) and silently get nothing (impl doc 655 D1 deleted
that class). G43/G48 guard the *consumer* surface (a dashboard/alert names a live
series) but neither prevents a *producer*-side re-registration of a dead bare
duplicate — G43 explicitly documents that a "registered-but-dead name would
pass: population is a runtime property a static gate cannot assert".

This guard closes that hole on the *producer* surface. The literal invariant
"every registered series has a live producer" is statically infeasible
(population is runtime), so the enforceable proxy is **prefix coherence**: every
framework series is ``baldur_``-prefixed except a small, explicitly-allowlisted
set of surviving legacy *live* bare families. A re-introduced bare duplicate (or
a new producer that forgets the prefix) is neither ``baldur_``-prefixed nor
allowlisted, so it turns the build red.

**Why a subprocess snapshot (NOT G43/G48's in-process fixture).** G43/G48 ask a
*subset* question ("are the names my dashboard/alert references present?"), so an
in-process snapshot of the shared global ``REGISTRY`` is fine — extra names other
tests registered are harmless. G49 asks the *inverse* ("is any UNEXPECTED bare
name present?"), which the shared registry breaks: by the time an in-process
fixture runs, the xdist worker's session has accumulated bare metrics that test
modules register to the global default registry (e.g. ``test_throttle_metrics``
registers bare ``throttle_rtt_ms`` / ``throttle_gradient``). Those are
test-only pollution, not framework producers, but an in-process snapshot can't
tell them apart. So G49 takes its snapshot in a **fresh subprocess** that imports
only the controlled registration set — the same isolation pattern
``test_registry_no_prometheus`` uses — making the verdict deterministic and
independent of whatever else the session registered.

**Controlled registration set** — the subprocess imports
``services.metrics.definitions`` + ``...recorders`` (the module-level series),
instantiates ``BaldurMetrics()`` directly (the per-domain recorder series,
profile-independent), and imports ``metrics.audit_buffer_metrics`` (the one wired
live module that still uses bare names) so the full documented bare surface is
exercised against the allowlist.

**Scope boundary** — name *coherence* (namespace), NOT population (identical to
G43/G48). The allowlist is the documented migration debt; migrating those live
bare names to a ``baldur_`` prefix is a breaking rename of populated metrics,
parked as a follow-up.

Rule registry:
``ARCHITECTURE.md#g49-metric-namespace-coherence``
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

# prometheus_client backs the registry snapshot; the OSS monorepo installs it,
# but skip cleanly on a stripped checkout rather than erroring at import.
pytest.importorskip("prometheus_client")

# Framework-owned series all share this prefix (the recorder-layer convention).
_FRAMEWORK_PREFIX = "baldur_"

# prometheus_client's own default collectors — runtime-foreign, not framework
# series, so excluded from the coherence check.
_DEFAULT_PREFIXES = ("python_", "process_")

# Surviving legacy live bare-name families (impl doc 655 D1/D6) — populated
# series that predate the ``baldur_`` prefix migration. Listed by FAMILY base
# name and matched as ``name == base + suffix`` for the EXACT closed set of
# synthetic per-sample suffixes prometheus appends (see ``_SYNTHETIC_SUFFIXES``)
# so a re-introduced dead bare DUPLICATE (655 D1 removed) — or an unrelated new
# bare family that merely extends an allowlisted base across an underscore
# boundary (e.g. ``dlq_outbox_drops_by_reason``) — is NOT here and turns the
# build red. This list is the documented migration debt.
_LEGACY_BARE_FAMILIES = (
    # core/retry_hooks.py — histogram whose metric name carries a _total suffix
    "retry_attempts_total",
    # services/dlq_outbox/outbox.py
    "dlq_outbox_drops",
    "dlq_outbox_current_size",
    "dlq_outbox_processing_delay_seconds",
    "dlq_outbox_worker_dead_coercions",
    # metrics/audit_buffer_metrics.py — the wired audit-buffer observability
    "audit_buffer_size",
    "audit_buffer_backpressure",
    "audit_buffer_dropped",
    "audit_buffer_batch_writes",
    "audit_buffer_batch_errors",
    "audit_buffer_flush",
    "audit_buffer_orphan_recovery",
    "audit_buffer_safety_ltrim",
    "audit_buffer_fallback_size",
)

# Subprocess that registers ONLY the controlled set and dumps the live series
# names as one JSON line prefixed with a sentinel (so structlog/stderr noise and
# any incidental stdout cannot be mistaken for the payload).
_SNAPSHOT_SNIPPET = textwrap.dedent(
    """
    import json
    from prometheus_client import REGISTRY
    import baldur.services.metrics.definitions  # noqa: F401
    import baldur.services.metrics.recorders  # noqa: F401
    import baldur.metrics.audit_buffer_metrics  # noqa: F401
    from baldur.metrics.prometheus import BaldurMetrics

    BaldurMetrics()
    names = sorted(REGISTRY._names_to_collectors.keys())
    print("NAMES_JSON:" + json.dumps(names))
    """
)

_SENTINEL = "NAMES_JSON:"


# The closed set of per-sample suffixes prometheus_client appends to a family
# base in ``_names_to_collectors`` (verified empirically across every entry in
# ``_LEGACY_BARE_FAMILIES``): ``""`` (the base key itself), counter ``_total`` /
# ``_created``, histogram ``_bucket`` / ``_count`` / ``_sum`` / ``_created``.
# Matching the EXACT suffix — not a loose ``startswith(base + "_")`` prefix — is
# what stops an unrelated new bare family that merely extends an allowlisted base
# (e.g. ``dlq_outbox_drops_by_reason``, ``audit_buffer_size_p99``) from slipping
# through the coherence gate.
_SYNTHETIC_SUFFIXES = ("", "_total", "_created", "_bucket", "_count", "_sum")


def _is_coherent(name: str) -> bool:
    """True if a series name is framework-owned, a prometheus_client default, or
    an allowlisted surviving legacy bare family (incl. its synthetic suffixes)."""
    if name.startswith(_FRAMEWORK_PREFIX):
        return True
    if name.startswith(_DEFAULT_PREFIXES):
        return True
    return any(
        name == fam + suffix
        for fam in _LEGACY_BARE_FAMILIES
        for suffix in _SYNTHETIC_SUFFIXES
    )


def _violations(names: frozenset[str] | set[str]) -> set[str]:
    """Series names that are neither ``baldur_``-prefixed nor allowlisted."""
    return {n for n in names if not _is_coherent(n)}


@pytest.fixture(scope="module")
def registered_metric_names() -> frozenset[str]:
    """Snapshot the live registry from a fresh subprocess (see module docstring).

    Isolation is the point: an in-process snapshot of the shared global registry
    would see bare metrics other test modules registered, which this inverse
    check would false-flag. The subprocess registers only the controlled set.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT_SNIPPET],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"snapshot subprocess failed (rc={result.returncode}); stderr={result.stderr}"
    )
    payload = next(
        (
            line[len(_SENTINEL) :]
            for line in result.stdout.splitlines()
            if line.startswith(_SENTINEL)
        ),
        None,
    )
    assert payload is not None, f"no {_SENTINEL} line in stdout: {result.stdout!r}"
    return frozenset(json.loads(payload))


def test_registry_snapshot_is_nonempty(
    registered_metric_names: frozenset[str],
) -> None:
    """Sentinel: the snapshot is populated — an empty set would vacuously pass."""
    assert registered_metric_names, "registry snapshot is empty"


def test_legacy_bare_families_are_actually_registered(
    registered_metric_names: frozenset[str],
) -> None:
    """The controlled snapshot really registers the allowlisted bare families.

    Guards against the allowlist silently rotting into dead entries: if a family
    is renamed/removed in production, its base name vanishes here and the gate's
    tolerance of it becomes meaningless. Match by the same base-name rule.
    """
    for fam in _LEGACY_BARE_FAMILIES:
        assert any(
            n == fam or n.startswith(f"{fam}_") for n in registered_metric_names
        ), f"allowlisted legacy family {fam!r} no longer registered — prune it"


def test_every_series_is_baldur_prefixed_or_allowlisted(
    registered_metric_names: frozenset[str],
) -> None:
    """G49: no registered series escapes the ``baldur_`` namespace except the
    documented legacy bare families."""
    bad = _violations(registered_metric_names)
    assert not bad, (
        f"non-baldur_ metric series registered outside the legacy allowlist: "
        f"{sorted(bad)}. A re-introduced dead bare duplicate (the 655 D1 class) "
        f"or a new producer that forgot the prefix must register a "
        f"baldur_-prefixed series; a genuinely new live bare family must be "
        f"added to _LEGACY_BARE_FAMILIES with a migration rationale."
    )


def test_guard_rejects_a_bare_duplicate(
    registered_metric_names: frozenset[str],
) -> None:
    """Guard-of-the-guard: a re-introduced bare duplicate turns the checker red.

    Simulates re-registering the bare ``circuit_breaker_state`` gauge — the exact
    dead duplicate 655 D1 deleted — by injecting it into the evaluated name set,
    and asserts the classifier flags exactly it, proving the positive test is not
    vacuously green.
    """
    bare_duplicate = "circuit_breaker_state"
    assert _is_coherent(bare_duplicate) is False
    poisoned = set(registered_metric_names) | {bare_duplicate}
    assert _violations(poisoned) == {bare_duplicate}


def test_allowlisted_families_and_synthetic_suffixes_pass() -> None:
    """Each legacy family base AND every synthetic per-sample suffix passes — else
    a counter family (base + ``_total`` / ``_created``) or a histogram family
    (base + ``_bucket`` / ``_count`` / ``_sum``) would false-flag."""
    for fam in _LEGACY_BARE_FAMILIES:
        for suffix in _SYNTHETIC_SUFFIXES:
            assert _is_coherent(fam + suffix), f"{fam}{suffix}"
    # A prefix collision must NOT leak: an unrelated bare name sharing only a
    # textual prefix (no underscore boundary) is still a violation.
    assert _is_coherent("audit_buffer_sizeable") is False
    # Nor may a NEW bare family that extends an allowlisted base across an
    # underscore boundary with a non-synthetic token slip through — the exact-
    # suffix match (not a loose prefix match) is what closes this hole.
    assert _is_coherent("dlq_outbox_drops_by_reason") is False
    assert _is_coherent("audit_buffer_size_p99") is False
    assert _is_coherent("retry_attempts_total_legacy") is False
    # Framework-prefixed and prometheus defaults always pass.
    assert _is_coherent("baldur_anything_total")
    assert _is_coherent("python_gc_objects_collected_total")
