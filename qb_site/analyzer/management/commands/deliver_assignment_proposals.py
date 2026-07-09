from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from analyzer.tasks.assignment_proposal_delivery import deliver_assignment_proposals_task
from core.models import Repository


class Command(BaseCommand):
    help = (
        "Send the per-reviewer assignment-proposal digest DM (design doc 050): one Zulip DM per "
        "confirm-mode reviewer listing their pending, not-yet-notified proposals with a console link. "
        "Dedupe is carried by AssignmentProposal.notified_at. Use --dry-run to inspect the would-send "
        "set without sending or stamping notified_at."
    )

    def add_arguments(self, parser):
        parser.add_argument("--repo", help="Restrict to a single repository in owner/name form", default=None)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute the would-send set without sending DMs or stamping notified_at.",
        )
        parser.add_argument(
            "--enable",
            action="store_true",
            help="Force a real send regardless of the ANALYZER_ASSIGNMENT_PROPOSALS_* flags.",
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Include repositories with is_active=False.",
        )

    def handle(self, *args, **options):
        repository_id = None
        repo_arg = options.get("repo")
        if repo_arg:
            if "/" not in repo_arg:
                raise CommandError("Expected --repo in owner/name form")
            owner, name = repo_arg.split("/", 1)
            repo = Repository.objects.filter(owner=owner, name=name).only("id").first()
            if repo is None:
                raise CommandError(f"Repository not found: {repo_arg}")
            repository_id = int(repo.id)

        dry_run = bool(options.get("dry_run"))
        enable = bool(options.get("enable"))
        # Explicit flags override; absent them, fall back to settings. --enable forces a real send
        # (dry-run off); --dry-run forces preview.
        enabled_override = True if enable else (False if dry_run else None)
        dry_run_override = True if dry_run else (False if enable else None)

        result = deliver_assignment_proposals_task.run(
            repository_id=repository_id,
            include_inactive_repositories=bool(options.get("include_inactive")),
            enabled_override=enabled_override,
            dry_run_override=dry_run_override,
        )
        self.stdout.write(json.dumps(result, indent=2, default=str))
