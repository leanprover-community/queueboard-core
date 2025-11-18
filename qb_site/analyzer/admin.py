from __future__ import annotations

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

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
        if not obj.head_sha:
            return "-"
        pr = obj.pull_request
        repo = getattr(pr, "repository", None)
        if repo is None:
            return (obj.head_sha or "")[:7]
        url = f"https://github.com/{repo.owner}/{repo.name}/commit/{obj.head_sha}"
        return format_html("<a href='{}' target='_blank'>{}</a>", url, (obj.head_sha or "")[:7])

    short_sha.short_description = "head_sha"  # type: ignore[attr-defined]
    short_sha.admin_order_field = "head_sha"  # type: ignore[attr-defined]

    def pr_link(self, obj: PRRevision) -> str:  # pragma: no cover - simple formatting
        pr = obj.pull_request
        url = reverse("admin:syncer_pullrequest_change", args=[pr.pk])
        return format_html("<a href='{}'>{}</a>", url, pr)

    pr_link.short_description = "PR"  # type: ignore[attr-defined]
    pr_link.admin_order_field = "pull_request"  # type: ignore[attr-defined]

    list_display = ("pr_link", "short_sha", "from_ts", "to_ts", "seq")
    list_filter = ("to_ts",)
    search_fields = ("pull_request__number", "head_sha")
    date_hierarchy = "from_ts"
    raw_id_fields = ("pull_request",)
    readonly_fields = ("pull_request", "head_sha", "from_ts", "to_ts", "seq", "created_at", "updated_at")
