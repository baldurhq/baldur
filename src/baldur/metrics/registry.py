"""
Metric Registration Helpers, Domain Registry, and Label Utilities.

Provides safe metric registration to avoid duplicate registration errors,
dynamic domain management for metric labeling,
label sanitization for Prometheus safety,
and batch metric recording for high-throughput paths.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Literal, cast

import structlog

logger = structlog.get_logger()

try:
    from prometheus_client import REGISTRY, Counter, Gauge, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    # Sentinel; helpers raise before touching it. Typed Any so the
    # try-branch import (CollectorRegistry) and the except-branch None
    # share a compatible annotation across conditional signatures.
    REGISTRY: Any = None  # type: ignore[no-redef]

PROMETHEUS_INSTALL_HINT = (
    "prometheus_client is required for metric registration but is not installed. "
    'Install with: pip install "baldur-framework[prometheus]" '
    "(quotes required in zsh/fish to prevent bracket glob expansion)."
)


# =============================================================================
# No-op metric (the prometheus extra is absent)
# =============================================================================


class NoOpMetric:
    """Null-object metric family used when the prometheus extra is absent.

    Absence of a metrics backend is a posture, not a per-call fault — the
    same stance OpenTelemetry's no-op MeterProvider and every statsd client
    take. Recording against this object does nothing and never raises, so
    the callers that record metrics need no absence branch of their own.

    Its surface is the closure of the protocols the real recorders expose,
    derived from those classes rather than listed here, because implicit
    special-method lookup goes to the *type* and bypasses ``__getattr__``:

    - attribute access returns ``self``, so a chain of any depth resolves;
    - ``__call__`` returns ``self``, because the dominant consumer shape
      resolves an attribute and then calls it;
    - the context-manager pair exists because one recorder method is a
      ``@contextmanager``, and a call-only stub passes right over that.

    ``__exit__`` returns False: a stub that swallowed exceptions would turn
    an absent optional dependency into a silent-failure factory. Dunder
    lookups still raise ``AttributeError`` so copy, pickle and inspect
    probes get honest answers.

    Peer at the labeled-child contract: ``metrics/safe_gauge/noop.py``
    ``NoOpGaugeChild``, which is reachable only through ``SafeGauge``.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> NoOpMetric:
        if name.startswith("__"):
            raise AttributeError(name)
        return self

    def __call__(self, *args: Any, **kwargs: Any) -> NoOpMetric:
        return self

    def __enter__(self) -> NoOpMetric:
        return self

    def __exit__(self, *exc_info: object) -> Literal[False]:
        # Literal, not bool: the type itself states that this stub can never
        # suppress an exception, which is the property that keeps an absent
        # optional dependency from becoming a silent-failure factory.
        return False


NOOP_METRIC = NoOpMetric()


def noop_metric_factory(*args: Any, **kwargs: Any) -> Any:
    """Stand in for any ``get_or_create_*`` helper when prometheus is absent.

    Absorbs all three helper signatures, including the histogram's optional
    ``buckets``. Module-scope metric definitions bind this instead of the
    raising helpers, which is what keeps those modules importable — and
    therefore keeps their consumers from failing once per call.

    Returns the shared :data:`NOOP_METRIC`, annotated ``Any`` because it
    substitutes for helpers declared to return collector types: a narrower
    annotation would make every substitution a type error.
    """
    return NOOP_METRIC


# =============================================================================
# Prometheus Label Sanitization
# =============================================================================

_LABEL_UNSAFE_PATTERN = re.compile(r"[^a-zA-Z0-9_]")
UNKNOWN_LABEL_VALUE = "unknown"
DEFAULT_LABEL_MAX_LENGTH = 128


def sanitize_label_value(value: str, max_length: int = DEFAULT_LABEL_MAX_LENGTH) -> str:
    """
    Normalize a Prometheus metric label value into a safe form.

    Characters other than alphanumerics/underscore are replaced with '_',
    the value is truncated to 128 characters, and an empty string yields
    'unknown'.

    Examples:
        >>> sanitize_label_value("my-service.v2")
        'my_service_v2'
        >>> sanitize_label_value("")
        'unknown'
    """
    if not value or not value.strip():
        return UNKNOWN_LABEL_VALUE
    sanitized = _LABEL_UNSAFE_PATTERN.sub("_", value.strip())
    return sanitized[:max_length]


