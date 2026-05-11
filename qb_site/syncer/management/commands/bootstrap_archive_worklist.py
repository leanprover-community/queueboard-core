"""Bootstrap the ArchiveImportItem worklist for one archive repo.

See ``docs/design-decisions/043-archive-repo-backfill-importer.md`` Commit 2.

Usage:

    python qb_site/manage.py bootstrap_archive_worklist \
        --archive queueboard-archive2 \
        --repo leanprover-community/mathlib4

To enroll the older archive in "diff mode" against an archive that has
already been imported (only enroll PR numbers not successfully completed
from the other archive)::

    python qb_site/manage.py bootstrap_archive_worklist \
        --archive queueboard-archive \
        --repo leanprover-community/mathlib4 \
        --diff-against queueboard-archive2

Idempotent: re-running is a no-op for rows already present.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Repository
from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.services.archive_bootstrap import enumerate_archive_pr_entries


_DEFAULT_ARCHIVE_OWNER = "leanprover-community"


class Command(BaseCommand):
    help = "Enroll per-PR worklist rows for an archive backfill repo (design doc 043)."

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument(
            "--archive",
            required=True,
            help="Archive repo name (e.g. queueboard-archive2). Owner defaults to leanprover-community.",
        )
        parser.add_argument(
            "--repo",
            required=True,
            help="Live target repository in owner/name form (e.g. leanprover-community/mathlib4).",
        )
        parser.add_argument(
            "--archive-owner",
            default=_DEFAULT_ARCHIVE_OWNER,
            help="Owner of the archive repo (default: %(default)s).",
        )
        parser.add_argument(
            "--branch",
            default="master",
            help="Branch / ref to read the data/ tree from (default: %(default)s).",
        )
        parser.add_argument(
            "--diff-against",
            default=None,
            help=(
                "Other archive_name to diff against. When set, only enroll PR numbers that are NOT "
                "already completed from that archive (i.e. archive2 has unsuccessful or no rows for them)."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Optional cap on the number of rows to enroll (useful for staging dry runs).",
        )

    def handle(self, *args, **opts):  # type: ignore[override]
        archive_name: str = opts["archive"]
        archive_owner: str = opts["archive_owner"]
        repo_owner_name: str = opts["repo"]
        branch: str = opts["branch"]
        diff_against: str | None = opts.get("diff_against")
        limit: int | None = opts.get("limit")

        if "/" not in repo_owner_name:
            raise CommandError("--repo must be in the form owner/name")
        owner, name = repo_owner_name.split("/", 1)
        repo = Repository.objects.filter(owner=owner, name=name).first()
        if repo is None:
            raise CommandError(f"Repository not found: {owner}/{name}")

        self.stdout.write(f"Enumerating {archive_owner}/{archive_name}@{branch} data/ tree via git/trees REST...")
        try:
            entries = enumerate_archive_pr_entries(owner=archive_owner, archive=archive_name, branch=branch)
        except Exception as exc:
            raise CommandError(f"Failed to enumerate {archive_owner}/{archive_name}: {exc}") from exc

        self.stdout.write(f"Found {len(entries)} per-PR directories in archive.")

        skip_pr_numbers: set[int] = set()
        if diff_against:
            skip_pr_numbers = set(
                ArchiveImportItem.objects.filter(
                    repository=repo,
                    archive_name=diff_against,
                    status=ArchiveImportItemStatus.COMPLETED,
                ).values_list("pr_number", flat=True)
            )
            self.stdout.write(f"Diff mode: skipping {len(skip_pr_numbers)} PR(s) already completed from {diff_against}.")

        rows_to_create: list[ArchiveImportItem] = []
        for entry in entries:
            if entry.pr_number in skip_pr_numbers:
                continue
            rows_to_create.append(
                ArchiveImportItem(
                    repository=repo,
                    archive_name=archive_name,
                    pr_number=entry.pr_number,
                    archive_path=entry.archive_path,
                    archive_blob_sha=entry.blob_sha,
                    status=ArchiveImportItemStatus.PENDING,
                )
            )
            if limit is not None and len(rows_to_create) >= limit:
                break

        if not rows_to_create:
            self.stdout.write(self.style.SUCCESS("Nothing to enroll."))
            return

        with transaction.atomic():
            created = ArchiveImportItem.objects.bulk_create(rows_to_create, ignore_conflicts=True)

        # bulk_create with ignore_conflicts on Postgres returns objects without
        # PKs for skipped rows; recompute the actual insert count by diffing
        # the table state. The doc's invariant is "re-running is a no-op", so
        # surfacing the inserted-vs-considered split is the operator's signal.
        considered = len(rows_to_create)
        present_after = ArchiveImportItem.objects.filter(
            repository=repo,
            archive_name=archive_name,
            pr_number__in=[r.pr_number for r in rows_to_create],
        ).count()
        # Anything in the considered set that is now present and was newly
        # inserted is bounded by ``considered``; the precise insert count is
        # not directly available from bulk_create with ignore_conflicts, so
        # we report the per-archive worklist totals instead.
        total_for_archive = ArchiveImportItem.objects.filter(repository=repo, archive_name=archive_name).count()
        pending_for_archive = ArchiveImportItem.objects.filter(
            repository=repo,
            archive_name=archive_name,
            status=ArchiveImportItemStatus.PENDING,
        ).count()
        self.stdout.write(
            self.style.SUCCESS(
                "Enrollment complete. "
                f"considered={considered} "
                f"present_after={present_after} "
                f"created_objects_returned={len(created)} "
                f"total_for_archive={total_for_archive} "
                f"pending_for_archive={pending_for_archive}"
            )
        )
