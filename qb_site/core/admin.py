from __future__ import annotations

from django.contrib import admin
from django.contrib import messages
from django.urls import path, reverse
from django.template.response import TemplateResponse
from django import forms
from django.utils.html import format_html
from django.http import HttpResponseRedirect, HttpResponse
from django.conf import settings
from django.db import transaction
from urllib.parse import quote_plus
from collections import OrderedDict
import json
import ast

from .models import Repository, ReviewerPreference, User
from core.services.reviewer_topics_importer import (
    DEFAULT_REPO,
    ReviewerTopicsExportError,
    ReviewerTopicsImportError,
    export_reviewer_topics,
    import_reviewer_topics,
)
from syncer.models import PullRequest  # type: ignore
from qb_site.celery import app as celery_app


class ReviewerTopicsImportForm(forms.Form):
    repo = forms.CharField(
        label="Repository",
        help_text="owner/name",
        initial=DEFAULT_REPO,
        widget=forms.TextInput(attrs={"size": 40}),
    )
    file = forms.FileField(
        label="reviewer-topics.json",
        help_text="Upload a JSON array of reviewer entries",
        widget=forms.ClearableFileInput(attrs={"accept": "application/json"}),
    )
    replace_labels = forms.BooleanField(
        label="Replace labels (default)",
        required=False,
        initial=True,
        help_text="Uncheck to merge preferred_labels with existing values",
    )
    dry_run = forms.BooleanField(
        label="Dry-run",
        required=False,
        initial=False,
        help_text="Preview changes without writing to the database",
    )
    create_missing_users = forms.BooleanField(
        label="Create missing users",
        required=False,
        initial=True,
        help_text="Create User rows for unknown GitHub logins",
    )
    create_missing_repo_default_branch = forms.CharField(
        label="Default branch for new repository",
        required=False,
        initial="master",
        widget=forms.TextInput(attrs={"size": 20}),
    )
    verbose = forms.BooleanField(
        label="Verbose logs",
        required=False,
        initial=False,
        help_text="Show per-entry changes",
    )

    def clean_repo(self) -> str:
        return (self.cleaned_data["repo"] or "").strip()

    def clean_create_missing_repo_default_branch(self) -> str:
        branch = (self.cleaned_data.get("create_missing_repo_default_branch") or "").strip()
        return branch or "master"


class ReviewerTopicsExportForm(forms.Form):
    repo = forms.CharField(
        label="Repository",
        help_text="owner/name",
        initial=DEFAULT_REPO,
        widget=forms.TextInput(attrs={"size": 40}),
    )

    def clean_repo(self) -> str:
        return (self.cleaned_data["repo"] or "").strip()


class UserZulipImportForm(forms.Form):
    file = forms.FileField(
        label="reviewer_zulip_ids.json",
        help_text="Upload a JSON array with github_login, zulip_full_name, and zulip_user_id.",
        widget=forms.ClearableFileInput(attrs={"accept": "application/json"}),
    )
    create_missing_users = forms.BooleanField(
        label="Create missing users",
        required=False,
        initial=False,
        help_text="Create a User row when github_login does not already exist.",
    )
    dry_run = forms.BooleanField(
        label="Dry-run",
        required=False,
        initial=False,
        help_text="Preview changes without writing to the database.",
    )


