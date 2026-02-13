from __future__ import annotations

from django.apps import AppConfig


class ZulipBotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "zulip_bot"
