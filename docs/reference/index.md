# Baldur v1.0 Public API Surface

Authoritative reference for the public API. Every name listed below is
covered by SemVer compatibility guarantees in v1.x. Everything not listed is
internal — subject to change without notice.

---

## Public Packages

Each sub-package declares its own `Status: Public` / `Status: Internal`
marker in its `__init__.py`. The classification below is the authoritative
source.

| Package | Status | Notes |
|---------|--------|-------|
| `baldur` (top-level) | Public | Marquee API — see Canonical Import Paths |
| `baldur.cli` | Public | `baldur` CLI entry-point |
| `baldur.decorators` | Public | `@dlq_protect`, `@idempotent`, `@rate_limit`, `@domain_tag` |
| `baldur.interfaces` | Public (adapter-author advanced) | Repository / Cache / Queue ABCs |
| `baldur.services` | Public | Re-export facade for `get_circuit_breaker_service`, `ReplayService`, etc. |
| `baldur.adapters.{django,fastapi,flask,sql,gunicorn}` | Public | Framework integrations |
| `baldur.adapters.{base,memory,cache}` | Public (adapter-author advanced) | Adapter-building primitives |
| `baldur.api.admin` | Public | Framework-free admin HTTP endpoints |
| `baldur.core.exceptions` | Public | Exception hierarchy (nested-only classes here) |
| `baldur.factory.registry` | Public | `ProviderRegistry` |
| `baldur.services.replay_service` | Public | `ReplayService` |
| `baldur.resilience.policies` | Internal | Composer is private; `protect()` is the public surface |

All other `baldur.*` sub-packages (`audit`, `audit/*`, `bridges`, `celery_tasks`,
`context`, `coordination`, `core`, `factory`, `meta`, `metrics`, `models`,
`observability`, `resilience`, `scaling`, `settings`, `tasks`,
`utils`) are **Internal**.

### PRO Addendum

Classification for the `baldur_pro.*` distribution. The 7 launch-set service
sub-packages below carry `Status: Public` and are covered by v1.x SemVer
guarantees. PRO subscribers consume PRO either by nested-path imports into
these Public sub-packages or by resolving singletons via the OSS-Public
`ProviderRegistry.<slot>.safe_get()` API.

| Package | Status | Notes |
|---------|--------|-------|
| `baldur_pro` (top-level) | Internal | Entry-point addon (`register_pro_services` bootstrap hook only) — no marquee re-export layer |
| `baldur_pro.services` | Internal | Empty re-export shell (`__all__ = []`) — no Public contract |
| `baldur_pro.services.dlq` | **Public** | `DLQService`, `store_to_dlq`, DLQ models — user-facing types |
| `baldur_pro.services.replay` | **Public** | `ReplayQueueService`, `BackpressureStatus`, `RateLimitStatus` |
| `baldur_pro.services.emergency_mode` | **Public** | `EmergencyLevel`, `get_emergency_manager`, `is_emergency_active` |
| `baldur_pro.services.bulkhead` | **Public** | `BulkheadPolicy`, `SemaphoreBulkhead`, `@bulkhead` decorator |
| `baldur_pro.services.throttle` | **Public** | `ThrottleConfig`, `AdaptiveThrottle`, `ThrottlePolicy` |
| `baldur_pro.services.canary` | **Public** | `CanaryRolloutService`, `CanaryFeatureFlag`, `CanarySafetyInterlock` |
| `baldur_pro.services.unified_notification` | **Public** | `NotificationPayload`, `UnifiedNotificationManager`, `notify` family |
| `baldur_pro.services.audit` | Internal | Registry-fronted internal log emitters |
| `baldur_pro.services.governance` | Internal | Registry-fronted via `ProviderRegistry.governance` (OSS-Public). Subscriber access is `ProviderRegistry.governance.get()`, not direct imports |

