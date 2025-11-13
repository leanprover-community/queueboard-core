from __future__ import annotations

from django.contrib import admin
from django.urls import path, reverse
from django.template.response import TemplateResponse
from django import forms
from django.utils.html import format_html
from django.http import HttpResponseRedirect, HttpResponse
from collections import OrderedDict
import json

from .models import Repository, ReviewerPreference, User


@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = (
        "owner",
        "name",
        "sync_tools_link",
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

        submitted_action = None
        enqueued = []
        error = None
        notice = None
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
                        for n in nums:
                            res = sync_pr_task.delay(repo.id, int(n), timelineK=timelineK, commitsM=commitsM, dry_run=dry_run)
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

    # Convenience action to open the tools page from the changelist
    actions = ["open_sync_tools_action"]

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

    # Quick actions to toggle repository active state for scheduling
    actions = ["open_sync_tools_action", "mark_active_action", "mark_inactive_action"]

    def mark_active_action(self, request, queryset):  # type: ignore[override]
        updated = queryset.update(is_active=True)
        self.message_user(request, f"Marked {updated} repositories as active.")

    mark_active_action.short_description = "Mark selected repositories as ACTIVE"  # type: ignore[attr-defined]

    def mark_inactive_action(self, request, queryset):  # type: ignore[override]
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Marked {updated} repositories as inactive.")

    mark_inactive_action.short_description = "Mark selected repositories as INACTIVE"  # type: ignore[attr-defined]

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
        for key in ("open_sync_tools_action", "mark_active_action", "mark_inactive_action"):
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
            "task_name",
            "repo_pr",
            "status",
            "date_done",
        )
        list_display_links = ("short_id", "task_name")
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

        def _json_load(self, raw):  # pragma: no cover - trivial helper
            if raw is None:
                return None
            if isinstance(raw, (dict, list)):
                return raw
            try:
                return json.loads(raw)
            except Exception:
                return None

        def repo_pr(self, obj):  # type: ignore[override]
            name = getattr(obj, "task_name", "") or ""
            if name == "syncer.sync_pr":
                res = self._json_load(getattr(obj, "result", None))
                if isinstance(res, dict) and res.get("repo") and res.get("number") is not None:
                    return f"{res.get('repo')}#{res.get('number')}"
                # Fallback to args (repo_id, number)
                args = self._json_load(getattr(obj, "task_args", None))
                if isinstance(args, list) and len(args) >= 2:
                    repo_id, number = args[0], args[1]
                    try:
                        repo = Repository.objects.only("owner", "name").get(id=int(repo_id))
                        return f"{repo.owner}/{repo.name}#{number}"
                    except Exception:  # pragma: no cover - best-effort
                        return f"repo_id={repo_id}#{number}"
                return "-"
            if name == "syncer.sync_repo_since":
                res = self._json_load(getattr(obj, "result", None))
                if isinstance(res, dict) and res.get("repo"):
                    since = res.get("since")
                    return f"{res.get('repo')} (since {since})"
                # Fallback to kwargs
                kwargs = self._json_load(getattr(obj, "task_kwargs", None))
                if isinstance(kwargs, dict) and kwargs.get("repo_id"):
                    try:
                        repo = Repository.objects.only("owner", "name").get(id=int(kwargs["repo_id"]))
                        return f"{repo.owner}/{repo.name}"
                    except Exception:  # pragma: no cover
                        return f"repo_id={kwargs.get('repo_id')}"
                return "-"
            return "-"

        repo_pr.short_description = "Repo/PR"  # type: ignore[attr-defined]

except Exception:  # pragma: no cover - django-celery-results not installed
    pass
