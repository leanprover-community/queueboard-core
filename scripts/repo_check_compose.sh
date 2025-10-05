#!/usr/bin/env bash

# Run Django checks inside Docker Compose using the web service with an overridden command.
# Requires Docker and the Compose plugin available locally/CI.

set -euo pipefail

# Ensure we run from the repo root so docker compose picks up the local project
cd "$(dirname "$0")/.."

echo "[1/3] Starting web (waits on db:healthy via depends_on)"
docker compose up -d web

echo "[2/3] Django system checks (compose)"
docker compose exec -T web python qb_site/manage.py check

echo "[3/3] Dry-run makemigrations (compose)"
docker compose exec -T web python qb_site/manage.py makemigrations --dry-run --check

echo "Compose checks completed."

echo "Stopping web container"
docker compose stop web >/dev/null 2>&1 || true

echo "Stopping database container"
docker compose stop db >/dev/null 2>&1 || true
