# API Discoverability Runbook

> **Purpose**: Stand up the OpenAPI 3.0 schema, Swagger UI, ReDoc, and the consolidated `/features/` inventory endpoint on a Baldur-instrumented Django project.
> **Audience**: Operator wiring Baldur for the first time, or an SRE rotating a service to consume Baldur's surface for a custom admin UI.
> **Cadence**: One-time during install; refer back when toggling visibility or rotating tokens.

---

## TL;DR

```bash
pip install 'baldur-framework[django,openapi]'
```

Add `"drf_spectacular"` to your project's `INSTALLED_APPS`, restart the worker, and browse to `/api/baldur/docs/`. Click **Authorize** in Swagger UI, paste `Bearer <jwt>`, then exercise endpoints.

To hide the surface in a hardened environment, set `BALDUR_OPENAPI_ENABLED=0` — the routes become 404.

---

## Step 1 — Install the extras

The Django adapter ships without drf-spectacular by default. Pull it in via the `[openapi]` extras:

```bash
pip install 'baldur-framework[django,openapi]'
```

The `[openapi]` extras adds `drf-spectacular>=0.27`. Without it, the `/api/baldur/{schema,docs,redoc}/` routes silently return an empty pattern list (the conditional-import gate in `src/baldur/api/django/urls/schema.py`).

## Step 2 — Wire `drf_spectacular` into INSTALLED_APPS

Baldur deliberately does **not** mutate your `INSTALLED_APPS` at import time (530 D12 — mutating consumer Django config is fragile). Add the entry yourself:

```python
# settings.py
INSTALLED_APPS = [
    # ...your apps
    "rest_framework",
    "drf_spectacular",          # ← add this line
    "baldur.adapters.django",
]
```

Restart the worker. drf-spectacular's own startup validation produces a clear error if it can't find this app, so misconfiguration surfaces immediately.

## Step 3 — Verify the routes resolve

The four endpoints land under your existing baldur URL prefix (typically `/api/baldur/`):

| Route | Purpose | Auth |
|-------|---------|------|
| `GET /api/baldur/schema/`    | OpenAPI 3.0 JSON document        | Authenticated |
| `GET /api/baldur/docs/`      | Swagger UI HTML                  | Authenticated |
| `GET /api/baldur/redoc/`     | ReDoc HTML                       | Authenticated |
| `GET /api/baldur/features/`  | Consolidated feature inventory   | Admin |

Quick smoke test with a valid bearer token:

```bash
curl -sS -H "Authorization: Bearer ${JWT}" http://localhost:8000/api/baldur/schema/ | jq '.openapi, (.paths | length)'
# "3.0.3"
# 87
```

## Step 4 — Browser access via Swagger UI

Open `http://localhost:8000/api/baldur/docs/`. Swagger UI ships an **Authorize** button (top-right) that accepts a `Bearer <jwt>` value. After authorizing, subsequent calls to `/schema/` and the per-endpoint *Try it out* widgets attach the header automatically — no template override or `SessionAuthentication` fallback needed (530 D11).

If you see a 403 on first load, you haven't authorized yet — that's the expected fail-secure default.

## Step 5 — Inspect the feature inventory

The `/features/` endpoint joins the manifest at `baldur/_data/V1_LAUNCH_MANIFEST.yaml` (shipped in the wheel with the package) with the active entitlement, returning one row per Pydantic `*enabled*` field:

```bash
curl -sS -H "Authorization: Bearer ${ADMIN_JWT}" \
  http://localhost:8000/api/baldur/features/ | jq '.entitlement, (.features | length)'
```

Response shape (abridged — full spec in 530 D9):

```json
{
  "entitlement": {"status": "active", "customer_id": "...", "expires": "...", "days_until_expiry": 312},
  "features": [
    {"module": "circuit_breaker.py", "class": "CircuitBreakerSettings", "field": "enabled", "tier": "Core", "default": true, "enabled": true, "env_var": "BALDUR_CB_ENABLED", "license_status": "active"}
  ]
}
```

The `enabled` field reflects the currently-resolved Pydantic value: if the entry's `env_var` is set in the worker's environment, Baldur invokes the canonical accessor and reads the live setting (Pydantic validators apply). If not set, the manifest `default` is returned directly without instantiation.

## Step 6 — Hide the surface (optional)

For environments where the OpenAPI listing must not be exposed at all (e.g., a production service behind a public ingress without API-key-only enforcement), set:

```bash
BALDUR_OPENAPI_ENABLED=0
```

The `/schema/`, `/docs/`, and `/redoc/` routes drop out of the URL conf entirely — requests return 404. The `/features/` endpoint remains available because it is admin-only and answers a separate operational question (530 D2).

## Step 7 — Customize titles / version (optional)

drf-spectacular reads its document metadata from a `SPECTACULAR_SETTINGS` dict in Django settings. Baldur ships sane defaults; override them when you want the document branded for your service:

```python
SPECTACULAR_SETTINGS = {
    "TITLE": "Acme Reliability API",
    "VERSION": "2025.11",
    "DESCRIPTION": "Custom resilience surface for Acme platform services.",
    "SERVE_INCLUDE_SCHEMA": False,
}
```

These pass through verbatim to drf-spectacular. The equivalent `BALDUR_OPENAPI_TITLE` / `BALDUR_OPENAPI_VERSION` / `BALDUR_OPENAPI_DESCRIPTION` env vars exist on `OpenAPISettings` for cases where SPECTACULAR_SETTINGS is managed elsewhere.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `/schema/` returns 404 | drf-spectacular not installed OR `BALDUR_OPENAPI_ENABLED=0` | `pip install 'baldur-framework[openapi]'` and unset the env var |
| `/schema/` returns 403 | Caller is unauthenticated | Pass a valid `Authorization: Bearer <jwt>` |
| `/features/` returns 403 for an authenticated caller | Caller is not in the `baldur_admin` group | Add the user to `baldur_admin` (or use a Django superuser) |
| `/features/` returns an empty `features` array | Manifest YAML unreachable (force-include broke, env override points at a non-existent file) | Verify `python -c "from importlib.resources import files; print(files('baldur._data').joinpath('V1_LAUNCH_MANIFEST.yaml').is_file())"` returns `True` |
| Schema body is missing request/response shapes for most endpoints | Expected — v1.0 ships paths-only baseline per 530 D6; only `/features/` is fully annotated. Per-view `@extend_schema` campaign is tracked as OOS #530-3 | None; consume the path inventory and rely on Baldur's source documentation for body shapes until the OOS campaign lands |

---

## See Also

- `baldur/_data/V1_LAUNCH_MANIFEST.yaml` — authoritative tier/default contract
