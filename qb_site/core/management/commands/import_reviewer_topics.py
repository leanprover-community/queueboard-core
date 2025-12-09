from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.services.reviewer_topics_importer import (
    DEFAULT_REPO,
    ReviewerTopicsImportError,
    import_reviewer_topics,
)


class Command(BaseCommand):
    help = "Import reviewer preferences from reviewer-topics.json into ReviewerPreference rows"

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument(
            "--repo",
            default=DEFAULT_REPO,
            help=f"Target repository in the form owner/name (default: {DEFAULT_REPO})",
        )
        parser.add_argument(
            "--path",
            default=str(Path("reviewer-topics.json")),
            help="Path to reviewer-topics.json (default: reviewer-topics.json)",
        )
        parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--replace-labels",
            dest="replace_labels",
            action="store_true",
            help="Replace preferred_labels with the file's list (default)",
        )
        group.add_argument(
            "--merge",
            dest="replace_labels",
            action="store_false",
            help="Merge preferred_labels instead of replacing",
        )
        parser.set_defaults(replace_labels=True)
        parser.add_argument(
            "--create-missing-users",
            dest="create_missing_users",
            action="store_true",
            default=True,
            help="Create User rows for unknown GitHub logins (default: true)",
        )
        parser.add_argument(
            "--no-create-missing-users",
            dest="create_missing_users",
            action="store_false",
            help="Do not create users for unknown GitHub logins",
        )
        parser.add_argument(
            "--create-missing-repo-default-branch",
            default="master",
            help="When creating the repository row, use this as default_branch (default: master)",
        )
        parser.add_argument("--verbose", action="store_true", help="Print per-entry changes")

    def handle(self, *args, **options):  # type: ignore[override]
        repo_arg: str = options["repo"]
        path_arg: str = options["path"]
        dry_run: bool = options["dry_run"]
        replace_labels: bool = options["replace_labels"]
        create_missing_users: bool = options["create_missing_users"]
        create_repo_branch: str = options["create_missing_repo_default_branch"]
        verbose: bool = options["verbose"]

        try:
            result = import_reviewer_topics(
                repo=repo_arg,
                file_obj=Path(path_arg),
                replace_labels=replace_labels,
                dry_run=dry_run,
                create_missing_users=create_missing_users,
                create_missing_repo_default_branch=create_repo_branch,
                verbose=verbose,
                logger=self.stdout.write if verbose else None,
            )
        except ReviewerTopicsImportError as exc:
            raise CommandError(str(exc))

        if result.repo_created:
            self.stdout.write(self.style.SUCCESS(f"Repository {result.owner}/{result.name} created"))
        self.stdout.write(self.style.SUCCESS(result.summary_text()))
