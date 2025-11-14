from __future__ import annotations

from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import reverse, path
from django.utils.html import format_html

from .models import (
    PullRequest,
    LabelDef,
    PRLabel,
    PRTimelineEvent,
    CheckRun,
    StatusContext,
    SyncerMetricsSnapshot,
)
from analyzer.models import PRRevision
from analyzer.services.revisions import rebuild_pr_revisions
from analyzer.services.ci_backfill import plan_missing_ci_shas, enqueue_ci_by_shas


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


class PRTimelineEventInline(admin.TabularInline):
    model = PRTimelineEvent
    extra = 0
    can_delete = False
    fields = ("type", "occurred_at", "label_name", "before_sha", "after_sha")
    readonly_fields = ("type", "occurred_at", "label_name", "before_sha", "after_sha")
    ordering = ("-occurred_at",)


class CheckRunInline(admin.TabularInline):
    model = CheckRun
    extra = 0
    can_delete = False
    fields = ("name", "status", "conclusion", "head_sha", "gh_completed_at", "details_url")
    readonly_fields = ("name", "status", "conclusion", "head_sha", "gh_completed_at", "details_url")
    ordering = ("-gh_completed_at",)
    show_change_link = True


class StatusContextInline(admin.TabularInline):
    model = StatusContext
    extra = 0
    can_delete = False
    fields = ("name", "state", "head_sha", "gh_created_at", "target_url")
    readonly_fields = ("name", "state", "head_sha", "gh_created_at", "target_url")
    ordering = ("-gh_created_at",)
    show_change_link = True


class PRRevisionInline(admin.TabularInline):
    model = PRRevision
    extra = 0
    can_delete = False
    fields = ("head_sha", "from_ts", "to_ts", "seq")
    readonly_fields = ("head_sha", "from_ts", "to_ts", "seq")
    ordering = ("from_ts",)


