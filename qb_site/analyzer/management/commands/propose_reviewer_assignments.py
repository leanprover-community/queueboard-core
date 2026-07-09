from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from analyzer.tasks.reviewer_assignment_propose import propose_reviewer_assignments_task
from core.models import Repository


class Command(BaseCommand):
    help = (
        "Propose reviewer assignments through the acceptance gate from the latest default-rule-set "
        "ReviewerAssignmentSnapshot: confirm-mode reviewers get an AssignmentProposal; auto-mode "
        "(and confirm-mode reviewers with no Zulip link) are direct-assigned. Use --dry-run to "
        "inspect the would-propose/would-assign set without any side effect."
    )

    def add_arguments(self, parser):
        parser.add_argument("--repo", help="Restrict to a single repository in owner/name form", default=None)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute would-do outcomes without creating proposals or POSTing assignees.",
        )
        parser.add_argument(
            "--enable",
            action="store_true",
            help="Force enabled=True for this run regardless of ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED.",
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
        # Explicit flags override; absent them, fall back to settings for BOTH knobs so a configured
        # ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN safety net is honored when run bare. --enable forces a
        # real run (dry-run off); --dry-run forces preview.
        enabled_override = True if enable else (False if dry_run else None)
        dry_run_override = True if dry_run else (False if enable else None)

        result = propose_reviewer_assignments_task.run(
            repository_id=repository_id,
            include_inactive_repositories=bool(options.get("include_inactive")),
            enabled_override=enabled_override,
            dry_run_override=dry_run_override,
        )
        self.stdout.write(json.dumps(result, indent=2, default=str))
