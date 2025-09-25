# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md uv.lock* ./
COPY src ./src
COPY qb_site ./qb_site
COPY scripts ./scripts

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -e .

RUN chmod +x scripts/docker-entrypoint.sh

CMD ["python", "qb_site/manage.py", "runserver", "0.0.0.0:8000"]
ENTRYPOINT ["scripts/docker-entrypoint.sh"]
