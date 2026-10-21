# syntax=docker/dockerfile:1

# Development image, for docker-compose.yml, which mounts the repo over /app. The production image
# is Dockerfile.production; this one starts from the same base image and the same uv.lock.

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

COPY --from=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH=/opt/venv/bin:$PATH \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Dependencies first, so that a change to the code doesn't reinstall them. The environment is
# outside /app, which Compose mounts over. --locked fails the build if uv.lock is out of date with
# pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY qb_site ./qb_site
COPY scripts ./scripts

# The project itself, editable, which puts src/ on the path.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

RUN chmod +x scripts/docker-entrypoint.sh

CMD ["python", "qb_site/manage.py", "runserver", "0.0.0.0:8000"]
ENTRYPOINT ["scripts/docker-entrypoint.sh"]
