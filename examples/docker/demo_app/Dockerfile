FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /repo

# Third-party deps installed at build time. Baldur itself is editable-installed
# at runtime from the repo mounted at /repo by the compose service (the demo
# app modules — settings/urls/views — live in examples/docker/demo_app and are
# resolved by running manage.py from that directory). This mirrors the testbed
# app Dockerfiles: the local package cannot be installed at build time because
# the repo is a runtime volume, not a build-context copy.
RUN pip install "django>=4.2"

CMD ["bash", "-lc", "pip install -e '.[django,prometheus,opentelemetry]' && cd examples/docker/demo_app && python manage.py migrate --noinput && exec python manage.py runserver 0.0.0.0:8000 --noreload"]

EXPOSE 8000
