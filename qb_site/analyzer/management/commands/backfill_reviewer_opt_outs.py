from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from analyzer.services.reviewer_opt_out_backfill import backfill_reviewer_opt_outs
from core.models import Repository


class Command(BaseCommand):
    help = "Backfill reviewer opt-outs from assignment/unassignment timeline events"

    def add_arguments(self, parser):
        parser.add_argument("--repo", help="Repository in owner/name form", default=None)
        parser.add_argument("--all-prs", action="store_true", help="Include closed/merged PRs (default: open only)")
        parser.add_argument(
            "--allow-incomplete",
            action="store_true",
            help="Include PRs without timeline_backfill_done",
        )
        parser.add_argument(
            "--cutoff-days",
            type=int,
            default=0,
            help="Only consider assignment events within this many days (default: 0 means no cutoff).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Compute counts without writing changes")

    def handle(self, *args, **options):
        repo_arg = options.get("repo")
        repo = None
        if repo_arg:
            if "/" not in repo_arg:
                raise CommandError("Expected --repo in owner/name form")
            owner, name = repo_arg.split("/", 1)
            repo = Repository.objects.filter(owner=owner, name=name).first()
            if repo is None:
                raise CommandError(f"Repository not found: {repo_arg}")

        only_open = not bool(options.get("all_prs"))
        require_complete = not bool(options.get("allow_incomplete"))
        cutoff_days = int(options.get("cutoff_days") or 0)
        if cutoff_days <= 0:
            cutoff_days = None

        result = backfill_reviewer_opt_outs(
            repository=repo,
            only_open=only_open,
            require_complete=require_complete,
            cutoff_days=cutoff_days,
            dry_run=bool(options.get("dry_run")),
        )
        self.stdout.write(result.summary())
