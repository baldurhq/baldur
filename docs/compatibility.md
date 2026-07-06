# Compatibility

What Baldur v1.0 runs on, and what continuous integration verifies on every
commit. Two facts matter for each dependency:

- **Minimum** — the lowest version Baldur declares it works against (the floor
  pinned in `pyproject.toml`). Anything at or above this is expected to work.
- **Tested in CI** — the exact versions exercised on every commit. This is the
  proof, not just a claim. A minimum wider than the tested set means the floor
  is supported, but only the listed combinations are run end-to-end.

## Runtime

| Component | Minimum | Tested in CI |
|-----------|---------|--------------|
| Python | 3.11 | 3.11 · 3.12 · 3.13 |

Python is tested on the three current releases. There is no upper bound in the
package metadata, but versions above 3.13 are not yet exercised in CI.

## Web frameworks

Baldur's core is framework-agnostic; the framework adapters are optional extras.

| Framework | Extra | Minimum | Tested in CI |
|-----------|-------|---------|--------------|
| Django | `baldur-framework[django]` | 4.2 | 4.2 LTS · 5.2 LTS · 6.0 |
| FastAPI | `baldur-framework[fastapi]` | 0.100 | latest ≥ floor (smoke) |
| Flask | `baldur-framework[flask]` | 2.3 | latest ≥ floor (smoke) |

Django is tested against the two current LTS releases plus the latest feature
release. FastAPI and Flask run a quickstart smoke test (install the extra, start
the app, hit a protected endpoint) against the latest release satisfying the
floor.

## Background tasks

| Component | Extra | Minimum | Tested in CI |
|-----------|-------|---------|--------------|
| Celery | `baldur-framework[celery]` | 5.3 | 5.4 |

## Infrastructure (optional)

Baldur runs zero-config on an in-memory backend with no infrastructure. Redis is
optional and only needed to share state across multiple workers.

| Component | Minimum | Tested in CI |
|-----------|---------|--------------|
| Redis server | — | 7.x |
| `redis-py` client | 4.0 | resolved from the extra |

The distinction matters: **Redis server 7.x** is the data store Baldur's
integration suite runs against, while **`redis-py` 4.0** is the floor for the
client library installed by `baldur-framework[redis]` (and by the `celery`,
`arq`, and `rq` extras).

## Test matrix shape

The Python × Django combinations are tested **diagonally**, not as a full grid:

| Python | Django |
|--------|--------|
| 3.11 | 4.2 |
| 3.12 | 5.2 |
| 3.13 | 6.0 |

Each Python version is paired with one Django version. Off-diagonal
combinations (for example Python 3.11 with Django 6.0) satisfy the declared
minimums and are expected to work, but are not run in CI.

## Version support policy

Baldur follows a latest-minor support model: the current minor release line
receives patches, and the previous minor reaches end of life the day a new minor
ships. See [`SECURITY.md`](https://github.com/gotoUSA/baldur-python/blob/main/SECURITY.md)
for the full policy.

## Not in this matrix

- **PostgreSQL** — the `baldur-framework[postgres]` extra exists for the SQL
  adapter, but the v1.0 integration suite does not provision a PostgreSQL
  service, so no version is listed here as tested.
- **Kafka, Kubernetes, and cloud adapters** — these are not part of the v1.0
  productized surface and are not covered by this matrix.