class ReviewerAttentionRunForm(forms.Form):
    repository = forms.ModelChoiceField(
        queryset=Repository.objects.order_by("owner", "name"),
        required=False,
        help_text="Optional: run only for one repository.",
    )
    include_inactive_repositories = forms.BooleanField(
        label="Include inactive repositories",
        required=False,
        initial=False,
        help_text="Allow running for inactive repositories when a repository is selected.",
    )
    run_async = forms.BooleanField(
        label="Enqueue as background task",
        required=False,
        initial=True,
        help_text="Recommended for large runs. Uncheck to execute inline in this request.",
    )
    send_zulip_messages = forms.BooleanField(
        label="Send Zulip messages",
        required=False,
        initial=False,
        help_text="If unchecked, delivery is disabled and no Zulip DMs are sent.",
    )
    enforce_auto_unassign = forms.BooleanField(
        label="Enable auto-unassign enforcement",
        required=False,
        initial=False,
        help_text=(
            "If enabled, stale assignments may be unassigned via GitHub mutation. "
            "This applies even when reviewer notifications are disabled."
        ),
    )
    restrict_delivery_reviewers = forms.BooleanField(
        label="Restrict delivery to selected reviewers",
        required=False,
        initial=False,
        help_text="If checked and no reviewers are selected, Zulip delivery is skipped.",
    )
    delivery_reviewers = forms.ModelMultipleChoiceField(
        label="Reviewer delivery allowlist",
        queryset=User.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    new_assignment_ping_window_seconds = forms.IntegerField(
        required=False,
        min_value=1,
        label="New-assignment window override (seconds)",
        help_text="Optional override for the newly-assigned ping window; blank uses schedule-derived value.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        repository = self._selected_repository()
        users = User.objects.filter(reviewer_preferences__notifications_enabled=True)
        if repository is not None:
            users = users.filter(
                reviewer_preferences__repository=repository,
                reviewer_preferences__notifications_enabled=True,
            )
        users = users.order_by("github_login", "id").distinct()
        self.fields["delivery_reviewers"].queryset = users
        self.fields["delivery_reviewers"].label_from_instance = self._user_label  # type: ignore[attr-defined]

    def _selected_repository(self) -> Repository | None:
        raw_id = None
        if self.is_bound:
            raw_id = self.data.get(self.add_prefix("repository"))
        else:
            initial_val = self.initial.get("repository")
            if isinstance(initial_val, Repository):
                return initial_val
            raw_id = initial_val
        if not raw_id:
            return None
        try:
            return Repository.objects.filter(id=int(raw_id)).only("id", "owner", "name", "is_active").first()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _user_label(user: User) -> str:
        return str(user.github_login or f"user#{user.id}")

    def clean(self):
        cleaned = super().clean()
        repository = cleaned.get("repository")
        include_inactive = bool(cleaned.get("include_inactive_repositories"))
        if repository is not None and not repository.is_active and not include_inactive:
            self.add_error(
                "include_inactive_repositories",
                "Selected repository is inactive. Enable 'Include inactive repositories' to run it.",
            )
        return cleaned


@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = (
        "owner",
        "name",
        "sync_tools_link",
        "github_link",
        "default_branch",
        "is_active",
        "github_node_id",
        "created_at",
        "updated_at",
    )
    search_fields = ("owner", "name", "github_node_id")
    list_filter = ("is_active", "default_branch")
    readonly_fields = ("created_at", "updated_at")

    def get_urls(self):  # type: ignore[override]
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/sync-tools/",
                self.admin_site.admin_view(self.sync_tools_view),
                name="core_repository_sync_tools",
            ),
        ]
        return custom + urls

    def sync_tools_link(self, obj):  # pragma: no cover - simple link
        url = reverse("admin:core_repository_sync_tools", args=[obj.pk])
        return format_html("<a href='{}'>Sync tools</a>", url)

    sync_tools_link.short_description = "Tools"  # type: ignore[attr-defined]
    sync_tools_link.allow_tags = True  # type: ignore[attr-defined]

    def github_link(self, obj):  # pragma: no cover - simple link
        url = f"https://github.com/{obj.owner}/{obj.name}"
        return format_html("<a href='{}' target='_blank'>GitHub</a>", url)

    github_link.short_description = "GitHub"  # type: ignore[attr-defined]

    class SyncPRsForm(forms.Form):
        pr_numbers = forms.CharField(
            label="PR numbers",
            help_text="Comma or space separated PR numbers",
            widget=forms.TextInput(attrs={"size": 50}),
        )
        dry_run = forms.BooleanField(label="Dry-run", required=False, initial=False)
        timelineK = forms.IntegerField(label="timelineK", required=False, initial=150, min_value=1)
        commitsM = forms.IntegerField(label="commitsM", required=False, initial=15, min_value=1)

    class RepoSyncTaskForm(forms.Form):
        since = forms.CharField(
            required=False,
            label="Since (optional)",
            help_text="Leave blank to use default sliding lookback",
            widget=forms.TextInput(attrs={"size": 30}),
        )
        states = forms.MultipleChoiceField(
            label="States",
            required=False,
            choices=[("OPEN", "OPEN"), ("MERGED", "MERGED"), ("CLOSED", "CLOSED")],
            widget=forms.CheckboxSelectMultiple,
        )
        limit = forms.IntegerField(label="Limit", required=False, min_value=1, max_value=200)
        dry_run = forms.BooleanField(label="Dry-run", required=False, initial=False)
        timelineK = forms.IntegerField(label="timelineK", required=False, min_value=1)
        commitsM = forms.IntegerField(label="commitsM", required=False, min_value=1)

    # Admin "Sync tools" view for a repository: quick enqueue, discovery, and toggles
    def sync_tools_view(self, request, object_id, *args, **kwargs):  # type: ignore[override]
        repo = self.get_object(request, object_id)
        if repo is None:
            return TemplateResponse(
                request,
                "admin/syncer/repository/tools.html",
                {**self.admin_site.each_context(request), "title": "Repository not found", "repo": None},
            )

        submitted_action: str | None = None
        enqueued: list[tuple[object, object]] = []
        error: str | None = None
        notice: str | None = None
        prs_initial = ""
        if request.method == "POST":
            submitted_action = request.POST.get("action")
            if submitted_action == "enqueue_prs":
                form = self.SyncPRsForm(request.POST, prefix="prs")
                repo_form = self.RepoSyncTaskForm(prefix="repo")
                if form.is_valid():
                    raw = form.cleaned_data["pr_numbers"]
                    prs_initial = raw
                    nums = []
                    for tok in raw.replace(",", " ").split():
                        try:
                            nums.append(int(tok))
                        except ValueError:
                            pass
                    if not nums:
                        error = "No valid PR numbers provided"
                    else:
                        from syncer.tasks.sync_tasks import sync_pr_task

                        dry_run = bool(form.cleaned_data.get("dry_run") or False)
                        timelineK = form.cleaned_data.get("timelineK") or 150
                        commitsM = form.cleaned_data.get("commitsM") or 15
                        from django.conf import settings

                        for n in nums:
                            res = sync_pr_task.delay(
                                repo.id,
                                int(n),
                                timelineK=timelineK,
                                commitsM=commitsM,
                                dry_run=dry_run,
                                backfill_timeline_pages=int(getattr(settings, "SYNCER_TIMELINE_BACKFILL_PAGES", 1)),
                                backfill_commit_pages=int(getattr(settings, "SYNCER_COMMITS_BACKFILL_PAGES", 1)),
                            )
                    enqueued.append((n, res.id))
                else:
                    error = "Invalid form submission for PR numbers"
            elif submitted_action == "enqueue_repo_sync":
                form = self.SyncPRsForm(prefix="prs")
                # Determine which button was pressed in the repo form
                submit_kind = request.POST.get("submit")
                if submit_kind == "enqueue_defaults":
                    # Bypass form values and use defaults entirely
                    from syncer.tasks.sync_tasks import sync_repo_since_task

                    res = sync_repo_since_task.delay(repo.id)
                    enqueued.append((f"repo:{repo.pk}", res.id))
                    repo_form = self.RepoSyncTaskForm(prefix="repo")
                else:
                    # Use provided overrides from the form
                    repo_form = self.RepoSyncTaskForm(request.POST, prefix="repo")
                    if repo_form.is_valid():
                        from syncer.tasks.sync_tasks import sync_repo_since_task

                        since = repo_form.cleaned_data.get("since") or None
                        states = repo_form.cleaned_data.get("states") or None
                        limit = repo_form.cleaned_data.get("limit") or None
                        dry_run = bool(repo_form.cleaned_data.get("dry_run") or False)
                        timelineK = repo_form.cleaned_data.get("timelineK") or None
                        commitsM = repo_form.cleaned_data.get("commitsM") or None

                        res = sync_repo_since_task.delay(
                            repo.id,
                            since_iso=since,
                            limit=limit,
                            states=states,
                            timelineK=timelineK,
                            commitsM=commitsM,
                            dry_run=dry_run,
                        )
                        # Show one row indicating the repo-level task enqueued
                        enqueued.append((f"repo:{repo.pk}", res.id))
                    else:
                        error = "Invalid repo sync form"
            elif submitted_action == "collect_metrics":
                form = self.SyncPRsForm(prefix="prs")
                repo_form = self.RepoSyncTaskForm(prefix="repo")
                try:
                    from syncer.tasks.metrics_tasks import collect_metrics_task

                    async_res = collect_metrics_task.delay()
                    notice = f"Enqueued metrics collection task: {async_res.id}"
                except Exception as e:  # pragma: no cover - external dependency
                    error = f"Failed to enqueue metrics collection: {e}"
            elif submitted_action == "backfill_history":
                form = self.SyncPRsForm(prefix="prs")
                repo_form = self.RepoSyncTaskForm(prefix="repo")
                try:
                    from syncer.tasks.backfill_tasks import backfill_repo_history_task

                    async_res = backfill_repo_history_task.delay(repo.id)
                    notice = f"Enqueued history backfill task for {repo.owner}/{repo.name}: {async_res.id}"
                except Exception as e:  # pragma: no cover - external dependency
                    error = f"Failed to enqueue history backfill: {e}"
            elif submitted_action == "backfill_incomplete":
                form = self.SyncPRsForm(prefix="prs")
                repo_form = self.RepoSyncTaskForm(prefix="repo")
                try:
                    from syncer.tasks.backfill_tasks import backfill_repo_incomplete_prs_task

                    async_res = backfill_repo_incomplete_prs_task.delay(repo.id)
                    notice = f"Enqueued incomplete-PR backfill task for {repo.owner}/{repo.name}: {async_res.id}"
                except Exception as e:  # pragma: no cover - external dependency
                    error = f"Failed to enqueue incomplete-PR backfill: {e}"
            elif submitted_action == "refresh_pending_ci":
                form = self.SyncPRsForm(prefix="prs")
                repo_form = self.RepoSyncTaskForm(prefix="repo")
                try:
                    from syncer.tasks.sync_tasks import refresh_pending_ci_for_repo_task

                    async_res = refresh_pending_ci_for_repo_task.delay(repo.id)
                    notice = f"Enqueued pending-CI refresh task for {repo.owner}/{repo.name}: {async_res.id}"
                except Exception as e:  # pragma: no cover - external dependency
                    error = f"Failed to enqueue pending-CI refresh: {e}"
            elif submitted_action == "toggle_active":
                form = self.SyncPRsForm(prefix="prs")
                repo_form = self.RepoSyncTaskForm(prefix="repo")
                to_state = request.POST.get("to_state")
                if to_state in {"active", "inactive"}:
                    new_val = to_state == "active"
                    if repo.is_active != new_val:
                        repo.is_active = new_val
                        repo.save(update_fields=["is_active"])
                    state_txt = "ACTIVE" if repo.is_active else "INACTIVE"
                    notice = (
                        f"Repository marked {state_txt}. Scheduled sync dispatcher will "
                        f"{'include' if repo.is_active else 'exclude'} this repo."
                    )
                else:
                    error = "Invalid toggle request"
            elif submitted_action == "build_queue_snapshot":
                form = self.SyncPRsForm(prefix="prs")
                repo_form = self.RepoSyncTaskForm(prefix="repo")
                try:
                    from analyzer.tasks.queueboard_snapshot import build_queueboard_snapshot

                    async_res = build_queueboard_snapshot.delay(repository_id=repo.id, cache_key="default")
                    notice = f"Enqueued queue snapshot build for {repo.owner}/{repo.name}: {async_res.id}"
                except Exception as e:  # pragma: no cover - external dependency
                    error = f"Failed to enqueue queue snapshot build: {e}"
            elif submitted_action == "build_reviewer_assignments":
                form = self.SyncPRsForm(prefix="prs")
                repo_form = self.RepoSyncTaskForm(prefix="repo")
                try:
                    from analyzer.tasks.reviewer_assignment import build_reviewer_assignment

                    async_res = build_reviewer_assignment.delay(repository_id=repo.id, cache_key="default")
                    notice = f"Enqueued reviewer assignment build for {repo.owner}/{repo.name}: {async_res.id}"
                except Exception as e:  # pragma: no cover - external dependency
                    error = f"Failed to enqueue reviewer assignment build: {e}"
            elif submitted_action == "build_area_stats":
                form = self.SyncPRsForm(prefix="prs")
                repo_form = self.RepoSyncTaskForm(prefix="repo")
                try:
                    from analyzer.tasks.reviewer_assignment import build_area_stats

                    async_res = build_area_stats.delay(repository_id=repo.id, cache_key="default")
                    notice = f"Enqueued area stats build for {repo.owner}/{repo.name}: {async_res.id}"
                except Exception as e:  # pragma: no cover - external dependency
                    error = f"Failed to enqueue area stats build: {e}"
            else:
                form = self.SyncPRsForm(prefix="prs")
                repo_form = self.RepoSyncTaskForm(prefix="repo")
        else:
            form = self.SyncPRsForm(prefix="prs")
            repo_form = self.RepoSyncTaskForm(prefix="repo")

        context = {
            **self.admin_site.each_context(request),
            "title": f"Sync tools for {repo}",
            "repo": repo,
            "form": form,
            "repo_form": repo_form,
            "enqueued": enqueued,
            "error": error,
            "notice": notice,
            "submitted_action": submitted_action,
            "prs_initial": prs_initial,
            "changelist_url": reverse("admin:core_repository_changelist"),
            "metrics_list_url": reverse("admin:syncer_syncermetricssnapshot_changelist"),
        }
        return TemplateResponse(request, "admin/syncer/repository/tools.html", context)

    # Convenience actions on the changelist
    actions = [
        "open_sync_tools_action",
        "mark_active_action",
        "mark_inactive_action",
        "backfill_history_action",
        "backfill_incomplete_action",
        "refresh_pending_ci_action",
        "build_queue_snapshot_action",
        "build_reviewer_assignment_action",
        "build_area_stats_action",
    ]

    def open_sync_tools_action(self, request, queryset):  # type: ignore[override]
        count = queryset.count()
        if count == 0:
            self.message_user(request, "Select one repository to open sync tools.")
            return None
        if count == 1:
            obj = queryset.first()
            url = reverse("admin:core_repository_sync_tools", args=[obj.pk])
            return HttpResponseRedirect(url)
        # If multiple selected, show quick links
        links = []
        for obj in queryset[:20]:  # cap for safety
            url = reverse("admin:core_repository_sync_tools", args=[obj.pk])
            links.append(f"<li><a href='{url}'>{obj.owner}/{obj.name}</a></li>")
        html = """
        <html><head><title>Open sync tools</title></head><body>
        <h1>Open sync tools</h1>
        <p>Select a repository below:</p>
        <ul>{items}</ul>
        <p><a href='{back}'>Back to repositories</a></p>
        </body></html>
        """.format(items="".join(links), back=reverse("admin:core_repository_changelist"))
        return HttpResponse(html)

    open_sync_tools_action.short_description = "Open sync tools"  # type: ignore[attr-defined]

    def mark_active_action(self, request, queryset):  # type: ignore[override]
        updated = queryset.update(is_active=True)
        self.message_user(request, f"Marked {updated} repositories as active.")

    mark_active_action.short_description = "Mark selected repositories as ACTIVE"  # type: ignore[attr-defined]

    def mark_inactive_action(self, request, queryset):  # type: ignore[override]
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Marked {updated} repositories as inactive.")

    mark_inactive_action.short_description = "Mark selected repositories as INACTIVE"  # type: ignore[attr-defined]

    def backfill_history_action(self, request, queryset):  # type: ignore[override]
        from syncer.tasks.backfill_tasks import backfill_repo_history_task

        count = 0
        for repo in queryset:
            backfill_repo_history_task.delay(repo.id)
            count += 1
        self.message_user(request, f"Enqueued history backfill for {count} repositories.")

    backfill_history_action.short_description = "Enqueue history backfill for selected repositories"  # type: ignore[attr-defined]

    def backfill_incomplete_action(self, request, queryset):  # type: ignore[override]
        from syncer.tasks.backfill_tasks import backfill_repo_incomplete_prs_task

        count = 0
        for repo in queryset:
            backfill_repo_incomplete_prs_task.delay(repo.id)
            count += 1
        self.message_user(request, f"Enqueued incomplete-PR backfill for {count} repositories.")

    backfill_incomplete_action.short_description = "Enqueue incomplete-PR backfill for selected repositories"  # type: ignore[attr-defined]

    def refresh_pending_ci_action(self, request, queryset):  # type: ignore[override]
        from syncer.tasks.sync_tasks import refresh_pending_ci_for_repo_task

        count = 0
        for repo in queryset:
            refresh_pending_ci_for_repo_task.delay(repo.id)
            count += 1
        self.message_user(request, f"Enqueued pending-CI refresh for {count} repositories.")

    refresh_pending_ci_action.short_description = "Enqueue pending-CI refresh for selected repositories"  # type: ignore[attr-defined]

    def build_queue_snapshot_action(self, request, queryset):  # type: ignore[override]
        from analyzer.tasks.queueboard_snapshot import build_queueboard_snapshot

        count = 0
        for repo in queryset:
            build_queueboard_snapshot.delay(repository_id=repo.id, cache_key="default")
            count += 1
        self.message_user(request, f"Enqueued queue snapshot build for {count} repositories.")

    build_queue_snapshot_action.short_description = "Build queue snapshot for selected repositories"  # type: ignore[attr-defined]

    def build_reviewer_assignment_action(self, request, queryset):  # type: ignore[override]
        from analyzer.tasks.reviewer_assignment import build_reviewer_assignment

        count = 0
        for repo in queryset:
            build_reviewer_assignment.delay(repository_id=repo.id, cache_key="default")
            count += 1
        self.message_user(request, f"Enqueued reviewer assignment build for {count} repositories.")

    build_reviewer_assignment_action.short_description = "Build reviewer assignments for selected repositories"  # type: ignore[attr-defined]

    def build_area_stats_action(self, request, queryset):  # type: ignore[override]
        from analyzer.tasks.reviewer_assignment import build_area_stats

        count = 0
        for repo in queryset:
            build_area_stats.delay(repository_id=repo.id, cache_key="default")
            count += 1
        self.message_user(request, f"Enqueued area stats build for {count} repositories.")

    build_area_stats_action.short_description = "Build area stats for selected repositories"  # type: ignore[attr-defined]

    def get_actions(self, request):  # type: ignore[override]
        """Return actions with 'delete_selected' moved to the end.

        Keeps our custom actions in a predictable order and appends the built-in
        delete action last to reduce accidental clicks.
        """
        actions = super().get_actions(request)
        # Extract delete_selected if present
        delete = actions.pop("delete_selected", None)
        ordered = OrderedDict()
        # Ensure our actions appear first in this order if available
        for key in (
            "open_sync_tools_action",
            "mark_active_action",
            "mark_inactive_action",
            "backfill_history_action",
            "backfill_incomplete_action",
            "refresh_pending_ci_action",
            "build_queue_snapshot_action",
            "build_reviewer_assignment_action",
            "build_area_stats_action",
        ):
            if key in actions:
                ordered[key] = actions.pop(key)
        # Append any remaining actions preserving their original order
        for k, v in actions.items():
            ordered[k] = v
        # Finally append delete_selected
        if delete is not None:
            ordered["delete_selected"] = delete
        return ordered


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    change_list_template = "admin/core/user/change_list.html"
    list_display = (
        "github_login",
        "name",
        "zulip_full_name",
        "zulip_user_id",
        "timezone",
        "is_active",
        "created_at",
        "updated_at",
    )
    search_fields = ("github_login", "github_node_id", "zulip_full_name")
    list_filter = ("is_active",)
    readonly_fields = ("created_at", "updated_at")

    def get_urls(self):  # type: ignore[override]
        urls = super().get_urls()
        custom = [
            path(
                "import-zulip-users/",
                self.admin_site.admin_view(self.import_zulip_users_view),
                name="core_user_import_zulip_users",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):  # type: ignore[override]
        extra = extra_context or {}
        extra["import_zulip_users_url"] = reverse("admin:core_user_import_zulip_users")
        return super().changelist_view(request, extra_context=extra)

    def import_zulip_users_view(self, request, *args, **kwargs):  # type: ignore[override]
        result_summary: str | None = None
        conflicts: list[str] = []
        skipped_missing: list[str] = []

        if request.method == "POST":
            form = UserZulipImportForm(request.POST, request.FILES)
            if form.is_valid():
                uploaded_file = form.cleaned_data["file"]
                create_missing_users = bool(form.cleaned_data["create_missing_users"])
                dry_run = bool(form.cleaned_data["dry_run"])
                try:
                    payload = json.load(uploaded_file)
                except json.JSONDecodeError as exc:
                    form.add_error("file", f"Invalid JSON: {exc.msg}")
                else:
                    if not isinstance(payload, list):
                        form.add_error("file", "Expected a JSON array of user entries.")
                    else:
                        parse_errors: list[str] = []
                        seen_logins: set[str] = set()
                        rows: list[tuple[str, str, int]] = []
                        for idx, entry in enumerate(payload, start=1):
                            if not isinstance(entry, dict):
                                parse_errors.append(f"Row {idx}: expected an object.")
                                continue
                            raw_login = entry.get("github_login")
                            raw_full_name = entry.get("zulip_full_name")
                            raw_user_id = entry.get("zulip_user_id")
                            if not isinstance(raw_login, str) or not raw_login.strip():
                                parse_errors.append(f"Row {idx}: github_login must be a non-empty string.")
                                continue
                            if not isinstance(raw_full_name, str) or not raw_full_name.strip():
                                parse_errors.append(f"Row {idx}: zulip_full_name must be a non-empty string.")
                                continue
                            if not isinstance(raw_user_id, int) or raw_user_id <= 0:
                                parse_errors.append(f"Row {idx}: zulip_user_id must be a positive integer.")
                                continue

                            login = raw_login.strip()
                            normalized = login.lower()
                            if normalized in seen_logins:
                                parse_errors.append(f"Row {idx}: duplicate github_login '{login}' in upload.")
                                continue
                            seen_logins.add(normalized)
                            rows.append((login, raw_full_name.strip(), raw_user_id))

                        if parse_errors:
                            form.add_error(None, "Upload has invalid rows.")
                            for err in parse_errors:
                                form.add_error(None, err)
                        else:
                            created_users = 0
                            updated_users = 0
                            unchanged_users = 0
                            with transaction.atomic():
                                for github_login, zulip_full_name, zulip_user_id in rows:
                                    user = (
                                        User.objects.filter(github_login__iexact=github_login)
                                        .only("id", "github_login", "zulip_full_name", "zulip_user_id")
                                        .first()
                                    )

                                    if user is None:
                                        if not create_missing_users:
                                            skipped_missing.append(github_login)
                                            continue
                                        user = User(github_login=github_login)
                                        created_users += 1

                                    login_changed = False
                                    if user.github_login != github_login:
                                        user.github_login = github_login
                                        login_changed = True

                                    conflict_user = (
                                        User.objects.filter(zulip_user_id=zulip_user_id)
                                        .exclude(pk=user.pk)
                                        .only("github_login")
                                        .first()
                                    )
                                    if conflict_user is not None:
                                        conflict_login = conflict_user.github_login or f"user#{conflict_user.pk}"
                                        conflicts.append(
                                            f"{github_login}: zulip_user_id {zulip_user_id} already belongs to {conflict_login}."
                                        )
                                        continue

                                    changed = login_changed
                                    if user.zulip_full_name != zulip_full_name:
                                        user.zulip_full_name = zulip_full_name
                                        changed = True
                                    if user.zulip_user_id != zulip_user_id:
                                        user.zulip_user_id = zulip_user_id
                                        changed = True

                                    if user.pk is None:
                                        if dry_run:
                                            continue
                                        user.save()
                                    elif changed:
                                        updated_users += 1
                                        if dry_run:
                                            continue
                                        user.save(update_fields=["github_login", "zulip_full_name", "zulip_user_id"])
                                    else:
                                        unchanged_users += 1

                                if dry_run:
                                    transaction.set_rollback(True)

                            prefix = "Dry-run summary" if dry_run else "Import summary"
                            result_summary = (
                                f"{prefix}: rows={len(rows)}, created={created_users}, updated={updated_users}, "
                                f"unchanged={unchanged_users}, missing={len(skipped_missing)}, conflicts={len(conflicts)}."
                            )
                            self.message_user(request, result_summary)
                            if conflicts:
                                self.message_user(
                                    request,
                                    f"Skipped {len(conflicts)} row(s) due to Zulip user ID conflicts.",
                                    level=messages.WARNING,
                                )
                            if skipped_missing:
                                self.message_user(
                                    request,
                                    f"Skipped {len(skipped_missing)} row(s) because github_login was not found.",
                                    level=messages.WARNING,
                                )
        else:
            form = UserZulipImportForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Import reviewer_zulip_ids.json",
            "form": form,
            "result_summary": result_summary,
            "conflicts": conflicts,
            "skipped_missing": skipped_missing,
            "changelist_url": reverse("admin:core_user_changelist"),
        }
        return TemplateResponse(request, "admin/core/user/import_zulip_users.html", context)


