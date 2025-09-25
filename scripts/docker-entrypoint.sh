#!/usr/bin/env bash
set -euo pipefail

python qb_site/manage.py migrate --noinput

exec "$@"