Any `baldur_pro.services.*` sub-package **not** listed as `Status: Public`
above stays `Status: Internal` and is **not** SemVer-covered in v1.x — see
§ PRO Internal Sub-Package Surface below.

---

## Canonical Import Paths

### Common Imports

```python
# Top-level marquee API
from baldur import init, protect, aprotect, protected, aprotected
from baldur import ProviderRegistry, get_circuit_breaker_service
from baldur import CircuitState, FailedOperationData, ReplayService
from baldur import sql_transaction, get_leader_scheduler
from baldur import start_admin_server, stop_admin_server
from baldur import fastapi_lifespan, init_flask

# Exceptions (top-level subset — base classes + leaves raised by top-level API)
from baldur import (
    BaldurError, AdapterError, AdapterNotFoundError,
    CircuitBreakerError, DLQError, DLQReplayError,
    ResilienceError, RetryExhaustedError, TimeoutPolicyError,
    RateLimitExceeded, IdempotencyDuplicateError,
    DomainValidationError, ConfigurationError,
)

# Decorators (Public sub-package)
from baldur.decorators import dlq_protect, idempotent, rate_limit, domain_tag

# Advanced protect surface (nested — Public sub-package)
from baldur.protect_facade import protect_with_meta, aprotect_with_meta, ProtectResult

# Internal-only exceptions (rarely raised from public surfaces)
from baldur.core.exceptions import (
    AdapterInitializationError, AdapterConnectionError, StoreError,
    AuditError, RunbookError, SettingsValidationError, ...,
)
```

### Top-level `__all__` (30 names)

`__version__`, `init`, `protect`, `aprotect`, `protected`, `aprotected`,
`get_leader_scheduler`, `sql_transaction`, `start_admin_server`,
`stop_admin_server`, `fastapi_lifespan`, `init_flask`, `CircuitState`,
`FailedOperationData`, `ProviderRegistry`, `get_circuit_breaker_service`,
`ReplayService`, `BaldurError`, `AdapterError`, `AdapterNotFoundError`,
`CircuitBreakerError`, `DLQError`, `DLQReplayError`, `ResilienceError`,
`RetryExhaustedError`, `TimeoutPolicyError`, `RateLimitExceeded`,
`IdempotencyDuplicateError`, `DomainValidationError`, `ConfigurationError`.

Both pre-import attribute access (`baldur.X`) and wildcard (`from baldur
import *`) resolve to exactly these names.

---

## Framework Setup Entry-Points

Each framework keeps its idiomatic entry-point shape — Baldur does **not**
impose a uniform `init_*` convention:

| Framework | Entry point | Idiom |
|-----------|-------------|-------|
| FastAPI | `fastapi_lifespan(app)` (async) | ASGI lifespan context manager |
| Flask | `init_flask(app)` | Imperative `init_app(...)` factory hook |
| Django | `BaldurConfig.ready()` in `AppConfig` | Class-based `apps.py` hook |
| Plain Python / CLI | `baldur.init()` | Direct call from the CLI / script |

All four call `baldur.init()` (or its equivalent) — the framework adapter
is responsible for invoking the centralized wiring (cache + storage backend
defaults, audit pipeline, scheduler, etc.).

---

## Exception Hierarchy

A name is exported by `baldur/__init__.py` iff it is **(a)** a domain base
class, or **(b)** a leaf class raised by code reachable from a top-level
public surface. Everything else stays in `baldur.core.exceptions`.

### When-to-catch table (13 top-level exceptions)

