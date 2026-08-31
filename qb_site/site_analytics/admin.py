"""Django admin registrations for site analytics models."""

from __future__ import annotations

from django.contrib import admin

from site_analytics.models import AnalyticsDailyMetric, AnalyticsMonthlyMetric, AnalyticsPageView


@admin.register(AnalyticsPageView)
class AnalyticsPageViewAdmin(admin.ModelAdmin):
    list_display = ("site", "path", "occurred_at", "visitor_month_hash_short", "referrer_short")
    list_filter = ("site",)
    search_fields = ("site", "path", "referrer")
    readonly_fields = ("site", "path", "referrer", "user_agent", "occurred_at", "visitor_month_hash")
    ordering = ("-occurred_at",)
    date_hierarchy = "occurred_at"

    # Raw rows are immutable; disable add/change/delete to enforce that.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="hash (prefix)")
    def visitor_month_hash_short(self, obj: AnalyticsPageView) -> str:
        return obj.visitor_month_hash[:12] + "…"

    @admin.display(description="referrer")
    def referrer_short(self, obj: AnalyticsPageView) -> str:
        return (obj.referrer[:60] + "…") if len(obj.referrer) > 60 else obj.referrer


@admin.register(AnalyticsDailyMetric)
class AnalyticsDailyMetricAdmin(admin.ModelAdmin):
    list_display = ("site", "date", "pageviews", "unique_visitors")
    list_filter = ("site",)
    search_fields = ("site",)
    readonly_fields = ("site", "date", "pageviews", "unique_visitors")
    ordering = ("-date", "site")
    date_hierarchy = "date"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AnalyticsMonthlyMetric)
class AnalyticsMonthlyMetricAdmin(admin.ModelAdmin):
    list_display = ("site", "month", "pageviews", "unique_visitors")
    list_filter = ("site",)
    search_fields = ("site",)
    readonly_fields = ("site", "month", "pageviews", "unique_visitors")
    ordering = ("-month", "site")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
