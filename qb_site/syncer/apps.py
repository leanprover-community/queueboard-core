from django.apps import AppConfig


class SyncerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "syncer"
    verbose_name = "Queueboard Syncer"
