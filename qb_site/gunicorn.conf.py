"""Gunicorn defaults for the production image (Dockerfile.production).

Passed explicitly with --config. GUNICORN_CMD_ARGS and command-line flags override these values.
"""

from __future__ import annotations

import os

bind = "0.0.0.0:8000"

# Sync workers (gunicorn's default class), one request each. WEB_CONCURRENCY is gunicorn's own
# variable for the count; reading it here keeps it working alongside this file.
workers = int(os.environ.get("WEB_CONCURRENCY", "3"))

# Zulip commands call Zulip and GitHub inside the request, several calls in a row with 15-30 s
# client timeouts each. Gunicorn's 30 s default could kill a worker part-way through a command
# that has already changed something on GitHub.
timeout = 120

# Worker heartbeats on tmpfs rather than the container's overlay filesystem, which can stall them.
worker_tmp_dir = "/dev/shm"

# Access and error logs to stdout/stderr, for the container runtime to collect.
accesslog = "-"
errorlog = "-"
