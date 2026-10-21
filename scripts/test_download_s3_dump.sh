#!/usr/bin/env bash
# Tests scripts/download_s3_dump.py against a local S3 server, with dumps made by Postgres 18, and
# restores what it picks with the analytics workflow's pg_restore flags. Also checks that Postgres
# 18's pg_restore restores a Postgres 17 dump with those flags.
#
# Needs Docker, and a Python with boto3 (the pin in scripts/requirements-analytics.txt); no
# credentials. The script runs the host's pg_restore, as the workflow does, if it is Postgres 18 or
# newer, and otherwise the one in the Postgres 18 container. REQUIRE_HOST_PG_RESTORE=1 (as in CI,
# after scripts/install_postgres_client.sh) makes the host's a requirement.
#
#   bash scripts/test_download_s3_dump.sh            # PYTHON=... to pick the interpreter

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
S3_IMAGE="versity/versitygw:v1.8.0@sha256:30292fc2eeacc67a36993b01f7a7a5e3361a19cced0e80c1d71cfa2a4b0a2499"
PG18_IMAGE="postgres:18-alpine"
PG17_IMAGE="postgres:17-alpine"
# The workflow's restore, but reading the dump from stdin. pg_restore exits 1 if it met any error.
RESTORE_FLAGS=(--clean --no-acl --no-owner --if-exists)
# Above boto3's 8 MiB multipart threshold, so the download takes the ranged-GET path.
LARGE_DUMP_BYTES=$((9 * 1024 * 1024))

