#!/usr/bin/env python
"""Entry point for the Baldur Django quickstart.

Run the dev server with ``python manage.py runserver`` from this directory,
then ``curl http://127.0.0.1:8000/demo/``.
"""

from __future__ import annotations

import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