# =============================================================================
# Metrics Batch Recorder (async batch for hot paths)
# =============================================================================


class MetricsBatchRecorder:
    """
    Record metrics on hot paths as an asynchronous batch.

    The calling thread only performs SimpleQueue.put() (lock-free, ~50ns).
    A background daemon thread flushes every 100ms, or once the batch reaches
    256 items. A failed flush drops that batch and logs a warning (fail-open).
    """

    __slots__ = (
        "_queue",
        "_batch_size",
        "_flush_interval",
        "_worker",
        "_running",
    )

    def __init__(
        self,
        batch_size: int = 256,
        flush_interval_ms: int = 100,
    ) -> None:
        self._queue: queue.SimpleQueue[tuple[Callable, tuple, dict]] = (
            queue.SimpleQueue()
        )
        self._batch_size = batch_size
        self._flush_interval = flush_interval_ms / 1000.0
        self._running = True
        self._worker = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="metrics-batch-recorder",
        )
        self._worker.start()

    def enqueue(
        self,
        metric_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """
        Enqueue a metric recording request — lock-free O(1).

        Called from hot paths. SimpleQueue.put() is lock-free, so it avoids
        contention on prometheus_client's internal lock.
        """
        if self._running:
            self._queue.put((metric_fn, args, kwargs))

    def _flush_loop(self) -> None:
        """Background thread: collect a batch, then record it in bulk."""
        while self._running:
            batch: list[tuple[Callable, tuple, dict]] = []
            deadline = time.monotonic() + self._flush_interval

            while len(batch) < self._batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=max(remaining, 0.001))
                    batch.append(item)
                except Exception:
                    break

            for metric_fn, args, kwargs in batch:
                try:
                    metric_fn(*args, **kwargs)
                except Exception as e:
                    logger.debug(
                        "metrics_batch_recorder.record_metric_failed",
                        error=e,
                    )

    def shutdown(self) -> None:
        """Graceful shutdown — flush the remaining batch."""
        self._running = False
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)


# =============================================================================
# Safe Metric Registration Helpers
# =============================================================================


def get_or_create_counter(name: str, description: str, labels: list[str]) -> Counter:
    """Get existing counter or create new one to avoid duplicate registration."""
    if not PROMETHEUS_AVAILABLE:
        raise ImportError(PROMETHEUS_INSTALL_HINT)
    if name in REGISTRY._names_to_collectors:
        # _names_to_collectors values are Collector (the base class) at the
        # stub level; here the caller guarantees the name maps to a Counter.
        return cast(Counter, REGISTRY._names_to_collectors[name])
    try:
        return Counter(name, description, labels)
    except ValueError:
        return cast(Counter, REGISTRY._names_to_collectors[name])


def get_or_create_gauge(name: str, description: str, labels: list[str]) -> Gauge:
    """Get existing gauge or create new one to avoid duplicate registration."""
    if not PROMETHEUS_AVAILABLE:
        raise ImportError(PROMETHEUS_INSTALL_HINT)
    if name in REGISTRY._names_to_collectors:
        return cast(Gauge, REGISTRY._names_to_collectors[name])
    try:
        return Gauge(name, description, labels)
    except ValueError:
        return cast(Gauge, REGISTRY._names_to_collectors[name])


def get_or_create_histogram(
    name: str,
    description: str,
    labels: list[str],
    buckets: tuple[float, ...] | None = None,
) -> Histogram:
    """Get existing histogram or create new one to avoid duplicate registration."""
    if not PROMETHEUS_AVAILABLE:
        raise ImportError(PROMETHEUS_INSTALL_HINT)
    if name in REGISTRY._names_to_collectors:
        return cast(Histogram, REGISTRY._names_to_collectors[name])
    try:
        if buckets:
            return Histogram(name, description, labels, buckets=buckets)
        return Histogram(name, description, labels)
    except ValueError:
        return cast(Histogram, REGISTRY._names_to_collectors[name])


# =============================================================================
# Exporter Liveness Marker
# =============================================================================

