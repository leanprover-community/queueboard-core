from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from analyzer.tasks.reviewer_assignment_apply import apply_reviewer_assignments_task
from core.models import Repository


class Command(BaseCommand):
    help = (
        "Apply proposed reviewer assignments to GitHub from the latest default-rule-set "
        "ReviewerAssignmentSnapshot. Use --dry-run to preview/record without mutating "
        "(handy for a pre-cutover parity check against the legacy automatic_assignments.json)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--repo", help="Restrict to a single repository in owner/name form", default=None)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute + record outcomes without POSTing assignees to GitHub.",
        )
        parser.add_argument(
            "--enable",
            action="store_true",
            help="Force enabled=True for this run regardless of ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED.",
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
        enabled_override = True if options.get("enable") else (False if dry_run else None)

        result = apply_reviewer_assignments_task.run(
            repository_id=repository_id,
            include_inactive_repositories=bool(options.get("include_inactive")),
            enabled_override=enabled_override,
            dry_run_override=dry_run,
        )
        self.stdout.write(json.dumps(result, indent=2, default=str))
