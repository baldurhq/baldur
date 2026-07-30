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
package metadata. Python 3.14 runs in CI as a non-blocking preview job — it
collects a signal ahead of time and is not a supported version; it will be
listed above once it is green and stays green.

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
| PostgreSQL server | — | 16.x |
| `psycopg2-binary` client | 2.9 | resolved from the extra |

The distinction matters: **Redis server 7.x** is the data store Baldur's
integration suite runs against, while **`redis-py` 4.0** is the floor for the
client library installed by `baldur-framework[redis]` (and by the `celery`,
`arq`, and `rq` extras). The same split applies to PostgreSQL: **server 16.x**
is what the integration suite provisions, and **`psycopg2-binary` 2.9** is the
client floor installed by `baldur-framework[postgres]`.

## Test matrix shape

The Python × Django combinations are tested as a **full grid** — every pair the
two projects both support is exercised on every commit:

| Python | Django 4.2 LTS | Django 5.2 LTS | Django 6.0 |
|--------|:--------------:|:--------------:|:----------:|
| 3.11 | ✅ | ✅ | — |
| 3.12 | ✅ | ✅ | ✅ |
| 3.13 | — | ✅ | ✅ |

The two blank cells are upstream limits rather than gaps in coverage: Django 6.0
requires Python 3.12 or newer, and Django 4.2 supports Python 3.12 at most.
Every remaining combination is a real CI job, so a failure identifies which
axis — the Python version or the Django version — carries the incompatibility.

## Version support policy

Baldur follows a latest-minor support model: the current minor release line
receives patches, and the previous minor reaches end of life the day a new minor
ships. See [`SECURITY.md`](https://github.com/baldurhq/baldur/blob/main/SECURITY.md)
for the full policy.

## Not in this matrix

- **Message-queue, orchestration, and cloud-provider adapters** — these are not
  part of the v1.0 productized surface and are not covered by this matrix.
