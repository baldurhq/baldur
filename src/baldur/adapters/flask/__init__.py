"""
Flask adapter for Baldur.

Provides:
- ``init_flask(app, service_name=None)`` — factory hook. Calls
  ``baldur.init()`` exactly once and registers
  ``before_request`` / ``after_request`` handlers that delegate to the
  framework-free helpers in ``baldur.api.middleware``.

Install: ``pip install baldur-framework[flask]``

Example:
    .. code-block:: python

        from flask import Flask
        from baldur.adapters.flask import init_flask

        app = Flask(__name__)
        init_flask(app)

The adapter intentionally stays thin — every decision lives in
``baldur.api.middleware``. The wrapper only translates between Flask's
``request`` proxy and Baldur's ``RequestContext`` / ``ResponseContext``.

Status: Public
"""

from __future__ import annotations

from baldur.adapters.flask.bootstrap import init_flask

__all__ = ["init_flask"]
