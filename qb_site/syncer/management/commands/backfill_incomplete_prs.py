from __future__ import annotations

from typing import Sequence

from django.core.management.base import BaseCommand, CommandError

from core.models import Repository
from syncer.tasks.backfill_tasks import backfill_repo_incomplete_prs_task


class Command(BaseCommand):
    help = "Enqueue an incomplete-PR backfill for a repository"

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum number of incomplete PRs to enqueue in this run",
        )
        parser.add_argument(
            "--states",
            action="append",
            help="Repeatable PR states to include (OPEN, MERGED, CLOSED). Default: OPEN,MERGED,CLOSED",
        )
        parser.add_argument(
            "--async",
            dest="use_async",
            action="store_true",
            help="Enqueue the Celery task asynchronously instead of running in-process",
        )

    def handle(self, *args, **opts):  # type: ignore[override]
        owner_name: str = opts["repo"]
        limit: int = int(opts["limit"])
        states_opt: Sequence[str] | None = opts.get("states")
        use_async: bool = bool(opts.get("use_async"))

        if "/" not in owner_name:
            raise CommandError("--repo must be in the form owner/name")
        owner, name = owner_name.split("/", 1)

        repo = Repository.objects.filter(owner=owner, name=name).first()
        if repo is None:
            raise CommandError(f"Repository not found: {owner}/{name}")

        if states_opt:
            states = [s.upper() for s in states_opt]
            allowed = {"OPEN", "MERGED", "CLOSED"}
            invalid = [s for s in states if s not in allowed]
            if invalid:
                raise CommandError(f"Invalid --states: {', '.join(invalid)}; allowed: OPEN, MERGED, CLOSED")
        else:
            states = None

        if use_async:
            async_res = backfill_repo_incomplete_prs_task.delay(
                repo.id,
                limit=limit,
                states=states,
            )
            self.stdout.write(self.style.SUCCESS(f"Enqueued backfill_repo_incomplete_prs task: id={async_res.id}"))
        else:
            res = backfill_repo_incomplete_prs_task(
                repo.id,
                limit=limit,
                states=states,
            )
            self.stdout.write(self.style.SUCCESS(f"Incomplete PR backfill result: {res}"))