UP_GAUGE_NAME = "baldur_up"
_UP_GAUGE_DESCRIPTION = (
    "Baldur exporter liveness marker (always 1 while the process exports metrics)"
)


def ensure_up_gauge() -> None:
    """Register the exporter liveness marker and pin it at 1.

    Follows the standard per-exporter ``*_up`` primitive (``mysql_up``,
    ``redis_up``, ``pg_up``): a label-less gauge whose *presence* — never its
    value — marks a scrape target as a Baldur exporter. The bundled
    scrape-liveness alert rules join on it, which is what makes them
    framework-agnostic: they need no knowledge of the scrape job's name.

    Fail-open on any exception, not only on ``prometheus_client`` absence.
    ``get_or_create_gauge`` returns whatever collector already owns the name
    without a type check, so a foreign collector registered by unrelated code
    collapses the call in more than one way: a ``Gauge`` carrying labels raises
    ``ValueError`` from ``.set()`` on an unlabelled handle, and a ``Counter``
    raises ``AttributeError`` for having no ``.set`` at all. Narrowing the catch
    would leave one of those uncaught. A liveness marker is a pure observability
    side-effect (fail-open), and this runs at module import, where a propagated
    raise would break every importer of the metrics registry.

    Returns immediately when the prometheus extra is absent. That is not a
    registration fault to warn about — it is the zero-config posture, and
    this runs at *import* time, before anything has configured logging, so a
    demoted line would still print. The install hint survives on the
    ``get_or_create_*`` raise and in the startup posture line.

    The WARNING is the only diagnostic a name collision produces, so its event
    name is a fixed literal and carries the exception text.
    """
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        get_or_create_gauge(UP_GAUGE_NAME, _UP_GAUGE_DESCRIPTION, []).set(1)
    except Exception as exc:
        logger.warning(
            "metrics.up_gauge_registration_failed",
            metric=UP_GAUGE_NAME,
            error=str(exc),
        )


# Registered once, at module scope: every metric-emitting path imports this
# module by construction (all recorders pull get_or_create_* from here), so the
# marker is exported whichever metrics backend the observability profile picks
# — including the OTel backend, which never constructs BaldurMetrics.
ensure_up_gauge()


# =============================================================================
# Domain Registry (Dynamic Domain Registration)
# =============================================================================

_MAX_REGISTERED_DOMAINS = 50

# How long a resolved cardinality cap stays memoized. Registration now runs on
# the request path, and a high-cardinality name (``protect(f"order_{id}")``)
# misses the registry, the admission gate and the refusal memo on every call —
# without this cache each such call would pay a full layered settings read
# forever after the cap fills. Sized as the console-edit propagation delay for
# a cardinality ceiling, which is the only thing the layered read buys here.
# A fixed module constant rather than a settings field: a settings field for it
# would itself need a settings read.
_CAP_CACHE_TTL_SECONDS = 5.0

# Fallback domain for unregistered domains — declared before _registered_domains
# so it can be included in the initial set. Single source of truth lives in
# ``utils/domain_validation`` (545 D1) so the metric label registry shares the
# same fallback string as DLQ/decorator rejection paths.
from baldur.core.exceptions import DomainValidationError
from baldur.utils.domain_validation import FALLBACK_DOMAIN as _FALLBACK_DOMAIN
from baldur.utils.domain_validation import validate_and_normalize_domain

# Default domains (domain-neutral fallbacks)
DEFAULT_DOMAINS: list[str] = [
    "external_service",
    "internal_process",
    "async_task",
    "notification",
    "data_sync",
]

# NOTE: Per-process registry. In multiprocess deployments (Gunicorn prefork,
# Celery workers), each process maintains its own copy. Registration is
# traffic-driven — a domain enters the set the first time a declaration site in
# THAT process is reached — so worker sets diverge by served traffic well below
# the cap. Label correctness is unaffected (every declaration site registers
# before it records, within the same call), but the periodic per-domain gauge
# inventory does diverge across workers.
# TSDB cardinality is bounded by max_registered_domains, not multiplied by
# worker count.
_registered_domains: set[str] = {
    "external_service",
    "internal_process",
    "async_task",
    "notification",
    "data_sync",
    _FALLBACK_DOMAIN,  # resolve idempotency — prevents spurious DEBUG logs
}

