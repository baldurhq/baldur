FROM python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015

WORKDIR /code
COPY . .
RUN pip install --no-cache-dir .
# selfhealing is auto-installed from pyproject.toml dependencies

ENV PYTHONPATH=/code

CMD ["gunicorn", "myproject.wsgi:application", "-c", "gunicorn.conf.py"]
