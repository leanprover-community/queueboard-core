from django.apps import AppConfig
from django.core.checks import register


class SiteAnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "site_analytics"
    verbose_name = "Site Analytics"

    def ready(self) -> None:
        from site_analytics.checks import check_hash_salt_configured

        register(check_hash_salt_configured)