# Distinct unregistered domains whose DEBUG notice has already been emitted.
# Bounded (caller-controlled keys) and independent of _MAX_REGISTERED_DOMAINS:
# this caps log noise, not label cardinality, which resolve_domain_label
# already collapses to the fallback.
_MAX_UNREGISTERED_LOGGED_DOMAINS = 256
_unregistered_seen: OrderedDict[str, None] = OrderedDict()
_unregistered_seen_lock = threading.Lock()

# Guards the registration miss path (membership re-check, cap read, add) and
# every piece of registry-owned memo state. The membership fast path in
# ``register_domain`` and the lookup in ``resolve_domain_label`` stay lock-free.
_registry_lock = threading.Lock()

# Domains that must never occupy a registry slot, in canonical form. Shared
# with the DLQ store's canonicalization retry so both channels skip-list the
# same values:
# ``unknown`` is what an empty/blank input sanitizes to AND the Celery
# unmatched-task fallback (registering it would merge unrelated traffic into
# one series), ``other_domain`` is the collapse bucket itself, and ``default``
# is ``RetryPolicyConfig.domain``'s field default — an absence of declaration,
# not a declaration.
NON_REGISTRABLE_DOMAIN_LABELS: frozenset[str] = frozenset(
    {UNKNOWN_LABEL_VALUE, "other_domain", "default"}
)

# Admission-gate refusals already reported, bounded exactly like
# ``_unregistered_seen``. Cap refusals deliberately do NOT enter this memo — a
# flood of unique at-cap names would thrash the LRU and re-warn forever; those
# are reported once per cap epoch instead (``_cap_epoch_warned``).
_MAX_REFUSED_LOGGED_DOMAINS = 256
_refused_seen: OrderedDict[str, None] = OrderedDict()

# Canonical names whose projection has already been EVALUATED for lossiness
# (warned or not — see _note_projection_lossiness). Its own lock, because the
# check runs on the lock-free membership hit as well as under _registry_lock.
_lossy_projection_seen: OrderedDict[str, None] = OrderedDict()
_lossy_projection_lock = threading.Lock()

# True once the registry has reported that it is full. Cleared by a successful
# add (i.e. the cap was raised) and by reset_registered_domains(), so the
# operator gets one line per time the registry fills, not one per rejected name.
_cap_epoch_warned = False

# Memoized cardinality cap + its monotonic expiry (see _CAP_CACHE_TTL_SECONDS).
_cap_cache_value: int | None = None
_cap_cache_expires_at: float = 0.0


def canonicalize_domain_label(domain: object) -> str:
    """Project any domain input onto its canonical Prometheus label form.

    The tree carries two domain vocabularies — the validated form
    (``utils.domain_validation``: lowercase, segmented identifier, <= 64 chars)
    and the label form (``sanitize_label_value``: underscore substitution,
    truncation) — and this is the single stated projection between them. Both
    ends of the registry go through it, which is what makes
    ``resolve(x)`` non-fallback iff ``canonicalize(x)`` is registered, and what
    keeps two spellings of one logical domain on one label value.

    Total on **any** input by construction: a non-``str`` yields
    ``UNKNOWN_LABEL_VALUE`` rather than raising. That is load-bearing, not
    defensive — ``sanitize_label_value`` short-circuits on falsy input before
    touching ``.strip()``, so ``None`` already resolves to a label today, and
    the DLQ store path calls this from inside an exception handler where a
    raise would drop the record entirely.

    Examples:
        >>> canonicalize_domain_label("  Payment-API ")
        'payment_api'
        >>> canonicalize_domain_label(None)
        'unknown'
    """
    if not isinstance(domain, str):
        return UNKNOWN_LABEL_VALUE
    return sanitize_label_value(domain.strip().lower())


