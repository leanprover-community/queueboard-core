from __future__ import annotations

from typing import List

from django.core.management.base import BaseCommand, CommandError

from core.models import Repository
from syncer.tasks.sync_tasks import sync_repo_since_task


class Command(BaseCommand):
    help = "Enqueue a repository-level sync task (Celery)"

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument("--since", help="ISO8601 cutoff; default uses sliding lookback from settings")
        parser.add_argument("--limit", type=int, help="Max PRs to discover for this run (overrides settings)")
        parser.add_argument(
            "--states", action="append", help="Repeatable PR states for discovery (OPEN, MERGED, CLOSED). Default from settings"
        )
        parser.add_argument("--timelineK", type=int, help="Max timeline items per PR bundle (defaults from settings)")
        parser.add_argument("--commitsM", type=int, help="Number of head commits to inspect (defaults from settings)")
        parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing (passed to per-PR task)")
        parser.add_argument(
            "--create-missing-repo-default-branch",
            default="master",
            help="Default branch to use when creating a missing repository row",
        )

    def handle(self, *args, **opts):  # type: ignore[override]
        owner_name: str = opts["repo"]
        if "/" not in owner_name:
            raise CommandError("--repo must be in the form owner/name")
        owner, name = owner_name.split("/", 1)

        since = opts.get("since")
        limit = opts.get("limit")
        states_opt: List[str] | None = opts.get("states")
        timelineK = opts.get("timelineK")
        commitsM = opts.get("commitsM")
        dry_run: bool = bool(opts.get("dry_run") or False)
        default_branch: str = opts["create_missing_repo_default_branch"]

        repo = Repository.objects.filter(owner=owner, name=name).first()
        if repo is None:
            repo = Repository(owner=owner, name=name, default_branch=default_branch, is_active=True)
            repo.save()

        # Normalize states (if provided)
        states: List[str] | None = None
        if states_opt:
            allowed = {"OPEN", "MERGED", "CLOSED"}
            states = [s.upper() for s in states_opt]
            invalid = [s for s in states if s not in allowed]
            if invalid:
                raise CommandError(f"Invalid --states: {', '.join(invalid)}; allowed: OPEN, MERGED, CLOSED")

        async_result = sync_repo_since_task.delay(
            repo.id,
            since_iso=since,
            limit=limit,
            states=states,
            timelineK=timelineK,
            commitsM=commitsM,
            dry_run=dry_run,
        )
        self.stdout.write(self.style.SUCCESS(f"Enqueued sync_repo_since for {owner_name}: task_id={async_result.id}"))
