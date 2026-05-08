from django.apps import AppConfig


class SyncerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "syncer"
    verbose_name = "Queueboard Syncer"

    def ready(self) -> None:
        # Register concrete sync-schema upgraders. Imports happen inside
        # ready() so model imports don't fire before the app registry is
        # fully populated.
        from syncer.services.sync_schema_upgrade_v2 import register_v2_upgrader

        register_v2_upgrader()
