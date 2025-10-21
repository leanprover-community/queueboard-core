from __future__ import annotations

from typing import List

from django.core.management.base import BaseCommand, CommandError

from core.models import Repository
from syncer.services.github_client import GitHubClient
from syncer.services.pr_sync_service import PRSyncService


class Command(BaseCommand):
    help = "Sync one or more pull requests for a repository via GraphQL"

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument("--number", action="append", type=int, help="PR number to sync (repeatable)")
        parser.add_argument("--timelineK", type=int, default=150, help="Max timeline items per PR bundle")
        parser.add_argument("--commitsM", type=int, default=15, help="Number of head commits to inspect")
        parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
        parser.add_argument(
            "--create-missing-repo-default-branch",
            default="master",
            help="Default branch to use when creating a missing repository row",
        )

    def handle(self, *args, **opts):  # type: ignore[override]
        owner_name: str = opts["repo"]
        numbers: List[int] = opts.get("number") or []
        timelineK: int = opts["timelineK"]
        commitsM: int = opts["commitsM"]
        dry_run: bool = bool(opts["dry_run"])  # not persisted if True
        default_branch: str = opts["create_missing_repo_default_branch"]

        if "/" not in owner_name:
            raise CommandError("--repo must be in the form owner/name")
        owner, name = owner_name.split("/", 1)

        if not numbers:
            raise CommandError("Provide at least one --number for now (changed PR discovery to be added)")

        repo = Repository.objects.filter(owner=owner, name=name).first()
        if repo is None:
            repo = Repository(owner=owner, name=name, default_branch=default_branch, is_active=True)
            repo.save()

        try:
            client = GitHubClient()
        except RuntimeError as e:
            raise CommandError(str(e))

        svc = PRSyncService()

        for num in numbers:
            res = svc.sync_pull_request(
                repo,
                number=int(num),
                client=client,
                timelineK=timelineK,
                commitsM=commitsM,
                dry_run=dry_run,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Synced PR #{num}: labels +{res['labels_created']}/~{res['labels_updated']} "
                    f"attachments +{res['prlabels_created']}/-{res['prlabels_deleted']}; "
                    f"events +{res['events_created']}; checkruns +{res['checkruns_upserted']}; "
                    f"statusctx +{res['statusctx_upserted']}"
                )
            )
