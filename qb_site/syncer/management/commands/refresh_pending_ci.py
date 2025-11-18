from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import Repository
from syncer.tasks.sync_tasks import refresh_pending_ci_for_repo_task


class Command(BaseCommand):
    help = "Enqueue a pending-CI refresh for a repository (re-poll pending CheckRuns/StatusContexts)."

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument(
            "--max-prs",
            type=int,
            default=20,
            help="Maximum number of PRs with pending CI to consider in this run",
        )
        parser.add_argument(
            "--max-shas-per-pr",
            type=int,
            default=5,
            help="Maximum number of SHAs per PR to refresh CI for",
        )
        parser.add_argument(
            "--max-pending-hours",
            type=int,
            default=None,
            help="Stop polling CI rows that GitHub has reported as pending for longer than this many hours "
            "(default drawn from SYNCER_PENDING_CI_MAX_AGE_HOURS)",
        )
        parser.add_argument(
            "--async",
            dest="use_async",
            action="store_true",
            help="Enqueue the Celery task asynchronously instead of running in-process",
        )

    def handle(self, *args, **opts):  # type: ignore[override]
        owner_name: str = opts["repo"]
        max_prs: int = int(opts["max_prs"])
        max_shas_per_pr: int = int(opts["max_shas_per_pr"])
        max_pending_hours_opt = opts.get("max_pending_hours")
        use_async: bool = bool(opts.get("use_async"))

        if "/" not in owner_name:
            raise CommandError("--repo must be in the form owner/name")
        owner, name = owner_name.split("/", 1)

        repo = Repository.objects.filter(owner=owner, name=name).first()
        if repo is None:
            raise CommandError(f"Repository not found: {owner}/{name}")

        max_pending_hours = int(max_pending_hours_opt) if max_pending_hours_opt is not None else None

        if use_async:
            async_res = refresh_pending_ci_for_repo_task.delay(
                repo.id,
                max_prs=max_prs,
                max_shas_per_pr=max_shas_per_pr,
                max_pending_hours=max_pending_hours,
            )
            self.stdout.write(self.style.SUCCESS(f"Enqueued refresh_pending_ci_for_repo task: id={async_res.id}"))
        else:
            res = refresh_pending_ci_for_repo_task(
                repo.id,
                max_prs=max_prs,
                max_shas_per_pr=max_shas_per_pr,
                max_pending_hours=max_pending_hours,
            )
            self.stdout.write(self.style.SUCCESS(f"Pending-CI refresh result: {res}"))