@admin.register(ReviewerPreference)
class ReviewerPreferenceAdmin(admin.ModelAdmin):
    change_list_template = "admin/core/reviewerpreference/change_list.html"
    list_display = (
        "repository",
        "user",
        "maximum_capacity",
        "auto_assign",
        "notifications_enabled",
        "away_until",
        "created_at",
        "updated_at",
    )
    list_filter = ("auto_assign", "notifications_enabled", "repository")
    search_fields = (
        "user__github_login",
        "repository__owner",
        "repository__name",
    )
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("repository", "user")

    def get_urls(self):  # type: ignore[override]
        urls = super().get_urls()
        custom = [
            path(
                "import-topics/",
                self.admin_site.admin_view(self.import_topics_view),
                name="core_reviewerpreference_import_topics",
            ),
            path(
                "export-topics/",
                self.admin_site.admin_view(self.export_topics_view),
                name="core_reviewerpreference_export_topics",
            ),
            path(
                "run-reviewer-attention/",
                self.admin_site.admin_view(self.run_reviewer_attention_view),
                name="core_reviewerpreference_run_reviewer_attention",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):  # type: ignore[override]
        extra = extra_context or {}
        extra["import_topics_url"] = reverse("admin:core_reviewerpreference_import_topics")
        extra["export_topics_url"] = reverse("admin:core_reviewerpreference_export_topics")
        extra["run_reviewer_attention_url"] = reverse("admin:core_reviewerpreference_run_reviewer_attention")
        return super().changelist_view(request, extra_context=extra)

    def import_topics_view(self, request, *args, **kwargs):  # type: ignore[override]
        result = None
        log_lines: list[str] = []

        def append_log(message: str) -> None:
            # Normalize escaped newlines coming from logs.
            normalized = message.replace("\\n", "\n")
            for part in normalized.splitlines() or [""]:
                log_lines.append(part)

        if request.method == "POST":
            form = ReviewerTopicsImportForm(request.POST, request.FILES)
            if form.is_valid():
                repo_val = form.cleaned_data["repo"]
                uploaded_file = form.cleaned_data["file"]
                replace_labels = form.cleaned_data["replace_labels"]
                dry_run = form.cleaned_data["dry_run"]
                create_missing_users = form.cleaned_data["create_missing_users"]
                create_repo_branch = form.cleaned_data["create_missing_repo_default_branch"]
                verbose = form.cleaned_data["verbose"]

                try:
                    result = import_reviewer_topics(
                        repo=repo_val,
                        file_obj=uploaded_file,
                        replace_labels=replace_labels,
                        dry_run=dry_run,
                        create_missing_users=create_missing_users,
                        create_missing_repo_default_branch=create_repo_branch,
                        verbose=verbose,
                        logger=append_log,
                    )
                except ReviewerTopicsImportError as exc:
                    form.add_error(None, str(exc))
                else:
                    if result.repo_created:
                        self.message_user(request, f"Repository {result.owner}/{result.name} created")
                    self.message_user(request, result.summary_text())
                    if not verbose:
                        log_lines.clear()
        else:
            form = ReviewerTopicsImportForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Import reviewer-topics.json",
            "form": form,
            "result": result,
            "result_summary": result.summary_text() if result else None,
            "log_lines": log_lines,
            "changelist_url": reverse("admin:core_reviewerpreference_changelist"),
        }
        return TemplateResponse(request, "admin/core/reviewerpreference/import_topics.html", context)

    def export_topics_view(self, request, *args, **kwargs):  # type: ignore[override]
        if request.method == "POST":
            form = ReviewerTopicsExportForm(request.POST)
            if form.is_valid():
                repo_val = form.cleaned_data["repo"]
                try:
                    owner, name, entries = export_reviewer_topics(repo=repo_val)
                except ReviewerTopicsExportError as exc:
                    form.add_error(None, str(exc))
                else:
                    payload = json.dumps(entries, indent=2, ensure_ascii=False)
                    filename = f"reviewer-topics-{owner}-{name}.json"
                    resp = HttpResponse(payload, content_type="application/json")
                    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
                    return resp
        else:
            form = ReviewerTopicsExportForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Export reviewer-topics.json",
            "form": form,
            "changelist_url": reverse("admin:core_reviewerpreference_changelist"),
        }
        return TemplateResponse(request, "admin/core/reviewerpreference/export_topics.html", context)

    def run_reviewer_attention_view(self, request, *args, **kwargs):  # type: ignore[override]
        result = None
        if request.method == "POST":
            form = ReviewerAttentionRunForm(request.POST)
            if form.is_valid():
                repository = form.cleaned_data.get("repository")
                include_inactive_repositories = bool(form.cleaned_data.get("include_inactive_repositories"))
                run_async = bool(form.cleaned_data.get("run_async"))
                send_zulip_messages = bool(form.cleaned_data.get("send_zulip_messages"))
                enforce_auto_unassign = bool(form.cleaned_data.get("enforce_auto_unassign"))
                restrict_delivery_reviewers = bool(form.cleaned_data.get("restrict_delivery_reviewers"))
                delivery_reviewers = form.cleaned_data.get("delivery_reviewers")
                new_assignment_ping_window_seconds = form.cleaned_data.get("new_assignment_ping_window_seconds")

                delivery_reviewer_user_ids = None
                if send_zulip_messages and restrict_delivery_reviewers:
                    delivery_reviewer_user_ids = [int(user.id) for user in delivery_reviewers]

                task_kwargs = {
                    "repository_id": int(repository.id) if repository is not None else None,
                    "include_inactive_repositories": include_inactive_repositories,
                    "reports_enabled_override": True,
                    "enforcement_enabled_override": enforce_auto_unassign,
                    "delivery_enabled_override": send_zulip_messages,
                    "delivery_reviewer_user_ids": delivery_reviewer_user_ids,
                    "new_assignment_ping_window_seconds_override": (
                        int(new_assignment_ping_window_seconds) if new_assignment_ping_window_seconds is not None else None
                    ),
                }
                # Keep kwargs compact for readability in admin output.
                task_kwargs = {key: value for key, value in task_kwargs.items() if value is not None}

                from analyzer.tasks.reviewer_attention import reviewer_attention_daily_task

                if run_async:
                    async_res = reviewer_attention_daily_task.delay(**task_kwargs)
                    self.message_user(
                        request,
                        f"Enqueued reviewer attention task: {async_res.id}",
                    )
                    result = {
                        "mode": "enqueued",
                        "task_id": getattr(async_res, "id", None),
                        "task_kwargs": task_kwargs,
                    }
                else:
                    result = reviewer_attention_daily_task.apply(kwargs=task_kwargs).get()
                    self.message_user(request, "Reviewer attention task completed inline.")
        else:
            form = ReviewerAttentionRunForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Run reviewer attention",
            "form": form,
            "result": result,
            "result_summary": json.dumps(result, indent=2, sort_keys=True, default=str) if result is not None else None,
            "changelist_url": reverse("admin:core_reviewerpreference_changelist"),
        }
        return TemplateResponse(request, "admin/core/reviewerpreference/run_reviewer_attention.html", context)


