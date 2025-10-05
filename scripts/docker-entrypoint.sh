#!/usr/bin/env bash
set -euo pipefail

# Runtime migrations are handled by the dedicated 'migrate' service in Docker Compose.
# This entrypoint simply executes the provided command.
exec "$@"
