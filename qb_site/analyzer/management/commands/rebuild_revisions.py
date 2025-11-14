from __future__ import annotations

from typing import List, Optional

from django.core.management.base import BaseCommand, CommandError

from core.models import Repository
from syncer.models import PullRequest
from analyzer.services.revisions import rebuild_pr_revisions


class Command(BaseCommand):
    help = (
        "Rebuild PR head revision windows from timeline events.\n"
        "Requires timeline backfill to be complete for each PR; otherwise the PR is skipped."
    )

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument(
            "--pr",
            nargs="*",
            type=int,
            default=None,
            help="Optional list of PR numbers to restrict the operation",
        )

    def handle(self, *args, **options):  # type: ignore[override]
        repo_str: str = options["repo"]
        pr_numbers: Optional[List[int]] = options["pr"]

        if "/" not in repo_str:
            raise CommandError("--repo must be in 'owner/name' format")
        owner, name = repo_str.split("/", 1)
        repo = Repository.objects.filter(owner=owner, name=name).first()
        if not repo:
            raise CommandError(f"Repository not found: {owner}/{name}")

        qs = PullRequest.objects.filter(repository=repo)
        if pr_numbers:
            qs = qs.filter(number__in=pr_numbers)
        qs = qs.only("id", "number", "timeline_backfill_done")

        if not qs.exists():
            self.stdout.write(self.style.WARNING("No matching PRs found."))
            return

        total_created = 0
        total_deleted = 0
        processed = 0

        self.stdout.write(self.style.MIGRATE_HEADING(f"Rebuild PR revisions for {owner}/{name}"))
        for pr in qs:
            if not pr.timeline_backfill_done:
                self.stdout.write(f" - PR #{pr.number}: skip (timeline backfill not complete)")
                continue
            res = rebuild_pr_revisions(pr)
            processed += 1
            total_created += int(res.created)
            total_deleted += int(res.deleted)
            self.stdout.write(f" - PR #{pr.number}: created={res.created} deleted={res.deleted}")

        self.stdout.write(
            self.style.SUCCESS(f"Processed {processed} PR(s); revisions created={total_created}, deleted={total_deleted}")
        )