# Enhance django-celery-results TaskResult admin with repo/PR context for syncer tasks
try:
    from django_celery_results.models import TaskResult, GroupResult  # type: ignore
    from django_celery_results.admin import TaskResultAdmin  # type: ignore

    try:  # Unregister default admin to replace with our enhanced version
        admin.site.unregister(TaskResult)
    except admin.sites.NotRegistered:  # pragma: no cover - depends on install order
        pass
    try:  # Hide GroupResult from admin if not used
        admin.site.unregister(GroupResult)
    except admin.sites.NotRegistered:  # pragma: no cover
        pass

    @admin.register(TaskResult)
    class EnhancedTaskResultAdmin(TaskResultAdmin):  # type: ignore[misc]
        change_form_template = "admin/django_celery_results/taskresult/change_form.html"
        change_list_template = "admin/django_celery_results/taskresult/change_list.html"
        list_display = (
            "short_id",
            "parent_link",
            "root_link",
            "task_name_link",
            "repo_pr",
            "status",
            "date_done",
        )
        list_display_links = ("short_id",)
        list_filter = getattr(TaskResultAdmin, "list_filter", tuple()) + ("task_name",)
        search_fields = getattr(TaskResultAdmin, "search_fields", tuple()) + (
            "task_id",
            "result",
            "task_args",
            "task_kwargs",
            "link__parent_task_id",
            "link__root_task_id",
        )

        def lookup_allowed(self, lookup, value, request=None):  # type: ignore[override]
            if lookup in {"link__parent_task_id__exact", "link__root_task_id__exact"}:
                return True
            try:
                return super().lookup_allowed(lookup, value, request)
            except TypeError:
                return super().lookup_allowed(lookup, value)

        def short_id(self, obj):  # type: ignore[override]
            tid = getattr(obj, "task_id", "") or ""
            return tid[:8]

        short_id.short_description = "ID"  # type: ignore[attr-defined]

        def has_add_permission(self, request):  # type: ignore[override]
            return False

        def get_queryset(self, request):  # type: ignore[override]
            qs = super().get_queryset(request)
            try:
                return qs.select_related("link")
            except Exception:
                return qs

        def task_name_link(self, obj):  # type: ignore[override]
            name = getattr(obj, "task_name", "") or ""
            changelist_url = reverse("admin:django_celery_results_taskresult_changelist")
            # Filter by this task_name; preserve existing GET parameters if any.
            url = f"{changelist_url}?task_name={name}"
            return format_html("<a href='{}'>{}</a>", url, name)

        task_name_link.short_description = "Task name"  # type: ignore[attr-defined]
        task_name_link.admin_order_field = "task_name"  # type: ignore[attr-defined]

        def parent_link(self, obj):
            try:
                link = getattr(obj, "link", None)
                parent_id = getattr(link, "parent_task_id", None) if link else None
                if not parent_id:
                    return "-"
                parent_tr = TaskResult.objects.only("id", "task_id").filter(task_id=parent_id).first()
                short = str(parent_id)[:8]
                if parent_tr is not None:
                    url = reverse("admin:django_celery_results_taskresult_change", args=[parent_tr.pk])
                    return format_html("<a href='{}'>{}</a>", url, short)
                return short
            except Exception:  # pragma: no cover - best-effort
                return "-"

        parent_link.short_description = "Parent"  # type: ignore[attr-defined]

        def root_link(self, obj):
            try:
                link = getattr(obj, "link", None)
                root_id = getattr(link, "root_task_id", None) if link else None
                if not root_id:
                    return "-"
                root_tr = TaskResult.objects.only("id", "task_id").filter(task_id=root_id).first()
                short = str(root_id)[:8]
                if root_tr is not None:
                    url = reverse("admin:django_celery_results_taskresult_change", args=[root_tr.pk])
                    return format_html("<a href='{}'>{}</a>", url, short)
                return short
            except Exception:  # pragma: no cover - best-effort
                return "-"

        root_link.short_description = "Root"  # type: ignore[attr-defined]

        def _json_load(self, raw):  # pragma: no cover - trivial helper
            if raw is None:
                return None
            if isinstance(raw, (dict, list)):
                return raw
            try:
                # Primary: JSON (TaskResult often stores JSON-encoded payloads)
                return json.loads(raw)
            except Exception:
                # Fallback: Python literals (e.g., "{'a': 1}" or "['x', 'y']")
                try:
                    val = ast.literal_eval(raw)
                    # Normalize tuples so callers can treat list/tuple similarly.
                    if isinstance(val, tuple):
                        return list(val)
                    return val
                except Exception:
                    return None

        def _decode_repo_and_number(self, obj):
            """Best-effort decode (repo, number, label) from a TaskResult.

            Intended for per-PR tasks like sync_pr and sync_ci_for_shas.
            """
            repo = None
            number = None
            label = "-"

            # Preferred: result payload with "repo" and "number".
            res = self._json_load(getattr(obj, "result", None))
            if isinstance(res, dict) and res.get("repo") and res.get("number") is not None:
                try:
                    number = int(res.get("number"))
                except Exception:
                    number = None
                repo_str = str(res.get("repo"))
                label = f"{repo_str}#{number}" if number is not None else repo_str
                try:
                    owner, name_part = repo_str.split("/", 1)
                    repo = Repository.objects.only("owner", "name", "id").get(owner=owner, name=name_part)
                except Exception:  # pragma: no cover - best-effort
                    pass
                return repo, number, label

            # Next: kwargs with repo_id and number.
            kwargs = self._json_load(getattr(obj, "task_kwargs", None))
            if isinstance(kwargs, dict) and kwargs.get("repo_id") is not None and kwargs.get("number") is not None:
                repo_id = kwargs.get("repo_id")
                number = kwargs.get("number")
                try:
                    repo = Repository.objects.only("owner", "name", "id").get(id=int(repo_id))
                    label = f"{repo.owner}/{repo.name}#{number}"
                except Exception:  # pragma: no cover
                    label = f"repo_id={repo_id}#{number}"
                try:
                    number = int(number)
                except Exception:
                    number = None
                return repo, number, label

            # Finally: args with [repo_id, number, ...].
            args = self._json_load(getattr(obj, "task_args", None))
            if isinstance(args, list) and len(args) >= 2:
                repo_id, number = args[0], args[1]
                try:
                    repo = Repository.objects.only("owner", "name", "id").get(id=int(repo_id))
                    label = f"{repo.owner}/{repo.name}#{number}"
                except Exception:  # pragma: no cover
                    label = f"repo_id={repo_id}#{number}"
                try:
                    number = int(number)
                except Exception:
                    number = None
                return repo, number, label

            return repo, number, label

        def _decode_repo_only(self, obj):
            """Best-effort decode (repo, label) from a TaskResult for repo-scoped tasks."""
            repo = None
            label = "-"

            res = self._json_load(getattr(obj, "result", None))
            if isinstance(res, dict) and res.get("repo"):
                repo_str = str(res.get("repo"))
                label = repo_str
                try:
                    owner, name_part = repo_str.split("/", 1)
                    repo = Repository.objects.only("owner", "name", "id").get(owner=owner, name=name_part)
                except Exception:  # pragma: no cover
                    pass
                return repo, label

            kwargs = self._json_load(getattr(obj, "task_kwargs", None))
            if isinstance(kwargs, dict) and kwargs.get("repo_id") is not None:
                repo_id = kwargs.get("repo_id")
                try:
                    repo = Repository.objects.only("owner", "name", "id").get(id=int(repo_id))
                    label = f"{repo.owner}/{repo.name}"
                except Exception:  # pragma: no cover
                    label = f"repo_id={repo_id}"
                return repo, label

            args = self._json_load(getattr(obj, "task_args", None))
            if isinstance(args, list) and args:
                repo_id = args[0]
                try:
                    repo = Repository.objects.only("owner", "name", "id").get(id=int(repo_id))
                    label = f"{repo.owner}/{repo.name}"
                except Exception:  # pragma: no cover
                    label = f"repo_id={repo_id}"
                return repo, label

            return repo, label

        def _format_pr_link(self, repo, number, label):
            """Format a link to a PR admin page when possible."""
            if repo is None or number is None:
                return label
            pr = PullRequest.objects.filter(repository=repo, number=int(number)).only("id").first()
            if pr is not None:
                url = reverse("admin:syncer_pullrequest_change", args=[pr.pk])
            else:
                url = "{}?repository__id__exact={}&number={}".format(
                    reverse("admin:syncer_pullrequest_changelist"),
                    repo.id,
                    int(number),
                )
            return format_html("<a href='{}'>{}</a>", url, label)

        def _format_repo_link(self, repo, label):
            """Format a link to a Repository admin page when possible."""
            if repo is None:
                return label
            url = reverse("admin:core_repository_change", args=[repo.pk])
            return format_html("<a href='{}'>{}</a>", url, label)

        def repo_pr(self, obj):  # type: ignore[override]
            """Best-effort extraction of repo/PR from task results/kwargs/args for all tasks."""
            res = self._json_load(getattr(obj, "result", None))
            if isinstance(res, dict):
                repo_pr_val = res.get("repo_pr")
                repo_val = res.get("repo")
                number_val = res.get("number")
                if repo_pr_val:
                    try:
                        repo_part, num_part = str(repo_pr_val).split("#", 1)
                        owner, name_part = repo_part.split("/", 1)
                        repo = Repository.objects.filter(owner=owner, name=name_part).only("id", "owner", "name").first()
                        num_int = int(num_part)
                        label = f"{owner}/{name_part}#{num_int}"
                        return self._format_pr_link(repo, num_int, label)
                    except Exception:
                        return str(repo_pr_val)
                if repo_val and number_val is not None:
                    try:
                        owner, name_part = str(repo_val).split("/", 1)
                        repo = Repository.objects.filter(owner=owner, name=name_part).only("id", "owner", "name").first()
                        num_int = int(number_val)
                        label = f"{owner}/{name_part}#{num_int}"
                        return self._format_pr_link(repo, num_int, label)
                    except Exception:
                        return f"{repo_val}#{number_val}"
                if repo_val:
                    try:
                        owner, name_part = str(repo_val).split("/", 1)
                        repo = Repository.objects.filter(owner=owner, name=name_part).only("id", "owner", "name").first()
                        return self._format_repo_link(repo, f"{owner}/{name_part}")
                    except Exception:
                        return str(repo_val)

            kwargs = self._json_load(getattr(obj, "task_kwargs", None))
            if isinstance(kwargs, dict) and kwargs.get("repo_id") is not None:
                repo_id = kwargs.get("repo_id")
                number = kwargs.get("number")
                try:
                    repo = Repository.objects.only("owner", "name", "id").get(id=int(repo_id))
                    if number is None:
                        return self._format_repo_link(repo, f"{repo.owner}/{repo.name}")
                    label = f"{repo.owner}/{repo.name}#{number}"
                    return self._format_pr_link(repo, int(number), label)
                except Exception:
                    if number is None:
                        return f"repo_id={repo_id}"
                    return f"repo_id={repo_id}#{number}"

            args = self._json_load(getattr(obj, "task_args", None))
            if isinstance(args, list) and len(args) >= 1:
                repo_id = args[0]
                number = args[1] if len(args) >= 2 else None
                try:
                    repo = Repository.objects.only("owner", "name", "id").get(id=int(repo_id))
                    if number is None:
                        return self._format_repo_link(repo, f"{repo.owner}/{repo.name}")
                    label = f"{repo.owner}/{repo.name}#{number}"
                    return self._format_pr_link(repo, int(number), label)
                except Exception:
                    if number is None:
                        return f"repo_id={repo_id}"
                    return f"repo_id={repo_id}#{number}"

            return "-"

        repo_pr.short_description = "Repo/PR"  # type: ignore[attr-defined]

        def changeform_view(  # type: ignore[override]
            self,
            request,
            object_id=None,
            form_url="",
            extra_context=None,
        ):
            extra = extra_context or {}
            obj = self.get_object(request, object_id)
            if obj is not None:
                try:
                    extra["repo_pr_value"] = self.repo_pr(obj)
                except Exception:  # pragma: no cover - best-effort
                    extra["repo_pr_value"] = None
                try:
                    extra["parent_value"] = self.parent_link(obj)
                    extra["root_value"] = self.root_link(obj)
                    children_qs = (
                        TaskResult.objects.filter(link__parent_task_id=obj.task_id)
                        .select_related("link")
                        .order_by("-date_done")[:10]
                    )
                    extra["child_results"] = children_qs
                    changelist = reverse("admin:django_celery_results_taskresult_changelist")
                    extra["child_filter_url"] = f"{changelist}?link__parent_task_id__exact={quote_plus(obj.task_id)}"
                    root_id = getattr(getattr(obj, "link", None), "root_task_id", None)
                    extra["root_filter_url"] = (
                        f"{changelist}?link__root_task_id__exact={quote_plus(root_id)}" if root_id else None
                    )
                    has_root_children = TaskResult.objects.filter(link__root_task_id=obj.task_id).exists()
                    extra["as_root_filter_url"] = (
                        f"{changelist}?link__root_task_id__exact={quote_plus(obj.task_id)}"
                        if obj.task_id and has_root_children
                        else None
                    )
                except Exception:  # pragma: no cover - best-effort
                    extra["parent_value"] = None
                    extra["root_value"] = None
                    extra["child_results"] = None
                    extra["child_filter_url"] = None
                    extra["root_filter_url"] = None
                    extra["as_root_filter_url"] = None
            return super().changeform_view(request, object_id, form_url, extra)

        def get_urls(self):  # type: ignore[override]
            urls = super().get_urls()
            custom = [
                path(
                    "scheduled-tasks/",
                    self.admin_site.admin_view(self.scheduled_tasks_view),
                    name="django_celery_results_taskresult_scheduled_tasks",
                )
            ]
            return custom + urls

        def _format_schedule(self, schedule_obj):  # pragma: no cover - formatting helper
            if schedule_obj is None:
                return "-"
            try:
                human = schedule_obj.human_readable()  # type: ignore[attr-defined]
                if human:
                    return human
            except Exception:
                pass
            return str(schedule_obj)

        def scheduled_tasks_view(self, request):
            schedule_conf = getattr(settings, "CELERY_BEAT_SCHEDULE", {}) or {}
            if request.method == "POST":
                task_key = request.POST.get("task_key")
                entry = schedule_conf.get(task_key)
                if entry:
                    task_name = entry.get("task")
                    args = entry.get("args") or []
                    kwargs = entry.get("kwargs") or {}
                    try:
                        async_res = celery_app.send_task(task_name, args=args, kwargs=kwargs)
                        self.message_user(request, f"Enqueued scheduled task '{task_key}' ({task_name}): {async_res.id}")
                    except Exception as exc:  # pragma: no cover - external dependency
                        self.message_user(request, f"Failed to enqueue '{task_key}': {exc}")
                else:
                    self.message_user(request, f"Unknown scheduled task: {task_key}")
                return HttpResponseRedirect(request.path)

            entries = []
            for key, entry in sorted(schedule_conf.items()):
                schedule_label = self._format_schedule(entry.get("schedule"))
                entries.append(
                    {
                        "key": key,
                        "task": entry.get("task"),
                        "schedule": schedule_label,
                        "args": entry.get("args") or [],
                        "kwargs": entry.get("kwargs") or {},
                    }
                )

            context = {
                **self.admin_site.each_context(request),
                "title": "Scheduled Celery tasks",
                "opts": self.model._meta,
                "entries": entries,
                "taskresult_changelist_url": reverse("admin:django_celery_results_taskresult_changelist"),
            }
            return TemplateResponse(
                request,
                "admin/django_celery_results/taskresult/scheduled_tasks.html",
                context,
            )

except Exception:  # pragma: no cover - django-celery-results not installed
    pass
