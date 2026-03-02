from __future__ import annotations

from typing import List, Optional

from django.core.management.base import BaseCommand, CommandError

from analyzer.services.queue_window_build_state import backfill_queue_window_build_states_for_repo
from core.models import Repository


class Command(BaseCommand):
    help = "Backfill analyzer.PRQueueWindowBuildState rows for active rulesets from legacy PR-level window build fields."

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument(
            "--pr",
            nargs="*",
            type=int,
            default=None,
            help="Optional list of PR numbers to restrict the operation",
        )
        parser.add_argument(
            "--write",
            action="store_true",
            default=False,
            help="Persist changes. Without this flag, command is dry-run only.",
        )

    def handle(self, *args, **options):  # type: ignore[override]
        repo_str: str = options["repo"]
        pr_numbers: Optional[List[int]] = options["pr"]
        write: bool = bool(options["write"])

        if "/" not in repo_str:
            raise CommandError("--repo must be in 'owner/name' format")
        owner, name = repo_str.split("/", 1)
        repo = Repository.objects.filter(owner=owner, name=name).first()
        if not repo:
            raise CommandError(f"Repository not found: {owner}/{name}")

        result = backfill_queue_window_build_states_for_repo(
            repository=repo,
            pr_numbers=pr_numbers,
            dry_run=not write,
        )
        self.stdout.write(self.style.MIGRATE_HEADING(f"Backfill queue-window build state for {owner}/{name}"))
        self.stdout.write(f" - prs_considered: {result.prs_considered}")
        self.stdout.write(f" - rows_created: {result.rows_created}")
        self.stdout.write(f" - rows_updated: {result.rows_updated}")
        if write:
            self.stdout.write(self.style.SUCCESS("Write mode complete."))
        else:
            self.stdout.write(self.style.WARNING("Dry-run only. Re-run with --write to persist."))
