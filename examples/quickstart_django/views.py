"""Minimal Django view protected by Baldur's marquee facade.

``@baldur.protected("demo")`` wraps the view in Baldur's composed resilience
pipeline (circuit breaker on by default). With zero configuration it uses the
in-memory fallback — no Redis, no env vars. See
``docs/getting-started/django.md`` for the 5-minute walkthrough.
"""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse

import baldur


@baldur.protected("demo")
def demo(request: HttpRequest) -> JsonResponse:
    """Return a JSON payload through Baldur's resilience pipeline."""
    return JsonResponse({"status": "ok", "service": "demo"})
