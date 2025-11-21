from __future__ import annotations

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from analyzer.models import PRRevision, PRRevisionBuildState, QueueRuleSet, PRQueueWindow, AnalyzerConvergenceSnapshot


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


@admin.register(PRRevisionBuildState)
class PRRevisionBuildStateAdmin(ReadOnlyAdmin):
    def pr_link(self, obj: PRRevisionBuildState) -> str:  # pragma: no cover - simple formatting
        pr = obj.pull_request
        url = reverse("admin:syncer_pullrequest_change", args=[pr.pk])
        return format_html("<a href='{}'>{}</a>", url, pr)

    pr_link.short_description = "PR"  # type: ignore[attr-defined]
    pr_link.admin_order_field = "pull_request"  # type: ignore[attr-defined]

    def tail_link(self, obj: PRRevisionBuildState) -> str:  # pragma: no cover - simple formatting
        tail = obj.tail_revision
        if tail is None:
            return "-"
        url = reverse("admin:analyzer_prrevision_change", args=[tail.pk])
        return format_html("<a href='{}'>{}</a>", url, tail)

    tail_link.short_description = "tail_revision"  # type: ignore[attr-defined]
    tail_link.admin_order_field = "tail_revision"  # type: ignore[attr-defined]

    list_display = (
        "pr_link",
        "builder_version",
        "revision_version",
        "ci_checked_revision_version",
        "ci_checked_at",
        "windows_built_revision_version",
        "windows_built_at",
        "built_through_ts",
        "dirty_from_ts",
        "tail_link",
        "tail_from_ts",
        "last_built_at",
        "updated_at",
    )
    list_filter = ("builder_version", "revision_version")
    search_fields = ("pull_request__number",)
    raw_id_fields = ("pull_request", "tail_revision")
    readonly_fields = (
        "pull_request",
        "builder_version",
        "revision_version",
        "ci_checked_revision_version",
        "ci_checked_at",
        "windows_built_revision_version",
        "windows_built_at",
        "built_through_ts",
        "dirty_from_ts",
        "tail_revision",
        "tail_from_ts",
        "last_built_at",
        "created_at",
        "updated_at",
    )


@admin.register(QueueRuleSet)
class QueueRuleSetAdmin(admin.ModelAdmin):
    list_display = (
        "repository",
        "version",
        "require_open",
        "require_not_draft",
        "require_ci_success",
        "effective_from",
        "effective_to",
    )
    list_filter = ("repository", "require_ci_success")
    search_fields = ("repository__owner", "repository__name", "version", "description")
    raw_id_fields = ("repository",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(PRQueueWindow)
class PRQueueWindowAdmin(ReadOnlyAdmin):
    list_display = ("pull_request", "rule_set", "from_ts", "to_ts", "cycle_index")
    list_filter = ("rule_set",)
    search_fields = (
        "pull_request__number",
        "rule_set__version",
        "pull_request__repository__owner",
        "pull_request__repository__name",
    )
    date_hierarchy = "from_ts"
    raw_id_fields = ("pull_request", "rule_set")
    readonly_fields = ("pull_request", "rule_set", "from_ts", "to_ts", "cycle_index", "created_at", "updated_at")


@admin.register(AnalyzerConvergenceSnapshot)
class AnalyzerConvergenceSnapshotAdmin(ReadOnlyAdmin):
    list_display = (
        "repository",
        "collected_at",
        "pr_no_revisions",
        "windows_stale",
        "ci_not_checked",
        "ci_gated_missing_windows",
    )
    list_filter = ("repository",)
    date_hierarchy = "collected_at"
    search_fields = ("repository__owner", "repository__name")
    raw_id_fields = ("repository",)
    readonly_fields = (
        "repository",
        "collected_at",
        "pr_no_revisions",
        "windows_stale",
        "ci_not_checked",
        "ci_gated_missing_windows",
        "created_at",
    )