| Exception | When raised | Typical catch pattern | Recovery hint |
|-----------|-------------|----------------------|---------------|
| `BaldurError` | Catch-all base | `except BaldurError` | Last-resort logging + alert |
| `AdapterError` | Adapter base (Redis/SQL misuse) | `except AdapterError` | Reconnect; fall back to in-memory |
| `AdapterNotFoundError` | `ProviderRegistry.resolve(...)` finds no match | Bootstrap path | Wire the missing adapter |
| `CircuitBreakerError` | CB-base — domain-specific subclasses | `except CircuitBreakerError` | Do NOT retry — `non_retryable_exceptions()` includes this |
| `DLQError` | DLQ-base | `except DLQError` | Inspect DLQ entry; manual replay if needed |
| `DLQReplayError` | `ReplayService.replay(...)` could not complete | `except DLQReplayError` | Mark for manual review; surface in admin |
| `ResilienceError` | Resilience-pattern base (retry/timeout/rate-limit) | `except ResilienceError` | Domain-specific fallback |
| `RetryExhaustedError` | All retry attempts failed | `except RetryExhaustedError` | Send to DLQ; alert on SLA breach |
| `TimeoutPolicyError` | `protect(timeout=...)` exceeded | `except TimeoutPolicyError` | Cancel downstream; degrade |
| `RateLimitExceeded` | `@rate_limit` rejected the call | `except RateLimitExceeded` | Surface a 429 to caller; honor `reset_at` |
| `IdempotencyDuplicateError` | `@idempotent` detected a duplicate | `except IdempotencyDuplicateError` | Return cached result; do NOT retry |
| `DomainValidationError` | Domain-tag validation rejected the call | `except DomainValidationError` | Caller bug — fix the domain identifier; do NOT retry |
| `ConfigurationError` | Settings or wiring misconfiguration | Bootstrap path | Crash fast — operator must fix env |

---

## Decorator Locations

| Path | Names | Semantic |
|------|-------|----------|
| `baldur` (top-level) | `protected`, `aprotected` | Universal primitive — `@protected(timeout=...)` style |
| `baldur.decorators` | `dlq_protect`, `idempotent`, `rate_limit`, `domain_tag` | Presets of `@protected` pinned with DLQ+retry+CB, or orthogonal gates |

Rule: the primitive lives at top level. Presets (opinionated combinations)
and orthogonal gates (idempotency, rate limit, domain tagging) live in
`baldur.decorators`.

---

## Canonical Import Paths — PRO

Subscribers consume the PRO surface via nested-path imports into the 7
`Status: Public` sub-packages. The top-level `baldur_pro` package
deliberately exposes only `register_pro_services` (entry-point bootstrap
hook) — there is no top-level marquee re-export layer.

```python
# Entry-point bootstrap (top-level — invoked once at startup)
from baldur_pro import register_pro_services

# DLQ service
from baldur_pro.services.dlq import (
    DLQService, DLQConfig, DLQEntryResult,
    get_dlq_service, reset_dlq_service, store_to_dlq,
)

# Replay queue
from baldur_pro.services.replay import (
    ReplayQueueService, BackpressureStatus, RateLimitStatus,
    get_replay_queue_service, reset_replay_queue_service,
)

# Emergency mode + graceful degradation
from baldur_pro.services.emergency_mode import (
    EmergencyLevel, EmergencyModeError, EmergencyStateError,
    RecoveryNotAllowedError, GracefulDegradationManager,
    get_emergency_manager, is_emergency_active, get_emergency_level,
)

# Bulkhead (resource isolation)
from baldur_pro.services.bulkhead import (
    BulkheadPolicy, AsyncBulkheadPolicy,
    SemaphoreBulkhead, AsyncSemaphoreBulkhead, ThreadPoolBulkhead,
    BulkheadError, BulkheadFullError, BulkheadTimeoutError,
    bulkhead, bulkhead_for_database, bulkhead_for_cache,
    get_bulkhead_registry,
)

# Throttle (adaptive rate limiting)
from baldur_pro.services.throttle import (
    ThrottleConfig, ThrottlePolicy, AdaptiveThrottle,
    get_adaptive_throttle, get_throttle_registry,
)

# Canary rollout + safety interlock
from baldur_pro.services.canary import (
    CanaryRolloutService, CanaryFeatureFlag, CanarySafetyInterlock,
    ConfigLockError, VersionConflictError, InterlockCheckFailure,
    get_canary_rollout_service, get_canary_feature_flag,
)

# Unified notification
from baldur_pro.services.unified_notification import (
    NotificationPayload, NotificationPriority, NotificationCategory,
    UnifiedNotificationManager, ChannelResolver,
    notify, notify_security, notify_sla, notify_error, notify_incident,
    get_unified_notification_manager,
)
```