def _get_max_domains_from_settings() -> int:
    """Read max_registered_domains from MetricsSettings, fallback to module constant.

    Fail-open is the decided direction, not an accident: fail-closed would let
    one unrelated invalid ``BALDUR_METRICS_*`` field re-collapse every
    application domain to the fallback label, while fail-open's worst case is a
    bounded cap of 50 (inside the field's own ``ge=10, le=500``) plus a WARNING.
    """
    try:
        from baldur.settings.layered_provider import get_layered_settings
        from baldur.settings.metrics import MetricsSettings

        # Layered read so a console edit of the metrics domain takes effect
        # (686 D1/D5); env base when no RuntimeConfigManager is registered.
        return get_layered_settings(MetricsSettings, "metrics").max_registered_domains
    except Exception as exc:
        logger.warning(
            "metrics.settings_load_failed",
            fallback=_MAX_REGISTERED_DOMAINS,
            error=str(exc),
        )
        return _MAX_REGISTERED_DOMAINS


def _resolve_max_domains_cached() -> int:
    """Return the cardinality cap, memoized for ``_CAP_CACHE_TTL_SECONDS``.

    Deliberately unsynchronized. The registry's own callers hold
    ``_registry_lock``, but the call-outcome window's cap resolve does not — it
    runs outside its own lock so a settings read can never reach a protected
    call. Concurrent refreshes of an expired entry therefore cost a duplicate
    settings read and nothing more: both writers store an equally valid cap, and
    the assignments are individually atomic. A lock here would buy only the
    duplicate read, at the price of putting the metrics-read path behind the
    registry's own contention.

    The fail-open fallback is cached on the same terms as a configured value:
    a persistently invalid settings field must not reinstate the per-call
    layered read this cache exists to remove. The priced consequence is that
    recovery from a *transient* settings failure lags by up to one TTL.
    """
    global _cap_cache_value, _cap_cache_expires_at

    now = time.monotonic()
    if _cap_cache_value is not None and now < _cap_cache_expires_at:
        return _cap_cache_value
    resolved = _get_max_domains_from_settings()
    _cap_cache_value = resolved
    _cap_cache_expires_at = now + _CAP_CACHE_TTL_SECONDS
    return resolved


def _admission_refusal_reason(canonical: str) -> str | None:
    """Return why ``canonical`` may not occupy a registry slot, or None.

    Pure — no settings read, no lock. Validation runs on the **canonical** form
    rather than the raw input so the length/shape measurement is identical to
    the one the DLQ channel applies through its own canonicalization retry;
    a raw-vs-canonical disagreement is therefore not expressible.
    """
    if canonical in NON_REGISTRABLE_DOMAIN_LABELS:
        return "non_registrable"
    try:
        validate_and_normalize_domain(canonical)
    except DomainValidationError as exc:
        return str(getattr(exc.reason, "value", exc.reason))
    return None


def _memoize_once(memo: OrderedDict[str, None], key: str, limit: int) -> bool:
    """Return True the first time ``key`` is offered to ``memo``, else False.

    **Saturating**, not LRU: once ``limit`` distinct keys have been recorded,
    every further key returns False. The keys are caller-controlled domain
    strings, so an unbounded memo would be a leak — but an LRU is the wrong
    bound for a WARNING gate on a request path: a rotation of more than
    ``limit`` distinct names evicts and re-admits forever, which is one
    WARNING per call, exactly the flood the cap-epoch flag exists to avoid on
    the other refusal path. Saturating caps the total at ``limit`` lines per
    ``reset_registered_domains()`` cycle.

    Callers hold ``_registry_lock``.
    """
    if key in memo:
        return False
    if len(memo) >= limit:
        return False
    memo[key] = None
    return True


