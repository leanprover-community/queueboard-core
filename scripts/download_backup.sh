#!/usr/bin/env bash
set -euo pipefail

# Download a Heroku PGBackups dump.
# Requires HEROKU_API_KEY and HEROKU_APP_NAME env vars (CI provides the API key).

BACKUP_ID=""
OUTPUT_PATH="backup.dump"
INFO_PATH="backup-info.txt"

usage() {
  echo "Usage: $0 [-b BACKUP_ID] [-o OUTPUT_PATH] [-i INFO_PATH] [-a HEROKU_APP_NAME]" >&2
  exit 1
}

while getopts ":b:o:i:a:" opt; do
  case "$opt" in
    b) BACKUP_ID="$OPTARG" ;;
    o) OUTPUT_PATH="$OPTARG" ;;
    i) INFO_PATH="$OPTARG" ;;
    a) HEROKU_APP_NAME="$OPTARG" ;;
    *) usage ;;
  esac
done

if [[ -z "${HEROKU_APP_NAME:-}" ]]; then
  echo "HEROKU_APP_NAME must be set (env or -a)" >&2
  exit 1
fi
if [[ -z "${HEROKU_API_KEY:-}" ]]; then
  echo "HEROKU_API_KEY must be set in the environment" >&2
  exit 1
fi

echo "Downloading backup ${BACKUP_ID:-latest} for app ${HEROKU_APP_NAME} -> ${OUTPUT_PATH}"
heroku pg:backups:download ${BACKUP_ID:+$BACKUP_ID} \
  --app "${HEROKU_APP_NAME}" \
  --output "${OUTPUT_PATH}"

echo "Writing backup info to ${INFO_PATH}"
{
  echo "# heroku pg:backups:info ${BACKUP_ID:-latest}"
  heroku pg:backups:info ${BACKUP_ID:+$BACKUP_ID} --app "${HEROKU_APP_NAME}" || true
  echo
  echo "# heroku pg:backups (recent list)"
  heroku pg:backups --app "${HEROKU_APP_NAME}" || true
} > "${INFO_PATH}"

echo "Done."
