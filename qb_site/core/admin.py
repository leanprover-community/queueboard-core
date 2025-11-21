from __future__ import annotations

from django.contrib import admin
from django.urls import path, reverse
from django.template.response import TemplateResponse
from django import forms
from django.utils.html import format_html
from django.http import HttpResponseRedirect, HttpResponse
from collections import OrderedDict
import json
import ast

from .models import Repository, ReviewerPreference, User
from syncer.models import PullRequest  # type: ignore


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


@admin.register(ReviewerPreference)
class ReviewerPreferenceAdmin(admin.ModelAdmin):
    list_display = (
        "repository",
        "user",
        "maximum_capacity",
        "auto_assign",
        "away_until",
    )
    list_filter = ("auto_assign", "repository")
    search_fields = (
        "user__github_login",
        "repository__owner",
        "repository__name",
    )
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("repository", "user")


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
        list_display = (
            "short_id",
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
        )

        def short_id(self, obj):  # type: ignore[override]
            tid = getattr(obj, "task_id", "") or ""
            return tid[:8]

        short_id.short_description = "ID"  # type: ignore[attr-defined]

        def has_add_permission(self, request):  # type: ignore[override]
            return False

        def task_name_link(self, obj):  # type: ignore[override]
            name = getattr(obj, "task_name", "") or ""
            changelist_url = reverse("admin:django_celery_results_taskresult_changelist")
            # Filter by this task_name; preserve existing GET parameters if any.
            url = f"{changelist_url}?task_name={name}"
            return format_html("<a href='{}'>{}</a>", url, name)

        task_name_link.short_description = "Task name"  # type: ignore[attr-defined]
        task_name_link.admin_order_field = "task_name"  # type: ignore[attr-defined]

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
            name = getattr(obj, "task_name", "") or ""
            if name == "syncer.sync_pr":
                repo, number, label = self._decode_repo_and_number(obj)
                return self._format_pr_link(repo, number, label)
            if name == "analyzer.process_pr":
                # Analyzer per-PR follow-up: decode repo/number from task kwargs/args.
                repo, number, label = self._decode_repo_and_number(obj)
                return self._format_pr_link(repo, number, label)
            if name == "syncer.harvest_commit_history":
                repo, number, label = self._decode_repo_and_number(obj)
                return self._format_pr_link(repo, number, label)
            if name == "syncer.sync_repo_since":
                res = self._json_load(getattr(obj, "result", None))
                since = res.get("since") if isinstance(res, dict) else None
                repo, base_label = self._decode_repo_only(obj)
                label = base_label
                if since and base_label and base_label != "-":
                    label = f"{base_label} (since {since})"
                return self._format_repo_link(repo, label)
            if name == "syncer.sync_ci_for_shas":
                # CI-by-SHA tasks: show owner/name#number and link to the PR/admin when possible.
                res = self._json_load(getattr(obj, "result", None))
                if isinstance(res, dict) and res.get("repo") and res.get("number") is not None:
                    repo_label = f"{res.get('repo')}#{res.get('number')}"
                    try:
                        owner, name_part = str(res.get("repo")).split("/", 1)
                        number = int(res.get("number"))
                        repo = Repository.objects.only("id").get(owner=owner, name=name_part)
                        pr = PullRequest.objects.filter(repository=repo, number=number).only("id").first()
                        if pr is not None:
                            url = reverse("admin:syncer_pullrequest_change", args=[pr.pk])
                            return format_html("<a href='{}'>{}</a>", url, repo_label)
                    except Exception:  # pragma: no cover
                        return repo_label
                    return repo_label
                # Fallback to kwargs (repo_id, number)
                kwargs = self._json_load(getattr(obj, "task_kwargs", None))
                if isinstance(kwargs, dict) and kwargs.get("repo_id") is not None and kwargs.get("number") is not None:
                    repo_id = kwargs.get("repo_id")
                    number = kwargs.get("number")
                    try:
                        repo = Repository.objects.only("owner", "name", "id").get(id=int(repo_id))
                        label = f"{repo.owner}/{repo.name}#{number}"
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
                    except Exception:  # pragma: no cover
                        return f"repo_id={repo_id}#{number}"
                # Fallback to args (repo_id, number)
                args = self._json_load(getattr(obj, "task_args", None))
                if isinstance(args, list) and len(args) >= 2:
                    repo_id, number = args[0], args[1]
                    try:
                        repo = Repository.objects.only("owner", "name", "id").get(id=int(repo_id))
                        label = f"{repo.owner}/{repo.name}#{number}"
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
                    except Exception:  # pragma: no cover
                        return f"repo_id={repo_id}#{number}"
                return "-"
            if name in {
                "syncer.backfill_repo_history",
                "syncer.backfill_repo_incomplete_prs",
                "syncer.refresh_pending_ci_for_repo",
            }:
                repo, label = self._decode_repo_only(obj)
                return self._format_repo_link(repo, label)
            if name == "syncer.sync_ci_for_shas":
                repo, number, label = self._decode_repo_and_number(obj)
                return self._format_pr_link(repo, number, label)
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
            return super().changeform_view(request, object_id, form_url, extra)

except Exception:  # pragma: no cover - django-celery-results not installed
    pass
