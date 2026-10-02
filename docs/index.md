---
title: Python LLM jobs that survive 429s and outages
description: One line around your OpenAI, Anthropic or Gemini client — your workers share the provider's 429 wait instead of each retrying into it, and a job an outage stops is parked with its arguments and re-run when the provider is back. In-process Python, no proxy.
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

Baldur keeps the Python jobs a failing API stops. For LLM calls it is one line:
`baldur.llm.wrap` puts one wait in front of every worker when OpenAI, Anthropic
or Gemini answers 429 or "overloaded", and `@baldur.protected(..., replay=True)`
parks a job an outage stopped, with its arguments, and re-runs it once the
provider is back — automatically on a Celery worker, or from the console with a
click. See [LLM rate limits](llm-rate-limits.md).

Underneath, it is a reliability layer for any dependency: circuit breaker,
retry, fallback, and dead-letter queue behind one decorator. With zero
configuration it runs on an in-memory fallback — no Redis, no environment
variables, no Docker. Add Redis when you go multi-process.

Baldur is framework-agnostic (Django, FastAPI, Flask), ships a built-in web
console to operate and recover from the browser, and exports Prometheus and
OpenTelemetry. The free Apache-2.0 core covers the resilience patterns
themselves, the dead-letter queue with replay included; the PRO package adds an
audit trail, unified notification, emergency mode, dead-letter operations at
scale, and more.

Start with the [Getting Started guide](getting-started/index.md), compare tiers
in [OSS vs PRO](concepts/oss-vs-pro.md), or read [what self-healing means](concepts/foundations/self-healing.md).
