from django.apps import AppConfig


class SyncerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "qb_site.syncer"
    verbose_name = "Queueboard Syncer"
