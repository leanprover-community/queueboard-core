from __future__ import annotations

from django.contrib import admin

from analyzer.models import PRRevision


class ReadOnlyAdmin(admin.ModelAdmin):
    """Base admin that makes a model read-only in the Django admin."""

    def has_add_permission(self, request):  # type: ignore[override]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[override]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[override]
        return False


@admin.register(PRRevision)
class PRRevisionAdmin(ReadOnlyAdmin):
    def short_sha(self, obj: PRRevision) -> str:  # pragma: no cover - simple formatting
        return (obj.head_sha or "")[:7]

    short_sha.short_description = "head_sha"  # type: ignore[attr-defined]

    list_display = ("pull_request", "short_sha", "from_ts", "to_ts", "seq")
    list_filter = ("to_ts",)
    search_fields = ("pull_request__number", "head_sha")
    date_hierarchy = "from_ts"
    raw_id_fields = ("pull_request",)
    readonly_fields = ("pull_request", "head_sha", "from_ts", "to_ts", "seq", "created_at", "updated_at")