def _note_projection_lossiness(domain: object, canonical: str) -> None:
    """Announce a domain whose stored form and label form cannot agree.

    Every domain-labeled *series* agrees after canonicalization, but a consumer
    that joins a **stored** domain key against the registered set misses
    whenever the validated form survives a character the label form rewrites.
    Today exactly one character does that (the dot), but the predicate is
    written as "validated form != canonical form" rather than "contains a dot"
    so it stays correct if the validation pattern ever admits another
    label-unsafe character.

    Runs on the membership hit as well as on a fresh admission, because the
    divergence is a property of the *spelling*, not of who won the race to
    register it: ``data.sync`` resolves against a shipped default that is
    already in the set, so an admission-only check would stay silent on the
    one vocabulary the framework itself ships. The memo therefore records
    every canonical form it has **evaluated**, not only the ones it warned
    about — otherwise an agreeing name would re-run the validator on every
    steady-state call, which is the per-call cost the fast path exists to
    remove. Known residue: a second, differently-spelled raw input for an
    already-evaluated canonical form is not re-checked.

    Without this line the symptom is a silent zero on the pending gauge, the
    console panel and the drift report — the worst failure mode. Takes its own
    lock, so it is callable from inside or outside ``_registry_lock``.
    """
    if canonical in _lossy_projection_seen:
        return
    try:
        validated = validate_and_normalize_domain(domain)
    except DomainValidationError:
        # The validated channel rejects it outright, so the DLQ store path
        # falls back to this same canonical form — the two agree.
        validated = canonical
    with _lossy_projection_lock:
        if not _memoize_once(
            _lossy_projection_seen, canonical, _MAX_REFUSED_LOGGED_DOMAINS
        ):
            return
    if validated != canonical:
        logger.warning(
            "metrics.domain_label_projection_lossy",
            domain=validated,
            label=canonical,
        )


def register_domain(domain: object, *, max_domains: int | None = None) -> bool:
    """
    Register a domain so its metric label survives instead of collapsing.

    Called from the surfaces where application code *declares* a domain
    (``protect()``'s retry stage, ``@domain_tag``, the DLQ store entry point,
    the Celery domain-consuming sites), never from runtime data reaching a
    recorder — auto-admitting recorder input would let an external client squat
    the cap.

    Total: never raises. Sites call it bare, without a local try/except, which
    matters because at least one of them runs inside an ``except`` block ahead
    of a re-raise, where a propagating registry error would mask the business
    exception.

    Args:
        domain: Domain name. Registered under its canonical label form
            (``canonicalize_domain_label``); a non-``str`` is refused.
        max_domains: Maximum number of registered domains.
            If None, reads from MetricsSettings.max_registered_domains
            (memoized for a few seconds).
            Falls back to _MAX_REGISTERED_DOMAINS (50) if settings unavailable.

    Returns:
        True if the canonical form is registered (including "already
        registered"), False if it was refused by the admission gate or the cap.
    """
    global _cap_epoch_warned

    try:
        canonical = canonicalize_domain_label(domain)

        # Fast path: lock-free, no settings read. This is the steady state for
        # every declaration site after its first call. The lossiness note runs
        # here too — after its own memo hit it is a single dict lookup, and a
        # spelling whose canonical form is already registered (``data.sync``
        # against the shipped ``data_sync`` default) diverges exactly as much
        # as one that had to be admitted.
        if canonical in _registered_domains:
            _note_projection_lossiness(domain, canonical)
            return True

        refusal_reason = _admission_refusal_reason(canonical)
        if refusal_reason is not None:
            # Lock-free memo probe so a repeating refused name pays nothing.
            if canonical not in _refused_seen:
                with _registry_lock:
                    if _memoize_once(
                        _refused_seen, canonical, _MAX_REFUSED_LOGGED_DOMAINS
                    ):
                        logger.warning(
                            "metrics.domain_registration_refused",
                            domain=canonical,
                            reason=refusal_reason,
                        )
            return False

        with _registry_lock:
            # Double-checked: the fast path above ran outside the lock, so a
            # concurrent registrant of the SAME name may have just added it.
            # Without this re-check, that second thread measures a set the
            # first just filled and — at the exact cap boundary — refuses a
            # domain that is now registered, opening a cap epoch that names it.
            if canonical in _registered_domains:
                return True

            cap = _resolve_max_domains_cached() if max_domains is None else max_domains
            if len(_registered_domains) >= cap:
                if not _cap_epoch_warned:
                    _cap_epoch_warned = True
                    logger.warning(
                        "metrics.domain_registration_limit_reached",
                        domain=canonical,
                        max_domains=cap,
                        current_count=len(_registered_domains),
                    )
                return False

            _registered_domains.add(canonical)
            _cap_epoch_warned = False
            logger.debug(
                "metrics.domain_registered",
                domain=canonical,
            )

        # Outside the registry lock on purpose: the lossiness note takes its
        # own lock, and nesting two locks for a diagnostic buys nothing.
        _note_projection_lossiness(domain, canonical)
        return True
    except Exception as exc:
        logger.warning(
            "metrics.domain_registration_failed",
            error=str(exc),
        )
        return False


