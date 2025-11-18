from __future__ import annotations

from typing import Sequence

from django.core.management.base import BaseCommand, CommandError

from core.models import Repository
from syncer.tasks.backfill_tasks import backfill_repo_history_task


class Command(BaseCommand):
    help = "Enqueue a createdAt-based PR history backfill for a repository"

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument(
            "--page-size",
            type=int,
            default=50,
            help="Number of PRs to request per page (max 100)",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=1,
            help="Maximum number of pages to process in this run",
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
        page_size: int = int(opts["page_size"])
        max_pages: int = int(opts["max_pages"])
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
            async_res = backfill_repo_history_task.delay(
                repo.id,
                page_size=page_size,
                max_pages=max_pages,
                states=states,
            )
            self.stdout.write(self.style.SUCCESS(f"Enqueued backfill_repo_history task: id={async_res.id}"))
        else:
            res = backfill_repo_history_task(
                repo.id,
                page_size=page_size,
                max_pages=max_pages,
                states=states,
            )
            self.stdout.write(self.style.SUCCESS(f"Backfill result: {res}"))
