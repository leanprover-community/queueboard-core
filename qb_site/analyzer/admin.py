from __future__ import annotations

from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html

from analyzer.models import (
    PRRevision,
    PRRevisionBuildState,
    QueueRuleSet,
    PRQueueWindow,
    AnalyzerConvergenceSnapshot,
    PRDependency,
    PRDependencyState,
    QueueSnapshot,
    ReviewerAssignmentSnapshot,
    AreaStatsSnapshot,
)
from analyzer.tasks.queueboard_snapshot import build_queueboard_snapshot
from core.models import Repository


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
        "is_active",
        "effective_from",
        "effective_to",
    )
    list_filter = ("repository", "require_ci_success", "is_active")
    search_fields = ("repository__owner", "repository__name", "version", "description")
    raw_id_fields = ("repository",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(PRQueueWindow)
class PRQueueWindowAdmin(ReadOnlyAdmin):
    list_display = ("pull_request", "rule_set", "from_ts", "to_ts", "cycle_index", "created_at", "updated_at")
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


@admin.register(PRDependency)
class PRDependencyAdmin(ReadOnlyAdmin):
    list_display = (
        "pull_request",
        "depends_on_repository",
        "depends_on_number",
        "depends_on_pr_link",
        "created_at",
        "updated_at",
    )
    list_filter = ("depends_on_repository",)
    search_fields = (
        "pull_request__number",
        "pull_request__repository__owner",
        "pull_request__repository__name",
        "depends_on_number",
    )
    raw_id_fields = ("pull_request", "depends_on_repository", "depends_on_pull_request")
    readonly_fields = (
        "pull_request",
        "depends_on_repository",
        "depends_on_number",
        "depends_on_pull_request",
        "created_at",
        "updated_at",
    )

    def depends_on_pr_link(self, obj: PRDependency) -> str:  # pragma: no cover - simple formatting
        if not obj.depends_on_pull_request_id:
            return "-"
        url = reverse("admin:syncer_pullrequest_change", args=[obj.depends_on_pull_request_id])
        return format_html("<a href='{}'>{}</a>", url, obj.depends_on_pull_request)

    depends_on_pr_link.short_description = "Depends on PR"  # type: ignore[attr-defined]
    depends_on_pr_link.admin_order_field = "depends_on_pull_request"  # type: ignore[attr-defined]


@admin.register(PRDependencyState)
class PRDependencyStateAdmin(ReadOnlyAdmin):
    list_display = ("pull_request", "last_checked_at", "last_body_hash", "builder_version", "created_at", "updated_at")
    list_filter = ("builder_version",)
    search_fields = ("pull_request__number", "pull_request__repository__owner", "pull_request__repository__name")
    raw_id_fields = ("pull_request",)
    readonly_fields = (
        "pull_request",
        "last_checked_at",
        "last_body_hash",
        "builder_version",
        "created_at",
        "updated_at",
    )


@admin.register(QueueSnapshot)
class QueueSnapshotAdmin(ReadOnlyAdmin):
    list_display = ("repository", "cache_key", "generated_at", "pr_count", "queue_count")
    list_filter = ("cache_key",)
    search_fields = ("repository__owner", "repository__name", "cache_key")
    readonly_fields = (
        "repository",
        "cache_key",
        "generated_at",
        "expires_at",
        "etag",
        "pr_count",
        "queue_count",
        "payload",
    )

    def get_urls(self):
        urls = super().get_urls()
        from django.urls import path

        custom = [
            path("build/", self.admin_site.admin_view(self.build_snapshot_view), name="analyzer_queue_snapshot_build"),
        ]
        return custom + urls

    def build_snapshot_view(self, request):
        repo_id = request.GET.get("repo_id")
        cache_key = request.GET.get("cache_key", "default")
        if not repo_id:
            self.message_user(request, "Missing repo_id", level="error")
            return HttpResponseRedirect(reverse("admin:analyzer_queuesnapshot_changelist"))
        try:
            repo = Repository.objects.get(pk=repo_id)
        except Repository.DoesNotExist:
            self.message_user(request, f"Repository {repo_id} not found", level="error")
            return HttpResponseRedirect(reverse("admin:analyzer_queuesnapshot_changelist"))

        build_queueboard_snapshot.delay(repository_id=repo.id, cache_key=cache_key)
        self.message_user(request, f"Enqueued snapshot build for {repo} (cache_key={cache_key})")
        return HttpResponseRedirect(reverse("admin:analyzer_queuesnapshot_changelist"))


@admin.register(ReviewerAssignmentSnapshot)
class ReviewerAssignmentSnapshotAdmin(ReadOnlyAdmin):
    list_display = ("repository", "cache_key", "generated_at", "assignment_count")
    list_filter = ("cache_key",)
    search_fields = ("repository__owner", "repository__name", "cache_key")
    readonly_fields = (
        "repository",
        "queue_snapshot",
        "cache_key",
        "generated_at",
        "expires_at",
        "etag",
        "assignment_count",
        "payload",
    )

    def get_urls(self):
        urls = super().get_urls()
        from django.urls import path

        custom = [
            path(
                "build/",
                self.admin_site.admin_view(self.build_snapshot_view),
                name="analyzer_reviewerassignmentsnapshot_build",
            ),
        ]
        return custom + urls

    def build_snapshot_view(self, request):
        from analyzer.tasks.reviewer_assignment import build_reviewer_assignment

        repo_id = request.GET.get("repo_id")
        cache_key = request.GET.get("cache_key", "default")
        if not repo_id:
            self.message_user(request, "Missing repo_id", level="error")
            return HttpResponseRedirect(reverse("admin:analyzer_reviewerassignmentsnapshot_changelist"))
        try:
            repo = Repository.objects.get(pk=repo_id)
        except Repository.DoesNotExist:
            self.message_user(request, f"Repository {repo_id} not found", level="error")
            return HttpResponseRedirect(reverse("admin:analyzer_reviewerassignmentsnapshot_changelist"))

        build_reviewer_assignment.delay(repository_id=repo.id, cache_key=cache_key)
        self.message_user(request, f"Enqueued reviewer assignment build for {repo} (cache_key={cache_key})")
        return HttpResponseRedirect(reverse("admin:analyzer_reviewerassignmentsnapshot_changelist"))


@admin.register(AreaStatsSnapshot)
class AreaStatsSnapshotAdmin(ReadOnlyAdmin):
    list_display = ("repository", "cache_key", "generated_at", "area_count")
    list_filter = ("cache_key",)
    search_fields = ("repository__owner", "repository__name", "cache_key")
    readonly_fields = (
        "repository",
        "queue_snapshot",
        "cache_key",
        "generated_at",
        "expires_at",
        "etag",
        "area_count",
        "payload",
    )

    def get_urls(self):
        urls = super().get_urls()
        from django.urls import path

        custom = [
            path(
                "build/",
                self.admin_site.admin_view(self.build_snapshot_view),
                name="analyzer_areastatssnapshot_build",
            ),
        ]
        return custom + urls

    def build_snapshot_view(self, request):
        from analyzer.tasks.reviewer_assignment import build_area_stats

        repo_id = request.GET.get("repo_id")
        cache_key = request.GET.get("cache_key", "default")
        if not repo_id:
            self.message_user(request, "Missing repo_id", level="error")
            return HttpResponseRedirect(reverse("admin:analyzer_areastatssnapshot_changelist"))
        try:
            repo = Repository.objects.get(pk=repo_id)
        except Repository.DoesNotExist:
            self.message_user(request, f"Repository {repo_id} not found", level="error")
            return HttpResponseRedirect(reverse("admin:analyzer_areastatssnapshot_changelist"))

        build_area_stats.delay(repository_id=repo.id, cache_key=cache_key)
        self.message_user(request, f"Enqueued area stats build for {repo} (cache_key={cache_key})")
        return HttpResponseRedirect(reverse("admin:analyzer_areastatssnapshot_changelist"))


@admin.register(AnalyzerConvergenceSnapshot)
class AnalyzerConvergenceSnapshotAdmin(ReadOnlyAdmin):
    list_display = (
        "repository",
        "collected_at",
        "pr_no_revisions",
        "windows_stale",
        "ci_not_checked",
        "ci_gated_missing_windows",
        "prs_missing_queue_window_rollups",
        "prs_missing_dependency_state",
        "prs_stale_dependency_state",
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
        "prs_missing_queue_window_rollups",
        "prs_missing_dependency_state",
        "prs_stale_dependency_state",
        "created_at",
    )

    change_list_template = "admin/analyzer/analyzerconvergencesnapshot/change_list.html"

    def changelist_view(self, request, extra_context=None):  # type: ignore[override]
        extra = extra_context or {}
        if request.method == "POST" and request.POST.get("action") == "collect_convergence":
            try:
                from analyzer.tasks.collect_convergence import collect_analyzer_convergence_task

                async_res = collect_analyzer_convergence_task.delay()
                self.message_user(request, f"Enqueued analyzer convergence collection task: {async_res.id}")
            except Exception as exc:  # pragma: no cover - external dependency
                self.message_user(request, f"Failed to enqueue analyzer convergence collection: {exc}")
            return HttpResponseRedirect(request.path)
        return super().changelist_view(request, extra_context=extra)
