# Contributing to Baldur

Thank you for your interest in Baldur. Contributions are welcome — bug reports,
fixes, documentation, tests, and features. This document explains how to
contribute and the few rules that keep the project healthy.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## What this repository is

This repository is the **open-source core** of Baldur (`baldur-framework`),
licensed under Apache-2.0. It develops in the open, accepts pull requests, and
has a real, forward-growing history.

The commercial tier (`baldur-pro`) and a set of non-productized adapters live in
a **separate private repository** and consume this package as a dependency. You
do not need them to build, test, or contribute to the core. Contributions here
land in the open-source core only; the boundary is one-directional and described
in [ARCHITECTURE.md](ARCHITECTURE.md) (see *The OSS / PRO Boundary*).

## Ways to contribute

- **Bug reports** — open an issue with a minimal reproduction, the Baldur
  version, your Python version, and the framework/adapter in use.
- **Feature requests** — open an issue describing the use case and what the
  current gap costs you.
- **Pull requests** — fixes, features, docs, and tests. Read
  [Before you open a PR](#before-you-open-a-pr) first.
- **Security vulnerabilities** — do **not** open a public issue. Follow
  [SECURITY.md](SECURITY.md).
- **PRO / pricing / licensing questions** — email `support@baldur.sh`.

## Before you open a PR

1. **Open an issue first for anything non-trivial.** A quick discussion avoids
   work that does not fit the architecture. Typo and doc fixes can skip this.
2. **Read [ARCHITECTURE.md](ARCHITECTURE.md).** Most review feedback traces to one
   of the enforced patterns there (the architecture suite under
   `tests/architecture/` mechanically checks them on every commit).
3. **Keep the change focused.** One concern per PR. Separate refactors from
   behavior changes.

## Developer Certificate of Origin (DCO)

Baldur uses the [Developer Certificate of Origin](https://developercertificate.org/)
— a lightweight, sign-off-based affirmation that you wrote the contribution, or
otherwise have the right to submit it under the project's Apache-2.0 license.
There is **no CLA**: inbound contributions are licensed under the same Apache-2.0
license as the project, and the core is never relicensed.

Every commit must carry a `Signed-off-by` line matching the commit author:

```
Signed-off-by: Your Name <your.email@example.com>
```

Add it automatically with the `-s` flag:

```bash
git commit -s -m "fix(retry): correct backoff jitter ceiling"
```

A CI check verifies that every commit in a PR has a matching `Signed-off-by`
line. If you forget, amend or rebase with `git commit --amend -s` /
`git rebase --signoff` and force-push the branch.

You contribute under your **own** identity — name and email of your choosing
(a GitHub `noreply` address is fine). The DCO ties the sign-off to that identity.

## Development setup

```bash
git clone https://github.com/baldurhq/baldur.git
cd baldur-python
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install        # installs the ruff lint/format gate
```

Run the suites:

```bash
pytest tests/unit/ tests/architecture/ -n auto -q
```

The architecture suite (`tests/architecture/`) is the set of fitness
functions described in [ARCHITECTURE.md](ARCHITECTURE.md). If one fails, its
message links to the matching rule.

## Coding standards

- **Style and lint** are enforced by `ruff` via the pre-commit hook. Install it
  once (`pre-commit install`) and it runs on every commit.
- **English only** in code, comments, docstrings, log messages, and error
  messages.
- **Follow the enforced patterns** in [ARCHITECTURE.md](ARCHITECTURE.md):
  `__all__` declarations, lazy imports, singleton `get_*`/`reset_*` pairs,
  `utc_now()` for time, no `print()` in business logic, `baldur_`-prefixed
  metrics, and the rest. The architecture suite is the backstop.
- **Tests.** Bug fixes get a regression test; features get unit tests (and
  integration tests when they cross a service or hit real infrastructure).
- **Public API changes** must keep `__all__` accurate and stay backward-compatible
  within a major version (the project follows SemVer).

## Security-critical core changes

Changes to the resilience core — circuit breaker, retry, idempotency, execution
engine, graceful shutdown, the `protect()` facade, and anything that decides
whether a call is allowed to proceed — get a **stricter review**: at least one
maintainer review focused on safety semantics (fail-open vs fail-closed, lock
correctness, exception handling), and they are not merged on a single
rubber-stamp. If your PR touches these areas, call it out in the description so
reviewers know to apply the stricter bar.

## Review and merge

- A maintainer will review your PR. Expect questions — they are about getting the
  architecture right, not about you.
- CI must be green: the lint gate, the OSS test suites (run with the private
  tiers absent — the published reality), and the DCO check.
- The project does not force-push the published history. Your merged commits
  stay in the permanent record, credited to you.

## License

By contributing, you agree that your contributions are licensed under the
project's [Apache-2.0 license](LICENSE), and you certify the DCO sign-off above.