### Registry-Slot Resolution (alternative path)

The PRO singletons registered by `register_pro_services` are also reachable
through the OSS-Public `ProviderRegistry` API — operators who prefer
slot-based late binding (e.g., to keep adapter swap-points explicit) can use:

```python
from baldur import ProviderRegistry

dlq_service = ProviderRegistry.dlq.safe_get()
governance_checker = ProviderRegistry.governance.safe_get()
emergency_manager = ProviderRegistry.emergency_mode.safe_get()
# ... etc. (slot names mirror the registered providers)
```

Either path is SemVer-covered: nested-path imports are stable under the
`Status: Public` markers above, and `ProviderRegistry.<slot>` slot names
are stable under the OSS `baldur.factory.registry` Public surface.

---

## Exception Hierarchy — PRO Leaves

PRO has no top-level subset (`baldur_pro.__all__` is deliberately minimal).
PRO exception leaves are SemVer-covered by virtue of inclusion in their
owning Public sub-package's `__all__`:

| Sub-package | Public exception leaves |
|-------------|------------------------|
| `services.bulkhead` | `BulkheadError`, `BulkheadFullError`, `BulkheadTimeoutError` |
| `services.emergency_mode` | `EmergencyModeError`, `EmergencyStateError`, `RecoveryNotAllowedError` |
| `services.canary` | `ConfigLockError`, `VersionConflictError`, `InterlockCheckFailure` (failure-code enum) |

PRO-specific `DLQ*Error` subclasses raised inside `services.dlq` inherit
from the OSS-Public `baldur.core.exceptions.DLQError` and are caught via
the OSS top-level `from baldur import DLQError, DLQReplayError` pattern.

Audit and governance exception classes stay Internal — they are not
user-raised contracts and the owning sub-packages are `Status: Internal`.

---

## PRO Internal Sub-Package Surface

Any `baldur_pro.services.*` sub-package **not** listed as `Status: Public` in
the PRO Addendum above is `Status: Internal` for v1.0. An Internal sub-package
may be **user-reachable today** (nested-path imports resolve), but its
nested-path symbols are **NOT SemVer-covered** in v1.x: renames, package moves,
and facade collapses inside them do not constitute breaking changes for v1.x
subscribers. A post-v1.0 freeze pass graduates launch-ready surfaces to
`Status: Public`.

Subscriber code MUST consume governance via `ProviderRegistry.governance`
and audit emitters indirectly through public service surfaces; reaching
directly into Internal sub-packages is not SemVer-covered.

Some `baldur.interfaces.__all__` Protocols are typing seams for internal
OSS↔private boundaries — OSS code type-hints against them and resolves the
concrete implementations through `ProviderRegistry`. They are intentionally
not rendered on this reference and carry no v1.x SemVer surface of their own.

---

## Entitlement Public-Key Note

`src/baldur_pro/_entitlement.py` is Internal by convention (underscore
prefix). It holds the Ed25519 verifying key (`PUBLIC_KEY_BYTES`) used by
`baldur.core.entitlement` to validate signed entitlement tokens. Key
rotation requires a **version-coordinated `baldur-pro` release** —
signing-key and verifying-key changes must ship together. Operators do
NOT consume `PUBLIC_KEY_BYTES` directly; the file is internal
infrastructure and may move or be renamed in v1.x without notice.

An env-var override of the verifying key was rejected as a security
anti-pattern (it would let an attacker-controlled environment substitute
the verifying key, defeating Ed25519 signature validation). Multi-key
pinning and TTL-narrowing are valid follow-ups for the broader licensing
pipeline, not the surface freeze.

