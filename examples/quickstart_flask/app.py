"""Minimal Flask app wired with Baldur.

``init_flask(app)`` calls ``baldur.init()`` exactly once and installs the
request hooks (rate-limit + backpressure + circuit-breaker pre-flight). The
``/demo`` route is protected by the marquee ``@baldur.protected`` facade.
Zero infrastructure: in-memory fallback, no Redis, no env vars.

Run the server with ``flask run`` from this directory, then
``curl http://127.0.0.1:5000/demo``. See ``docs/getting-started/flask.md``.
"""

from __future__ import annotations

from flask import Flask, jsonify

import baldur
from baldur.adapters.flask import init_flask

app = Flask(__name__)
init_flask(app)


@app.get("/demo")
@baldur.protected("demo")
def demo():
    """Return a JSON payload through Baldur's resilience pipeline."""
    return jsonify({"status": "ok", "service": "demo"})
