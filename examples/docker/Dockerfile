FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

WORKDIR /code
COPY . .
RUN pip install --no-cache-dir .
# selfhealing is auto-installed from pyproject.toml dependencies

ENV PYTHONPATH=/code

CMD ["gunicorn", "myproject.wsgi:application", "-c", "gunicorn.conf.py"]
