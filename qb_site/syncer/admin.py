from __future__ import annotations

from django.contrib import admin

from .models import (
    PullRequest,
    LabelDef,
    PRLabel,
    PRTimelineEvent,
    CheckRun,
    StatusContext,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Base admin that makes a model read-only in the Django admin."""

    def has_add_permission(self, request):  # type: ignore[override]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[override]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[override]
        return False


class PRLabelInline(admin.TabularInline):
    model = PRLabel
    extra = 0
    can_delete = False
    fields = ("label_def", "created_at")
    readonly_fields = ("label_def", "created_at", "updated_at")
    raw_id_fields = ("label_def",)


@admin.register(PullRequest)
class PullRequestAdmin(ReadOnlyAdmin):
    list_display = (
        "repository",
        "number",
        "state",
        "is_draft",
        "gh_updated_at",
        "last_synced_at",
        "author",
    )
    list_filter = ("repository", "state", "is_draft")
    search_fields = ("title", "number", "author__github_login")
    date_hierarchy = "gh_updated_at"
    raw_id_fields = ("repository", "author")
    readonly_fields = (
        "repository",
        "number",
        "author",
        "state",
        "is_draft",
        "gh_created_at",
        "gh_updated_at",
        "closed_at",
        "merged_at",
        "base_ref_name",
        "head_ref_name",
        "head_repo_owner_login",
        "head_repo_name",
        "title",
        "body",
        "additions",
        "deletions",
        "changed_files_count",
        "last_synced_at",
        "created_at",
        "updated_at",
    )
    inlines = [PRLabelInline]


@admin.register(LabelDef)
class LabelDefAdmin(ReadOnlyAdmin):
    list_display = ("repository", "name", "color")
    list_filter = ("repository",)
    search_fields = ("name",)
    raw_id_fields = ("repository",)
    readonly_fields = ("repository", "name", "color", "created_at", "updated_at")


@admin.register(PRTimelineEvent)
class PRTimelineEventAdmin(ReadOnlyAdmin):
    list_display = ("pull_request", "type", "occurred_at", "label_name")
    list_filter = ("type",)
    search_fields = ("label_name", "pull_request__number")
    date_hierarchy = "occurred_at"
    raw_id_fields = ("pull_request",)
    readonly_fields = (
        "pull_request",
        "github_node_id",
        "type",
        "occurred_at",
        "label_name",
        "created_at",
        "updated_at",
    )


@admin.register(CheckRun)
class CheckRunAdmin(ReadOnlyAdmin):
    def short_sha(self, obj: CheckRun) -> str:  # pragma: no cover - simple formatting
        return obj.head_sha[:7]

    short_sha.short_description = "head_sha"  # type: ignore[attr-defined]

    list_display = ("pull_request", "name", "status", "conclusion", "short_sha", "gh_completed_at")
    list_filter = ("status", "conclusion")
    search_fields = ("name", "head_sha", "pull_request__number")
    date_hierarchy = "gh_completed_at"
    raw_id_fields = ("pull_request",)
    readonly_fields = (
        "pull_request",
        "github_node_id",
        "head_sha",
        "name",
        "status",
        "conclusion",
        "details_url",
        "external_id",
        "gh_started_at",
        "gh_completed_at",
        "last_synced_at",
        "created_at",
        "updated_at",
    )


@admin.register(StatusContext)
class StatusContextAdmin(ReadOnlyAdmin):
    def short_sha(self, obj: StatusContext) -> str:  # pragma: no cover - simple formatting
        return obj.head_sha[:7]

    short_sha.short_description = "head_sha"  # type: ignore[attr-defined]

    list_display = ("pull_request", "name", "state", "short_sha", "gh_created_at")
    list_filter = ("state",)
    search_fields = ("name", "head_sha", "pull_request__number")
    date_hierarchy = "gh_created_at"
    raw_id_fields = ("pull_request",)
    readonly_fields = (
        "pull_request",
        "github_node_id",
        "rest_id",
        "head_sha",
        "name",
        "state",
        "target_url",
        "description",
        "gh_created_at",
        "last_synced_at",
        "created_at",
        "updated_at",
    )