def _should_log_unregistered(canonical: str) -> bool:
    """Return True the first time ``canonical`` is seen, False afterwards.

    The unregistered-domain notice is worth one line per domain, not one per
    call: a domain that no declaration site registers — a runtime-only string
    reaching a recorder, or one refused by the cap — is unregistered on *every*
    recording call. Structlog is wired with a non-filtering ``BoundLogger``, so
    a ``debug()`` pays its full processor chain and its global lock regardless
    of the configured level, and this resolver runs once per retry attempt
    rather than once per resolution.

    The seen-set is bounded because the domain string is caller-controlled:
    an unbounded memo would be a leak. LRU shape mirrors the endpoint
    normalizer's in this same package.
    """
    with _unregistered_seen_lock:
        if canonical in _unregistered_seen:
            _unregistered_seen.move_to_end(canonical)
            return False
        if len(_unregistered_seen) >= _MAX_UNREGISTERED_LOGGED_DOMAINS:
            _unregistered_seen.popitem(last=False)
        _unregistered_seen[canonical] = None
        return True


def resolve_domain_label(domain: object) -> str:
    """
    Safely resolve a domain label for metric recording (enforcement).

    Returns the canonical label form if it is registered, otherwise forces
    OTHER_DOMAIN. This is what makes register_domain()'s cap enforced rather
    than advisory.

    Total on any input, including non-``str`` (see
    ``canonicalize_domain_label``). The unregistered-domain DEBUG line is
    emitted once per distinct domain (see ``_should_log_unregistered``); the
    resolved value is unaffected.

    Args:
        domain: Domain name

    Returns:
        Canonical domain label if registered, "OTHER_DOMAIN" otherwise
    """
    canonical = canonicalize_domain_label(domain)
    # Idempotent echo: resolving the fallback label returns it without a
    # membership lookup or a memo lock. The registered set holds the fallback
    # in its UPPERCASE spelling for gauge inventory only, which no canonical
    # form can ever equal.
    if canonical == _FALLBACK_DOMAIN.lower():
        return _FALLBACK_DOMAIN
    if canonical in _registered_domains:
        return canonical
    if _should_log_unregistered(canonical):
        logger.debug(
            "metrics.domain_label_unregistered",
            domain=canonical,
            resolved_to=_FALLBACK_DOMAIN,
        )
    return _FALLBACK_DOMAIN


_DEFAULT_DOMAINS: frozenset[str] = frozenset(
    {
        "external_service",
        "internal_process",
        "async_task",
        "notification",
        "data_sync",
        _FALLBACK_DOMAIN,
    }
)


def reset_registered_domains() -> None:
    """Reset registered domains to defaults for test isolation.

    Uses clear() + update() instead of reassignment to preserve the set
    object identity — test fixtures may hold direct references to it.

    Every memo is cleared here rather than through separate reset entry
    points: a test that re-registers a domain and expects its first-resolve
    notice again must not depend on remembering to reset several things.

    Taking the registry lock for the set, the refusal memo, the cap epoch flag
    and the cap cache together closes the reset-vs-registration race — a
    concurrent registrant either completes before the reset or observes a
    fully-reset registry. The two diagnostic memos hold their own locks and are
    cleared sequentially, never nested, so no inverse lock order exists.
    """
    global _cap_epoch_warned, _cap_cache_value, _cap_cache_expires_at

    with _registry_lock:
        _registered_domains.clear()
        _registered_domains.update(_DEFAULT_DOMAINS)
        _refused_seen.clear()
        _cap_epoch_warned = False
        _cap_cache_value = None
        _cap_cache_expires_at = 0.0
    with _lossy_projection_lock:
        _lossy_projection_seen.clear()
    with _unregistered_seen_lock:
        _unregistered_seen.clear()


def get_registered_domains() -> list[str]:
    """Get all registered domains, including defaults.

    The uppercase fallback label is part of the returned inventory by design:
    the periodic per-domain gauge updaters enumerate this list, so dropping it
    would freeze the collapse bucket's own gauge refresh.
    """
    all_domains = _registered_domains | set(DEFAULT_DOMAINS)
    return sorted(all_domains)