@admin.register(PullRequest)
class PullRequestAdmin(ReadOnlyAdmin):
    change_form_template = "admin/syncer/pullrequest/change_form.html"
    list_display = (
        "repository",
        "number",
        "state",
        "is_draft",
        "gh_updated_at",
        "last_synced_at",
        "timeline_backfill_done",
        "commits_backfill_done",
        "author",
    )
    list_filter = ("repository", "state", "is_draft", "timeline_backfill_done", "commits_backfill_done")
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
        "timeline_backfill_cursor",
        "timeline_backfill_done",
        "timeline_earliest_synced_at",
        "commits_backfill_cursor",
        "commits_backfill_done",
        "commits_earliest_synced_at",
        "created_at",
        "updated_at",
    )
    inlines = [PRLabelInline, PRTimelineEventInline, PRRevisionInline, CheckRunInline, StatusContextInline]

    def get_urls(self):  # type: ignore[override]
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/enqueue-sync/",
                self.admin_site.admin_view(self.enqueue_sync_view),
                name="syncer_pullrequest_enqueue_sync",
            ),
            path(
                "<path:object_id>/enqueue-sync-dry/",
                self.admin_site.admin_view(self.enqueue_sync_dry_view),
                name="syncer_pullrequest_enqueue_sync_dry",
            ),
            path(
                "<path:object_id>/enqueue-ci-sha/",
                self.admin_site.admin_view(self.enqueue_ci_sha_view),
                name="syncer_pullrequest_enqueue_ci_sha",
            ),
            path(
                "<path:object_id>/analyzer-rebuild-revisions/",
                self.admin_site.admin_view(self.analyzer_rebuild_revisions_view),
                name="syncer_pullrequest_analyzer_rebuild",
            ),
            path(
                "<path:object_id>/analyzer-enqueue-missing-ci/",
                self.admin_site.admin_view(self.analyzer_enqueue_missing_ci_view),
                name="syncer_pullrequest_analyzer_enqueue_missing_ci",
            ),
        ]
        return custom + urls

    def change_view(self, request, object_id, form_url="", extra_context=None):  # type: ignore[override]
        extra = extra_context or {}
        pr = self.get_object(request, object_id)
        if pr is not None:
            try:
                from django_celery_results.models import TaskResult  # type: ignore

                owner = pr.repository.owner
                name = pr.repository.name
                number = pr.number
                recent = (
                    TaskResult.objects.filter(task_name="syncer.sync_pr")
                    .filter(result__contains=f'"repo": "{owner}/{name}"')
                    .filter(result__contains=f'"number": {int(number)}')
                    .order_by("-date_done")[:10]
                )
                extra.update(
                    {
                        "recent_task_results": recent,
                        "task_results_changelist_url": reverse("admin:django_celery_results_taskresult_changelist"),
                        "task_results_filter_query": f"?task_name=syncer.sync_pr&q={owner}/{name} {int(number)}",
                    }
                )
            except Exception:  # pragma: no cover - optional dependency
                pass
        return super().change_view(request, object_id, form_url, extra_context=extra)

    def enqueue_sync_view(self, request, object_id, *args, **kwargs):  # type: no cover - simple action
        pr = self.get_object(request, object_id)
        if pr is None:
            return TemplateResponse(
                request,
                "admin/syncer/pullrequest/enqueue_sync.html",
                {**self.admin_site.each_context(request), "title": "PR not found", "enqueued": [], "dry_run": False},
            )
        from syncer.tasks.sync_tasks import sync_pr_task
        from django.conf import settings

        async_result = sync_pr_task.delay(
            pr.repository_id,
            pr.number,
            backfill_timeline_pages=int(getattr(settings, "SYNCER_TIMELINE_BACKFILL_PAGES", 0)),
            backfill_commit_pages=int(getattr(settings, "SYNCER_COMMITS_BACKFILL_PAGES", 0)),
        )
        self.message_user(request, f"Enqueued sync for PR #{pr.number}: task_id={async_result.id}")
        return TemplateResponse(
            request,
            "admin/syncer/pullrequest/enqueue_sync.html",
            {
                **self.admin_site.each_context(request),
                "title": "Enqueued PR sync",
                "enqueued": [(pr, async_result.id)],
                "dry_run": False,
                "changelist_url": reverse("admin:syncer_pullrequest_changelist"),
            },
        )

    def enqueue_sync_dry_view(self, request, object_id, *args, **kwargs):  # type: no cover - simple action
        pr = self.get_object(request, object_id)
        if pr is None:
            return TemplateResponse(
                request,
                "admin/syncer/pullrequest/enqueue_sync.html",
                {**self.admin_site.each_context(request), "title": "PR not found", "enqueued": [], "dry_run": True},
            )
        from syncer.tasks.sync_tasks import sync_pr_task
        from django.conf import settings

        async_result = sync_pr_task.delay(
            pr.repository_id,
            pr.number,
            dry_run=True,
            backfill_timeline_pages=int(getattr(settings, "SYNCER_TIMELINE_BACKFILL_PAGES", 0)),
            backfill_commit_pages=int(getattr(settings, "SYNCER_COMMITS_BACKFILL_PAGES", 0)),
        )
        self.message_user(request, f"Enqueued DRY-RUN sync for PR #{pr.number}: task_id={async_result.id}")
        return TemplateResponse(
            request,
            "admin/syncer/pullrequest/enqueue_sync.html",
            {
                **self.admin_site.each_context(request),
                "title": "Enqueued DRY-RUN PR sync",
                "enqueued": [(pr, async_result.id)],
                "dry_run": True,
                "changelist_url": reverse("admin:syncer_pullrequest_changelist"),
            },
        )

    def enqueue_ci_sha_view(self, request, object_id, *args, **kwargs):  # type: no cover - simple action
        from django.conf import settings
        from syncer.tasks.sync_tasks import sync_ci_for_shas_task

        pr = self.get_object(request, object_id)
        if pr is None:
            return TemplateResponse(
                request,
                "admin/syncer/pullrequest/enqueue_sync.html",
                {**self.admin_site.each_context(request), "title": "PR not found", "enqueued": [], "dry_run": False},
            )

        if request.method == "POST":
            raw = request.POST.get("shas", "")
            pages = request.POST.get("pages")
            dry_run = bool(request.POST.get("dry_run"))
            require_assoc = bool(request.POST.get("require_assoc", "on"))
            # Parse SHAs (split by comma/whitespace) and dedupe order-preserving
            toks = [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
            seen = set()
            shas = []
            for t in toks:
                if t not in seen:
                    seen.add(t)
                    shas.append(t)
            max_pages = int(pages) if pages and pages.isdigit() else int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1))
            if shas:
                async_result = sync_ci_for_shas_task.delay(
                    repo_id=pr.repository_id,
                    number=int(pr.number),
                    shas=shas,
                    max_pages_per_sha=max_pages,
                    dry_run=dry_run,
                    require_pr_association=require_assoc,
                )
                self.message_user(
                    request,
                    f"Enqueued CI-by-SHA for PR #{pr.number} (n={len(shas)} SHAs): task_id={async_result.id}",
                )
                return TemplateResponse(
                    request,
                    "admin/syncer/pullrequest/enqueue_sync.html",
                    {
                        **self.admin_site.each_context(request),
                        "title": "Enqueued CI-by-SHA",
                        "enqueued": [(pr, async_result.id)],
                        "dry_run": dry_run,
                        "changelist_url": reverse("admin:syncer_pullrequest_changelist"),
                    },
                )

        # GET or invalid post → render form
        context = {
            **self.admin_site.each_context(request),
            "title": f"Enqueue CI by SHA for {pr}",
            "pr": pr,
            "default_pages": int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1)),
            "changelist_url": reverse("admin:syncer_pullrequest_changelist"),
        }
        return TemplateResponse(request, "admin/syncer/pullrequest/enqueue_ci_sha.html", context)

    def analyzer_rebuild_revisions_view(self, request, object_id, *args, **kwargs):  # type: no cover - simple action
        pr = self.get_object(request, object_id)
        if pr is None:
            self.message_user(request, "PR not found")
            return self.change_view(request, object_id)
        if not pr.timeline_backfill_done:
            self.message_user(request, "Timeline backfill not complete; skipping revisions rebuild")
            return self.change_view(request, object_id)
        res = rebuild_pr_revisions(pr)
        self.message_user(request, f"Analyzer: revisions rebuilt (created={res.created}, deleted={res.deleted})")
        return self.change_view(request, object_id)

    def analyzer_enqueue_missing_ci_view(self, request, object_id, *args, **kwargs):  # type: no cover - simple action
        from django.conf import settings

        pr = self.get_object(request, object_id)
        if pr is None:
            self.message_user(request, "PR not found")
            return self.change_view(request, object_id)
        plan = plan_missing_ci_shas(repo=pr.repository, pr_numbers=[pr.number], limit_per_pr=2)
        if not plan:
            self.message_user(request, "Analyzer: no missing CI heads found for this PR")
            return self.change_view(request, object_id)
        shas = plan[0].shas
        task_id = enqueue_ci_by_shas(
            pr=pr,
            shas=shas,
            pages_per_sha=int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1)),
            require_pr_association=False,
        )
        self.message_user(request, f"Analyzer: enqueued CI by SHA for {len(shas)} head(s); task_id={task_id}")
        return self.change_view(request, object_id)

    actions = ["action_enqueue_sync", "action_enqueue_sync_dry_run"]

    def action_enqueue_sync(self, request, queryset):  # type: ignore[override]
        from syncer.tasks.sync_tasks import sync_pr_task
        from django.conf import settings

        enqueued: list[tuple[PullRequest, str]] = []
        for pr in queryset.select_related("repository"):
            # Enqueue Celery task per PR
            async_result = sync_pr_task.delay(
                pr.repository_id,
                pr.number,
                backfill_timeline_pages=int(getattr(settings, "SYNCER_TIMELINE_BACKFILL_PAGES", 0)),
                backfill_commit_pages=int(getattr(settings, "SYNCER_COMMITS_BACKFILL_PAGES", 0)),
            )
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
        from django.conf import settings

        enqueued: list[tuple[PullRequest, str]] = []
        for pr in queryset.select_related("repository"):
            async_result = sync_pr_task.delay(
                pr.repository_id,
                pr.number,
                dry_run=True,
                backfill_timeline_pages=int(getattr(settings, "SYNCER_TIMELINE_BACKFILL_PAGES", 0)),
                backfill_commit_pages=int(getattr(settings, "SYNCER_COMMITS_BACKFILL_PAGES", 0)),
            )
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


@admin.register(SyncerMetricsSnapshot)
class SyncerMetricsSnapshotAdmin(ReadOnlyAdmin):
    list_display = (
        "window_start",
        "window_seconds",
        "pr_tasks",
        "pr_deferred",
        "pr_failures",
        "pr_token_cost",
        "repo_tasks",
        "repo_low_budget",
        "repo_discovered",
        "repo_enqueued",
        "db_size_bytes",
    )
    date_hierarchy = "window_start"
    ordering = ("-window_start",)
    search_fields = ("window_start",)
    readonly_fields = [
        f.name
        for f in SyncerMetricsSnapshot._meta.fields  # type: ignore[attr-defined]
    ]
