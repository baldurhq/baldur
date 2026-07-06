#!/usr/bin/env python
"""Entry point for the Baldur Grafana demo app.

Run inside the compose stack (see examples/docker/docker-compose.yml). The
container migrates the in-memory DB and starts the dev server bound to
0.0.0.0:8000.
"""

from __future__ import annotations

import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