run="qb-s3-dump-test-$$"
work="$(mktemp -d)"
cleanup() {
  # -v: with their anonymous volumes, such as each Postgres's data directory.
  docker rm -f -v "$run-s3" "$run-pg18" "$run-pg17" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

failures=0
pass() { echo "ok - $*"; }
fail() {
  echo "not ok - $*" >&2
  failures=$((failures + 1))
}

# The pg_restore the script will run.
host_major="$(pg_restore --version 2>/dev/null | awk '{print $3}' | cut -d. -f1)" || true
if [[ "$host_major" =~ ^[0-9]+$ ]] && [ "$host_major" -ge 18 ]; then
  script_path="$PATH"
  echo "The script uses the host's pg_restore: $(pg_restore --version)"
elif [ "${REQUIRE_HOST_PG_RESTORE:-}" = 1 ]; then
  echo "REQUIRE_HOST_PG_RESTORE=1, but the host's pg_restore is not Postgres 18 or newer: $(pg_restore --version 2>&1 || true)" >&2
  exit 1
else
  # pg_restore from the Postgres 18 container, reading the dump from stdin as the script feeds it.
  mkdir -p "$work/bin"
  cat >"$work/bin/pg_restore" <<EOF
#!/usr/bin/env bash
exec docker exec -i "$run-pg18" pg_restore "\$@"
EOF
  chmod +x "$work/bin/pg_restore"
  script_path="$work/bin:$PATH"
  echo "The script uses the Postgres 18 container's pg_restore"
fi

# Distinctive, so the check that they appear in no output can look for the bare values.
export BACKUP_S3_BUCKET="qbtest-bucket-5e0c2a"
export BACKUP_S3_ACCESS_KEY_ID="testkey-5e0c2a"
export BACKUP_S3_SECRET_ACCESS_KEY="testsecret-9d41b7"
# Empty, as the workflow passes a repository variable that isn't set.
export BACKUP_S3_REGION="" BACKUP_S3_KEY_PREFIX=""

echo "Starting a local S3 server, Postgres 18 and Postgres 17"
docker run -d --name "$run-s3" -p 127.0.0.1::7070 "$S3_IMAGE" \
  --access "$BACKUP_S3_ACCESS_KEY_ID" --secret "$BACKUP_S3_SECRET_ACCESS_KEY" posix /tmp >/dev/null
docker run -d --name "$run-pg18" -e POSTGRES_PASSWORD=postgres "$PG18_IMAGE" >/dev/null
docker run -d --name "$run-pg17" -e POSTGRES_PASSWORD=postgres "$PG17_IMAGE" >/dev/null

# psql in a container, as the superuser unless -U says otherwise.
pg() {
  local container="$1"
  shift
  docker exec -i -e PGOPTIONS="-c client_min_messages=warning" "$container" \
    psql -X -q -t -A -v ON_ERROR_STOP=1 -U postgres "$@"
}
wait_for() {
  local what="$1"
  shift
  for _ in $(seq 1 60); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "$what did not start" >&2
  exit 1
}
# Over TCP: during initdb the image's temporary server listens on the socket only.
wait_for "Postgres 18" docker exec "$run-pg18" pg_isready -h 127.0.0.1 -U postgres
wait_for "Postgres 17" docker exec "$run-pg17" pg_isready -h 127.0.0.1 -U postgres

s3_port="$(docker port "$run-s3" 7070/tcp | head -n 1 | sed 's/.*://')"
export BACKUP_S3_ENDPOINT="http://127.0.0.1:$s3_port"
# The test's own client, for setting up: the same server and credentials as the script's.
S3_CLIENT='import os, boto3
s3 = boto3.client("s3", endpoint_url=os.environ["BACKUP_S3_ENDPOINT"], region_name="us-east-1",
                  aws_access_key_id=os.environ["BACKUP_S3_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["BACKUP_S3_SECRET_ACCESS_KEY"])'
wait_for "The S3 server" "$PYTHON" -c "$S3_CLIENT
s3.list_buckets()"

echo "Making dumps with Postgres 18, of a database owned by another role"
pg "$run-pg18" -c "CREATE ROLE app LOGIN" -c "CREATE DATABASE app OWNER app" -c "CREATE DATABASE fresh OWNER app"
pg "$run-pg18" -U app -d app -c "CREATE TABLE marker (dump text)" -c "INSERT INTO marker VALUES ('older')"
docker exec "$run-pg18" pg_dump -Fc -U postgres -d app >"$work/older.dump"
pg "$run-pg18" -U app -d app -c "UPDATE marker SET dump = 'newer'" \
  -c "CREATE TABLE filler AS SELECT md5(i::text) || md5((i * 7)::text) AS x FROM generate_series(1, 400000) AS i"
docker exec "$run-pg18" pg_dump -Fc -U postgres -d app >"$work/newer.dump"
# A database with no tables, as a new deployment may dump before its data is restored. Its sequence's
# name would pass for a table's data in a TOC line, if the match weren't anchored.
pg "$run-pg18" -U app -d fresh -c 'CREATE SEQUENCE "s TABLE DATA x"'
docker exec "$run-pg18" pg_dump -Fc -U postgres -d fresh >"$work/empty.dump"
printf 'not a dump\n' >"$work/garbage.dump"
if [ "$(wc -c <"$work/newer.dump")" -lt "$LARGE_DUMP_BYTES" ]; then
  echo "The large dump is only $(wc -c <"$work/newer.dump") bytes: too small to test a multipart download" >&2
  exit 1
fi

{
  echo "$S3_CLIENT"
  cat <<'PY'
import sys

work = sys.argv[1]
bucket = os.environ["BACKUP_S3_BUCKET"]
s3.create_bucket(Bucket=bucket)
for key, name in {
    "queueboard-20261009T100000Z.dump": "older",
    "queueboard-20261010T100000Z.dump": "newer",
    # Newer than any with data, but holding no tables.
    "queueboard-20261010T110000Z.dump": "empty",
    "queueboard-20261010T120000Z.dump": "empty",
    # Not named <prefix>-YYYYMMDDTHHMMSSZ.dump.
    "queueboard-latest.dump": "older",
    "queueboard-20261011T000000Z.dump.tmp": "older",
    "queueboardx-20261011T000000Z.dump": "older",
    "other/queueboard-20261011T000000Z.dump": "older",
    # Another prefix, whose newest dump can't be read.
    "broken-20261010T100000Z.dump": "older",
    "broken-20261010T110000Z.dump": "garbage",
}.items():
    s3.upload_file(f"{work}/{name}.dump", bucket, key)
PY
} | "$PYTHON" - "$work"

# fetch <args...>: run the script with output and metadata in $work; its output in $work/log.
fetch() {
  rm -f "$work/out.dump" "$work/info.txt"
  PATH="$script_path" "$PYTHON" scripts/download_s3_dump.py -o "$work/out.dump" -i "$work/info.txt" "$@" >"$work/log" 2>&1
}
# restored_marker: restore $work/out.dump as the workflow does, into a new database; print its marker.
# Fails if pg_restore does. (Called where errexit is off, so each step checks for itself.)
restored_marker() {
  pg "$run-pg18" -c "DROP DATABASE IF EXISTS analytics_source" -c "CREATE DATABASE analytics_source" || return 1
  docker exec -i "$run-pg18" pg_restore "${RESTORE_FLAGS[@]}" -U postgres -d analytics_source <"$work/out.dump" || return 1
  pg "$run-pg18" -d analytics_source -c "SELECT dump FROM marker"
}
expect_failure() {
  local what="$1" message="$2"
  shift 2
  if fetch "$@"; then
    fail "$what: succeeded"
  elif ! grep -qF "$message" "$work/log"; then
    fail "$what: no '$message' in: $(cat "$work/log")"
  elif [ -e "$work/info.txt" ]; then
    fail "$what: wrote metadata"
  else
    pass "$what"
  fi
}

if ! fetch; then
  fail "newest with table data: $(cat "$work/log")"
elif ! grep -qx '# dump: queueboard-20261010T100000Z.dump' "$work/info.txt"; then
  fail "newest with table data: took the wrong dump: $(cat "$work/info.txt")"
elif ! grep -qx 'skipped, holding no table data: queueboard-20261010T120000Z.dump, queueboard-20261010T110000Z.dump' "$work/info.txt"; then
  fail "newest with table data: didn't record the skipped dumps: $(cat "$work/info.txt")"
elif ! grep -q 'Dumped from database version: 18' "$work/info.txt"; then
  fail "newest with table data: no archive versions in the metadata: $(cat "$work/info.txt")"
elif grep -qE 'Archive created at|dbname' "$work/info.txt"; then
  fail "newest with table data: the metadata says when or from which database the dump was made"
elif grep -qF -e "$BACKUP_S3_ENDPOINT" -e "$BACKUP_S3_BUCKET" -e "$BACKUP_S3_SECRET_ACCESS_KEY" "$work/info.txt" "$work/log"; then
  fail "newest with table data: the endpoint, bucket or a credential is in the output"
elif ! grep -q '^Using pg_restore' "$work/log"; then
  fail "newest with table data: the log doesn't name pg_restore's version: $(cat "$work/log")"
elif ! marker="$(restored_marker)" || [ "$marker" != newer ]; then
  fail "newest with table data: the restore failed, or doesn't hold the newer dump's data"
else
  pass "takes the newest dump with table data (multipart), skipping newer empty ones, and it restores"
fi

if fetch --key queueboard-20261009T100000Z.dump && marker="$(restored_marker)" && [ "$marker" = older ] &&
  grep -qx '# dump: queueboard-20261009T100000Z.dump' "$work/info.txt"; then
  pass "takes a named dump"
else
  fail "takes a named dump: $(cat "$work/log")"
fi

expect_failure "refuses a named dump with no table data" "holds no table data" --key queueboard-20261010T120000Z.dump
expect_failure "a named dump that doesn't exist" "S3 request failed" --key queueboard-20200101T000000Z.dump
expect_failure "gives up after --max-candidates empty dumps" "none of the newest 2 dumps holds table data" --max-candidates 2
BACKUP_S3_KEY_PREFIX=broken expect_failure "stops at a dump pg_restore can't read" "pg_restore --list failed"
BACKUP_S3_KEY_PREFIX=nothing expect_failure "no dumps under the prefix" "the bucket holds no dumps named nothing-"
BACKUP_S3_SECRET_ACCESS_KEY=wrong expect_failure "wrong credentials" "S3 request failed"
BACKUP_S3_ENDPOINT='' expect_failure "no endpoint" "BACKUP_S3_ENDPOINT must be set"
BACKUP_S3_ENDPOINT=endpoint-5e0c2a expect_failure "an endpoint without a scheme" "BACKUP_S3_ENDPOINT is not a valid URL"
if grep -qF endpoint-5e0c2a "$work/log"; then
  fail "an endpoint without a scheme: the error repeats the endpoint"
fi

echo "Restoring a Postgres 17 dump with Postgres 18's pg_restore"
pg "$run-pg17" -c "CREATE DATABASE source"
pg "$run-pg17" -d source -c "CREATE EXTENSION pg_stat_statements" \
  -c "CREATE TABLE marker (dump text)" -c "INSERT INTO marker VALUES ('from 17')"
docker exec "$run-pg17" pg_dump -Fc -U postgres -d source >"$work/out.dump"
if marker="$(restored_marker 2>"$work/log")" && [ "$marker" = "from 17" ]; then
  pass "pg_restore 18 restores a Postgres 17 dump with the workflow's flags"
else
  fail "pg_restore 18 restores a Postgres 17 dump: $(cat "$work/log")"
fi

if [ "$failures" -ne 0 ]; then
  echo "$failures check(s) failed" >&2
  exit 1
fi
echo "All checks passed"
