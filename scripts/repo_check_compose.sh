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

created_env=0

if [ ! -f .env ]; then
  echo "No .env found; copying .env.example for compose checks"
  cp .env.example .env
  created_env=1
fi

if [ "$created_env" -eq 1 ]; then
  # Prefer a real token if provided; otherwise use a harmless placeholder so tests don't fail.
  token="${GH_TOKEN:-${GITHUB_TOKEN:-local-dev-token}}"
  echo "Populating GH_TOKEN for compose checks"
  python - <<'PY'
from pathlib import Path
import os

token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "local-dev-token"
path = Path(".env")
lines = path.read_text().splitlines()
out = []
found = False
for line in lines:
    if line.startswith("GH_TOKEN="):
        out.append(f"GH_TOKEN={token}")
        found = True
    else:
        out.append(line)

if not found:
    out.append(f"GH_TOKEN={token}")

path.write_text("\n".join(out) + "\n")
PY
fi

if [ "${SKIP_COMPOSE_BUILD:-0}" != "1" ]; then
  echo "[0/9] Building compose images (web/migrate/worker/beat) to pick up dependency changes"
  docker compose build web migrate worker beat
else
  echo "[0/9] Skipping compose build (SKIP_COMPOSE_BUILD=1)"
fi

echo "[1/9] Validate GitHub GraphQL queries (host)"
if [ "${SKIP_GRAPHQL_VALIDATE:-0}" = "1" ]; then
  echo "Skipping GraphQL validation (SKIP_GRAPHQL_VALIDATE=1)"
else
  if command -v uv >/dev/null 2>&1; then
    uv run python scripts/validate_github_graphql.py
  else
    python scripts/validate_github_graphql.py
  fi
fi

echo "[2/9] Starting web (waits on db:healthy via depends_on)"
if ! docker compose up -d web; then
  echo "Compose failed to start services. Dumping service status and migrate logs..." >&2
  docker compose ps || true
  docker compose logs --no-color migrate || true
  exit 1
fi

echo "[3/9] Django system checks (compose)"
docker compose exec -T web python qb_site/manage.py check

echo "[4/9] Dry-run makemigrations (compose)"
docker compose exec -T web python qb_site/manage.py makemigrations --dry-run --check

echo "[5/9] Run core tests (compose)"
# Use higher verbosity to list skipped tests with reasons.
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test core # --verbosity 2

echo "[6/9] Run syncer tests (compose)"
# Use higher verbosity to list skipped tests with reasons.
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test syncer # --verbosity 2

echo "[7/9] Run analyzer tests (compose)"
# Use higher verbosity to list skipped tests with reasons.
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test analyzer # --verbosity 2

echo "[8/9] Run api tests (compose)"
# Use higher verbosity to list skipped tests with reasons.
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test api # --verbosity 2

echo "[9/9] Run zulip_bot tests (compose)"
# Use higher verbosity to list skipped tests with reasons.
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test zulip_bot # --verbosity 2

echo "Compose checks completed."
