#!/usr/bin/env bash
set -euo pipefail

# Local helper: restore a Heroku PGBackups dump into a temporary Postgres
# container, sanitize it, export Parquet/CSV datasets, and emit a sanitized dump.
#
# Requires:
# - docker
# - uv (uses `uv run --with` to install pandas/pyarrow on the fly)
# - pg dump file (custom format) on disk
#
# Usage:
#   scripts/local_sanitize_backup.sh -f /path/to/backup.dump \
#     [-o artifacts/local-sanitize] [-p 55432] [-t postgres:17-alpine]

PORT=55432
OUTDIR="artifacts/local-sanitize"
DUMP_PATH=""
CONTAINER_STARTED=0
PG_IMAGE="postgres:17-alpine"

usage() {
  echo "Usage: $0 -f /path/to/backup.dump [-o output_dir] [-p host_port] [-t postgres_image]" >&2
  exit 1
}

while getopts ":f:o:p:t:" opt; do
  case "$opt" in
    f) DUMP_PATH="$OPTARG" ;;
    o) OUTDIR="$OPTARG" ;;
    p) PORT="$OPTARG" ;;
    t) PG_IMAGE="$OPTARG" ;;
    *) usage ;;
  esac
done

if [[ -z "$DUMP_PATH" ]]; then
  usage
fi
if [[ ! -f "$DUMP_PATH" ]]; then
  echo "Dump file not found: $DUMP_PATH" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required but not found in PATH" >&2
  exit 1
fi
if ! docker ps >/dev/null 2>&1; then
  echo "docker is not running or not accessible" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not found in PATH" >&2
  exit 1
fi

uv run python - <<'PY' "$PORT"
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
try:
    s.bind(("127.0.0.1", port))
except OSError:
    print(f"Port {port} appears to be in use. Choose a different port with -p.", file=sys.stderr)
    sys.exit(1)
finally:
    s.close()
PY

CONTAINER="qb-sanitize-$RANDOM"
DB_NAME="analytics_source"
DB_URL="postgres://postgres:postgres@localhost:${PORT}/${DB_NAME}"

cleanup() {
  if [[ $CONTAINER_STARTED -eq 1 ]]; then
    echo "Stopping container ${CONTAINER}..."
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "Starting Postgres container ${CONTAINER} on port ${PORT} with image ${PG_IMAGE}..."
if ! docker run --rm -d --name "$CONTAINER" -e POSTGRES_PASSWORD=postgres -p "${PORT}:5432" "$PG_IMAGE" >/dev/null; then
  echo "Failed to start Postgres container. Is port ${PORT} free and docker available?" >&2
  echo "If you see pg_restore format errors, try a matching Postgres image via -t (e.g., postgres:17-alpine)." >&2
  exit 1
fi
CONTAINER_STARTED=1

echo "Waiting for Postgres to be ready..."
ready=0
for i in {1..30}; do
  if docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ $ready -ne 1 ]]; then
  echo "Postgres did not become ready in time." >&2
  exit 1
fi

echo "Preparing database ${DB_NAME}..."
docker exec -e PGPASSWORD=postgres "$CONTAINER" psql -U postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
docker exec -e PGPASSWORD=postgres "$CONTAINER" psql -U postgres -c "CREATE DATABASE ${DB_NAME};"

echo "Copying dump into container and restoring..."
docker cp "$DUMP_PATH" "${CONTAINER}:/tmp/backup.dump"
docker exec -e PGPASSWORD=postgres "$CONTAINER" pg_restore --verbose --clean --no-acl --no-owner \
  --if-exists \
  -U postgres -d "${DB_NAME}" /tmp/backup.dump

mkdir -p "${OUTDIR}/data"

echo "Sanitizing database..."
DATABASE_URL="$DB_URL" uv run scripts/sanitize_backup.py \
  --database-url "$DB_URL" \
  --manifest "${OUTDIR}/sanitize-manifest.json"

echo "Exporting datasets (parquet)..."
DATABASE_URL="$DB_URL" uv run --with pandas==2.2.3 --with pyarrow==17.0.0 scripts/export_for_analysis.py \
  --database-url "$DB_URL" \
  --output-dir "${OUTDIR}/data" \
  --format parquet

echo "Creating sanitized dump..."
docker exec -e PGPASSWORD=postgres "$CONTAINER" pg_dump --no-acl --no-owner -U postgres -d "${DB_NAME}" -Fc -f /tmp/sanitized.dump
docker cp "${CONTAINER}:/tmp/sanitized.dump" "${OUTDIR}/sanitized.dump"

echo "Done. Outputs in ${OUTDIR}"
