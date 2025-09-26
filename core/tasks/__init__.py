"""Core Celery tasks."""

from __future__ import annotations

import logging

from celery import shared_task


logger = logging.getLogger(__name__)


@shared_task
def heartbeat() -> None:
    """Emit a log line to verify the Celery worker is running."""
    logger.info("Celery heartbeat task executed")
