FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /code
COPY . .
RUN pip install --no-cache-dir .
# selfhealing is auto-installed from pyproject.toml dependencies

ENV PYTHONPATH=/code

CMD ["gunicorn", "myproject.wsgi:application", "-c", "gunicorn.conf.py"]
