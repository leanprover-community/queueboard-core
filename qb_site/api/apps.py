from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "qb_site.api"
    verbose_name = "Queueboard API"