---

## Public Env Vars (operator-tunable allowlist)

The operator-tunable `BALDUR_*` allowlist is maintained as a single canonical
list in [Environment Variables](env-vars.md). Everything else with a `BALDUR_*`
prefix is advanced / internal and subject to change in v1.x; the full settings
inventory is internal to v1.0, with operator-tunable promotion handled via
dedicated proposals in later releases.

### Abbreviation Registry

Env-prefix tokens may use abbreviations only from this whitelist:

| Abbreviation | Expansion |
|--------------|-----------|
| `CB` | `CIRCUIT_BREAKER` (e.g., `BALDUR_CB_FAILURE_THRESHOLD`) |
| `DLQ` | `DEAD_LETTER_QUEUE` (e.g., `BALDUR_DLQ_MAX_SIZE`) |

Every other shortened form is spelled out in full (e.g.,
`BALDUR_CANARY_GOVERNANCE_`, `BALDUR_RECOVERY_COORDINATOR_`,
`BALDUR_META_WATCHDOG_`).

---

## Import-Time Contract

PEP 562 lazy-import (`__getattr__` + `TYPE_CHECKING`) is applied to 7
sub-packages with heavy dependency chains: top-level `baldur`,
`baldur.adapters`, `baldur.audit`, `baldur.coordination`, `baldur.models`,
`baldur.services`, `baldur.tasks`. The remaining sub-packages eager-import
at module load time.

Target: `import baldur` cold-start under 100 ms.

For IDE autocomplete, every lazy-imported name has a matching
`TYPE_CHECKING` import block so static analyzers resolve attributes
correctly without triggering the heavy load.

---

## Migration Guide (.env)

Run `scripts/migrate_baldur_env_vars.py` against any `.env` file to apply
the v1.0 rename map automatically:

```bash
# Dry run — preview the rewrite
python scripts/migrate_baldur_env_vars.py path/to/.env --dry-run

# Apply in place
python scripts/migrate_baldur_env_vars.py path/to/.env
```

The script is stdlib-only (Python 3.9+ — no baldur install required) and
carries the full rename map.

### Rename map summary

| Category | Renames |
|----------|---------|
| Correlation engine | `BALDUR_CORRELATION_{ENABLED,...}` (9 vars) → `BALDUR_CORRELATION_ENGINE_*`. DAG/co-occurrence keeps `BALDUR_CORRELATION_*`. |
| License | **Unchanged** — `BALDUR_LICENSE_KEY` / `BALDUR_LICENSE_FILE` preserved across the `entitlement.py` → `license.py` file rename. Zero migration. |
| Abbreviation expansions | `BALDUR_CANARY_GOV_*` → `BALDUR_CANARY_GOVERNANCE_*`; `BALDUR_RECOVERY_COORD_*` → `BALDUR_RECOVERY_COORDINATOR_*`; `BALDUR_META_*` → `BALDUR_META_WATCHDOG_*` |
| Module/prefix alignment | A set of prefix-alignment renames — the script applies the full list. |
| Event logging | `BALDUR_DLQ_LOG_LEVEL` etc. (4 vars) → `BALDUR_EVENT_LOGGING_*` |

### Observability surface changes

Logger event names were mechanically converted from `audit_watchdog_started`
style to `audit.watchdog_started` (`{component}.{entity}_{action}`).

Operators with log-based alert rules (Prometheus, Grafana Loki, Datadog,
etc.) keyed on the old (no-dot) event names MUST update their rules. A
number of free-form log messages (containing spaces / brackets / format
specifiers) are NOT renamed yet and may shift again in a later v1.x release.

Concrete affected event-name prefixes include (non-exhaustive):

- `audit.*` (audit pipeline)
- `coordination.*` (DLQ consumer)
- `metrics.*` (Prometheus emission)
- `services.*.*` (CB, replay, governance, ...)
- `adapters.*` (Redis, SQL)
