from __future__ import annotations

from django.apps import AppConfig


class ConsoleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "console"
    verbose_name = "Reviewer Console"
