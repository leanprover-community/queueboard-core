from __future__ import annotations

from django.contrib import admin
from django.urls import path, reverse
from django.template.response import TemplateResponse
from django import forms
from django.utils.html import format_html
from django.http import HttpResponseRedirect, HttpResponse

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

    class DiscoverForm(forms.Form):
        since = forms.CharField(
            label="Since (ISO8601)",
            help_text="e.g., 2025-10-20T00:00:00Z or 2025-10-20",
            widget=forms.TextInput(attrs={"size": 30}),
        )
        states = forms.MultipleChoiceField(
            label="States",
            required=False,
            initial=["OPEN"],
            choices=[("OPEN", "OPEN"), ("MERGED", "MERGED"), ("CLOSED", "CLOSED")],
            widget=forms.CheckboxSelectMultiple,
        )
        limit = forms.IntegerField(label="Limit", required=False, initial=20, min_value=1, max_value=200)
        dry_run = forms.BooleanField(label="Dry-run", required=False, initial=False)
        timelineK = forms.IntegerField(label="timelineK", required=False, initial=150, min_value=1)
        commitsM = forms.IntegerField(label="commitsM", required=False, initial=15, min_value=1)

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
        prs_initial = ""
        if request.method == "POST":
            submitted_action = request.POST.get("action")
            if submitted_action == "enqueue_prs":
                form = self.SyncPRsForm(request.POST)
                discover_form = self.DiscoverForm()
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
            elif submitted_action == "discover_sync":
                form = self.SyncPRsForm()
                discover_form = self.DiscoverForm(request.POST)
                if discover_form.is_valid():
                    from syncer.services.github_client import GitHubClient
                    from syncer.tasks.sync_tasks import sync_pr_task

                    since = discover_form.cleaned_data["since"]
                    states = discover_form.cleaned_data.get("states") or ["OPEN"]
                    limit = discover_form.cleaned_data.get("limit") or 20
                    dry_run = bool(discover_form.cleaned_data.get("dry_run") or False)
                    timelineK = discover_form.cleaned_data.get("timelineK") or 150
                    commitsM = discover_form.cleaned_data.get("commitsM") or 15
                    try:
                        gh = GitHubClient()
                        numbers = gh.get_changed_pr_numbers(
                            owner=repo.owner, name=repo.name, since_iso=since, states=states, limit=int(limit)
                        )
                        if not numbers:
                            error = "No changed PRs discovered for the given cutoff/states"
                        else:
                            for n in numbers:
                                res = sync_pr_task.delay(repo.id, int(n), timelineK=timelineK, commitsM=commitsM, dry_run=dry_run)
                                enqueued.append((n, res.id))
                    except Exception as e:  # pragma: no cover - external dependency
                        error = f"Discovery failed: {e}"
                else:
                    error = "Invalid discovery form"
            else:
                form = self.SyncPRsForm()
                discover_form = self.DiscoverForm()
        else:
            form = self.SyncPRsForm()
            discover_form = self.DiscoverForm()

        context = {
            **self.admin_site.each_context(request),
            "title": f"Sync tools for {repo}",
            "repo": repo,
            "form": form,
            "discover_form": discover_form,
            "enqueued": enqueued,
            "error": error,
            "submitted_action": submitted_action,
            "prs_initial": prs_initial,
            "changelist_url": reverse("admin:core_repository_changelist"),
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
