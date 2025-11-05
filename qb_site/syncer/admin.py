from __future__ import annotations

from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html

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

    actions = ["action_enqueue_sync", "action_enqueue_sync_dry_run"]

    def action_enqueue_sync(self, request, queryset):  # type: ignore[override]
        from syncer.tasks.sync_tasks import sync_pr_task

        enqueued: list[tuple[PullRequest, str]] = []
        for pr in queryset.select_related("repository"):
            # Enqueue Celery task per PR
            async_result = sync_pr_task.delay(pr.repository_id, pr.number)
            enqueued.append((pr, async_result.id))

        context = {
            **self.admin_site.each_context(request),
            "title": "Enqueued PR sync tasks",
            "enqueued": enqueued,
            "changelist_url": reverse("admin:syncer_pullrequest_changelist"),
            "dry_run": False,
        }
        return TemplateResponse(request, "admin/syncer/pullrequest/enqueue_sync.html", context)

    action_enqueue_sync.short_description = "Enqueue sync for selected PRs"  # type: ignore[attr-defined]

    def action_enqueue_sync_dry_run(self, request, queryset):  # type: ignore[override]
        from syncer.tasks.sync_tasks import sync_pr_task

        enqueued: list[tuple[PullRequest, str]] = []
        for pr in queryset.select_related("repository"):
            async_result = sync_pr_task.delay(pr.repository_id, pr.number, dry_run=True)
            enqueued.append((pr, async_result.id))

        context = {
            **self.admin_site.each_context(request),
            "title": "Enqueued DRY-RUN PR sync tasks",
            "enqueued": enqueued,
            "changelist_url": reverse("admin:syncer_pullrequest_changelist"),
            "dry_run": True,
        }
        return TemplateResponse(request, "admin/syncer/pullrequest/enqueue_sync.html", context)

    action_enqueue_sync_dry_run.short_description = "Enqueue DRY-RUN sync for selected PRs"  # type: ignore[attr-defined]


@admin.register(LabelDef)
class LabelDefAdmin(ReadOnlyAdmin):
    list_display = ("repository", "name", "color")
    list_filter = ("repository",)
    search_fields = ("name",)
    raw_id_fields = ("repository",)
    readonly_fields = ("repository", "name", "color", "created_at", "updated_at")


@admin.register(PRTimelineEvent)
class PRTimelineEventAdmin(ReadOnlyAdmin):
    def short_before_sha(self, obj: PRTimelineEvent) -> str:  # pragma: no cover - simple formatting
        return (obj.before_sha or "")[:7]

    def short_after_sha(self, obj: PRTimelineEvent) -> str:  # pragma: no cover - simple formatting
        return (obj.after_sha or "")[:7]

    short_before_sha.short_description = "before_sha"  # type: ignore[attr-defined]
    short_after_sha.short_description = "after_sha"  # type: ignore[attr-defined]

    list_display = ("pull_request", "type", "occurred_at", "label_name", "short_before_sha", "short_after_sha")
    list_filter = ("type",)
    search_fields = ("label_name", "pull_request__number", "before_sha", "after_sha")
    date_hierarchy = "occurred_at"
    raw_id_fields = ("pull_request",)
    readonly_fields = (
        "pull_request",
        "github_node_id",
        "type",
        "occurred_at",
        "label_name",
        "before_sha",
        "after_sha",
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
