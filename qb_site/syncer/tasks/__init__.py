from __future__ import annotations

# Ensure Celery autodiscovery picks up our task definitions
# by importing submodules that register tasks.
from . import sync_tasks  # noqa: F401
from . import metrics_tasks  # noqa: F401
