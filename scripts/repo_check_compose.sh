#!/usr/bin/env bash

# Run Django checks inside Docker Compose using the web service with an overridden command.
# Requires Docker and the Compose plugin available locally/CI.

set -euo pipefail

cleanup() {
  # Always try to stop containers we started, but don't fail the script on cleanup.
  docker compose stop web >/dev/null 2>&1 || true
  docker compose stop db >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Ensure we run from the repo root so docker compose picks up the local project
cd "$(dirname "$0")/.."

echo "[1/5] Starting web (waits on db:healthy via depends_on)"
if ! docker compose up -d web; then
  echo "Compose failed to start services. Dumping service status and migrate logs..." >&2
  docker compose ps || true
  docker compose logs --no-color migrate || true
  exit 1
fi

echo "[2/5] Django system checks (compose)"
docker compose exec -T web python qb_site/manage.py check

echo "[3/5] Dry-run makemigrations (compose)"
docker compose exec -T web python qb_site/manage.py makemigrations --dry-run --check

echo "[4/5] Run syncer tests (compose)"
# Use higher verbosity to list skipped tests with reasons.
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test syncer # --verbosity 2

echo "[5/5] Run analyzer tests (compose)"
# Use higher verbosity to list skipped tests with reasons.
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test analyzer # --verbosity 2

echo "Compose checks completed."
