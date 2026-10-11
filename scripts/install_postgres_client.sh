#!/usr/bin/env bash
# Install a Postgres client from the PostgreSQL apt repository on a GitHub-hosted Ubuntu runner, and
# put its binaries first on PATH for the job's later steps.
#
# The runner image has its own, older PostgreSQL server and cluster. Debian's pg_wrapper, behind
# /usr/bin/pg_restore and friends, picks the version of that local cluster when no host is given, so
# a plain `pg_restore --list` would run the older client, which refuses newer dumps. Calling the
# version's own binaries avoids the wrapper.
#
#   bash scripts/install_postgres_client.sh 18

set -euo pipefail

major="${1:?usage: $0 <major version>}"

sudo apt-get update
sudo apt-get install -y curl ca-certificates gnupg lsb-release
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc |
  sudo gpg --batch --yes --dearmor -o /etc/apt/trusted.gpg.d/apt.postgresql.org.gpg
echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" |
  sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt-get update
sudo apt-get install -y "postgresql-client-$major"

bin="/usr/lib/postgresql/$major/bin"
echo "Through pg_wrapper, with no host: $(pg_restore --version)"
echo "Installed: $("$bin/pg_restore" --version)"
if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$bin" >>"$GITHUB_PATH"
fi
