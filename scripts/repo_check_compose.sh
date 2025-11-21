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
  token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
  if [ -n "$token" ]; then
    echo "Populating GH_TOKEN from environment for compose checks"
    python - <<'PY'
from pathlib import Path
import os

token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
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
fi

echo "[1/6] Starting web (waits on db:healthy via depends_on)"
if ! docker compose up -d web; then
  echo "Compose failed to start services. Dumping service status and migrate logs..." >&2
  docker compose ps || true
  docker compose logs --no-color migrate || true
  exit 1
fi

echo "[2/6] Django system checks (compose)"
docker compose exec -T web python qb_site/manage.py check

echo "[3/6] Dry-run makemigrations (compose)"
docker compose exec -T web python qb_site/manage.py makemigrations --dry-run --check

echo "[4/6] Run core tests (compose)"
# Use higher verbosity to list skipped tests with reasons.
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test core # --verbosity 2

echo "[5/6] Run syncer tests (compose)"
# Use higher verbosity to list skipped tests with reasons.
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test syncer # --verbosity 2

echo "[6/6] Run analyzer tests (compose)"
# Use higher verbosity to list skipped tests with reasons.
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test analyzer # --verbosity 2

echo "Compose checks completed."
