"""Celery application configuration for Queueboard."""

from __future__ import annotations

import os

from celery import Celery


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qb_site.settings.local")

app = Celery("qb_site")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
extra_signal_modules = [
    "core.celery_signals",
]
for _mod in extra_signal_modules:  # pragma: no cover - import side-effect
    try:
        __import__(_mod)
    except Exception:
        pass


@app.task(bind=True)
def debug_task(self, *args, **kwargs) -> None:  # pragma: no cover - convenience hook
    """Simple debug task to verify Celery wiring."""
    print(f"Celery debug task executed: {self.request!r}")
