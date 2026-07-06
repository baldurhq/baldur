---
title: Self-healing reliability for Python
description: Self-healing reliability for Python — circuit breaker, retry, fallback, and dead-letter queue behind one decorator. Framework-agnostic, zero-config by default.
hide:
  - navigation
  - toc
---

<!--
  No `template:` here on purpose. On baldur.sh the built homepage HTML is
  REPLACED post-build: the overlay build (mkdocs.home.yml) runs a merge hook
  (web/hooks.py) that copies the hand-authored standalone landing
  (web/root/index.html) over this page's rendered output. This Markdown body is
  never shown on baldur.sh; it feeds the public OSS mirror (which has no
  custom_dir and builds this page as plain Markdown), the generated llms.txt,
  and the page's search index / SEO description — which is why the prose below
  is real intro content.
-->

Baldur is a self-healing reliability layer for Python applications: circuit
breaker, retry, fallback, and dead-letter queue behind one decorator. With zero
configuration it runs on an in-memory fallback — no Redis, no environment
variables, no Docker. Add Redis when you go multi-process.

Baldur is framework-agnostic (Django, FastAPI, Flask), ships a built-in web
console to operate and recover from the browser, and exports Prometheus and
OpenTelemetry. The free Apache-2.0 core covers the resilience patterns
themselves; the PRO package adds a durable dead-letter queue with replay, an
audit trail, unified notification, emergency mode, and more.

Start with the [Getting Started guide](getting-started/index.md), compare tiers
in [OSS vs PRO](concepts/oss-vs-pro.md), or read [what self-healing means](concepts/foundations/self-healing.md).
