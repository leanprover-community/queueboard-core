#!/usr/bin/env bash
# Tests scripts/download_s3_dump.py against a local S3 server, with dumps made by Postgres 18, and
# restores what it picks with the analytics workflow's pg_restore flags. Also checks that Postgres
# 18's pg_restore restores a Postgres 17 dump with those flags.
#
# Needs Docker, and a Python with boto3 (the pin in scripts/requirements-analytics.txt); no
# credentials. pg_restore runs in the Postgres 18 container, so none is needed on the host.
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

run="qb-s3-dump-test-$$"
work="$(mktemp -d)"
cleanup() {
  docker rm -f "$run-s3" "$run-pg18" "$run-pg17" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

failures=0
pass() { echo "ok - $*"; }
fail() {
  echo "not ok - $*" >&2
  failures=$((failures + 1))
}

echo "Starting a local S3 server, Postgres 18 and Postgres 17"
docker run -d --name "$run-s3" -p 127.0.0.1::7070 "$S3_IMAGE" \
  --access testkey --secret testsecret posix /tmp >/dev/null
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
export BACKUP_S3_BUCKET="dumps"
export BACKUP_S3_ACCESS_KEY_ID="testkey"
export BACKUP_S3_SECRET_ACCESS_KEY="testsecret"
unset BACKUP_S3_REGION BACKUP_S3_KEY_PREFIX
wait_for "The S3 server" "$PYTHON" -c "
import os, boto3
boto3.client('s3', endpoint_url=os.environ['BACKUP_S3_ENDPOINT'], region_name='us-east-1',
             aws_access_key_id='testkey', aws_secret_access_key='testsecret').list_buckets()"

echo "Making dumps with Postgres 18, of a database owned by another role"
pg "$run-pg18" -c "CREATE ROLE app LOGIN" -c "CREATE DATABASE app OWNER app" -c "CREATE DATABASE fresh OWNER app"
pg "$run-pg18" -U app -d app -c "CREATE TABLE marker (dump text)" -c "INSERT INTO marker VALUES ('older')"
docker exec "$run-pg18" pg_dump -Fc -U postgres -d app >"$work/older.dump"
pg "$run-pg18" -U app -d app -c "UPDATE marker SET dump = 'newer'"
docker exec "$run-pg18" pg_dump -Fc -U postgres -d app >"$work/newer.dump"
# A database with no tables, as a deploy onto a new, empty volume dumps.
docker exec "$run-pg18" pg_dump -Fc -U postgres -d fresh >"$work/empty.dump"
printf 'not a dump\n' >"$work/garbage.dump"

"$PYTHON" - "$work" <<'PY'
import os, sys
import boto3

work = sys.argv[1]
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["BACKUP_S3_ENDPOINT"],
    region_name="us-east-1",
    aws_access_key_id="testkey",
    aws_secret_access_key="testsecret",
)
s3.create_bucket(Bucket="dumps")
for key, name in {
    "queueboard-20261009T101500Z.dump": "older",
    "queueboard-20261010T101500Z.dump": "newer",
    # Newer than any with data, but holding no tables.
    "queueboard-20261010T111500Z.dump": "empty",
    "queueboard-20261010T121500Z.dump": "empty",
    # Not named <prefix>-YYYYMMDDTHHMMSSZ.dump.
    "queueboard-latest.dump": "older",
    "queueboard-20261011T000000Z.dump.tmp": "older",
    "queueboardx-20261011T000000Z.dump": "older",
    "other/queueboard-20261011T000000Z.dump": "older",
    # Another prefix, whose newest dump can't be read.
    "broken-20261010T101500Z.dump": "older",
    "broken-20261010T111500Z.dump": "garbage",
}.items():
    s3.upload_file(f"{work}/{name}.dump", "dumps", key)
PY

# pg_restore from the Postgres 18 container, reading the dump from stdin as the script feeds it.
mkdir -p "$work/bin"
cat >"$work/bin/pg_restore" <<EOF
#!/usr/bin/env bash
exec docker exec -i "$run-pg18" pg_restore "\$@"
EOF
chmod +x "$work/bin/pg_restore"

# fetch <args...>: run the script with output and metadata in $work; its output in $work/log.
fetch() {
  rm -f "$work/out.dump" "$work/info.txt"
  PATH="$work/bin:$PATH" "$PYTHON" scripts/download_s3_dump.py -o "$work/out.dump" -i "$work/info.txt" "$@" >"$work/log" 2>&1
}
# restored_marker: restore $work/out.dump as the workflow does, into a new database; print its marker.
restored_marker() {
  pg "$run-pg18" -c "DROP DATABASE IF EXISTS analytics_source" -c "CREATE DATABASE analytics_source"
  docker exec -i "$run-pg18" pg_restore "${RESTORE_FLAGS[@]}" -U postgres -d analytics_source <"$work/out.dump"
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
elif ! grep -qx '# dump: queueboard-20261010T101500Z.dump' "$work/info.txt"; then
  fail "newest with table data: took the wrong dump: $(cat "$work/info.txt")"
elif ! grep -qx 'skipped, holding no table data: queueboard-20261010T121500Z.dump, queueboard-20261010T111500Z.dump' "$work/info.txt"; then
  fail "newest with table data: didn't record the skipped dumps: $(cat "$work/info.txt")"
elif ! grep -q 'Dumped from database version: 18' "$work/info.txt"; then
  fail "newest with table data: no archive header in the metadata: $(cat "$work/info.txt")"
elif grep -qF -e "$BACKUP_S3_ENDPOINT" -e "$BACKUP_S3_BUCKET/" -e testsecret "$work/info.txt" "$work/log"; then
  fail "newest with table data: the endpoint, bucket or a credential is in the output"
elif [ "$(restored_marker)" != newer ]; then
  fail "newest with table data: the restore doesn't hold the newer dump's data"
else
  pass "takes the newest dump with table data, skipping newer empty ones, and it restores"
fi

if fetch --key queueboard-20261009T101500Z.dump && [ "$(restored_marker)" = older ] &&
  grep -qx '# dump: queueboard-20261009T101500Z.dump' "$work/info.txt"; then
  pass "takes a named dump"
else
  fail "takes a named dump: $(cat "$work/log")"
fi

expect_failure "refuses a named dump with no table data" "holds no table data" --key queueboard-20261010T121500Z.dump
expect_failure "a named dump that doesn't exist" "S3 request failed" --key queueboard-20200101T000000Z.dump
expect_failure "gives up after --max-candidates empty dumps" "none of the newest 2 dumps holds table data" --max-candidates 2
BACKUP_S3_KEY_PREFIX=broken expect_failure "stops at a dump pg_restore can't read" "pg_restore --list failed"
BACKUP_S3_KEY_PREFIX=nothing expect_failure "no dumps under the prefix" "the bucket holds no dumps named nothing-"
BACKUP_S3_SECRET_ACCESS_KEY=wrong expect_failure "wrong credentials" "S3 request failed"
BACKUP_S3_ENDPOINT='' expect_failure "no endpoint" "BACKUP_S3_ENDPOINT must be set"

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
