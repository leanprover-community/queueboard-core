"""Operator-friendly status for the archive backfill worklist (design doc 043).

Usage:

    python qb_site/manage.py archive_import_status
    python qb_site/manage.py archive_import_status --repo leanprover-community/mathlib4
    python qb_site/manage.py archive_import_status --errors 10

Prints per-archive counts grouped by status, the oldest still-pending row
per archive, and a sample of recent error messages — the kind of summary
operators want during the multi-day worklist drain without writing SQL.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from core.models import Repository
from syncer.models import ArchiveImportItem, ArchiveImportItemStatus


_STATUS_ORDER = (
    ArchiveImportItemStatus.PENDING,
    ArchiveImportItemStatus.IN_PROGRESS,
    ArchiveImportItemStatus.COMPLETED,
    ArchiveImportItemStatus.FAILED_TRANSIENT,
    ArchiveImportItemStatus.FAILED_PERMANENT,
    ArchiveImportItemStatus.SKIPPED,
)


class Command(BaseCommand):
    help = "Print archive backfill worklist status (design doc 043)."

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument(
            "--repo",
            default=None,
            help="Filter to a single repository in owner/name form (default: all).",
        )
        parser.add_argument(
            "--errors",
            type=int,
            default=5,
            help="Number of recent error samples to print per archive (default: %(default)s).",
        )

    def handle(self, *args, **opts):  # type: ignore[override]
        repo_filter: str | None = opts.get("repo")
        error_count: int = max(0, int(opts.get("errors", 5)))

        qs = ArchiveImportItem.objects.all()
        if repo_filter:
            if "/" not in repo_filter:
                raise CommandError("--repo must be in owner/name form")
            owner, name = repo_filter.split("/", 1)
            repo = Repository.objects.filter(owner=owner, name=name).first()
            if repo is None:
                raise CommandError(f"Repository not found: {repo_filter}")
            qs = qs.filter(repository=repo)

        archives = sorted(qs.values_list("archive_name", flat=True).distinct())
        if not archives:
            self.stdout.write("No archive worklist rows.")
            return

        counts = _counts_by_archive_status(qs)
        for archive in archives:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(f"archive: {archive}"))
            archive_qs = qs.filter(archive_name=archive)
            for status in _STATUS_ORDER:
                n = counts.get((archive, status.value), 0)
                self.stdout.write(f"  {status.value:<18} {n:>8}")

            oldest_pending = (
                archive_qs.filter(status=ArchiveImportItemStatus.PENDING)
                .order_by("pr_number")
                .values("pr_number", "archive_path", "created_at")
                .first()
            )
            if oldest_pending:
                self.stdout.write(
                    "  oldest pending:    "
                    f"PR #{oldest_pending['pr_number']} "
                    f"({oldest_pending['archive_path']}, enrolled {oldest_pending['created_at'].isoformat()})"
                )
            if error_count:
                _print_recent_errors(self.stdout, archive_qs, error_count)


def _counts_by_archive_status(qs) -> dict[tuple[str, str], int]:
    rows = qs.values("archive_name", "status").annotate(n=Count("id"))
    out: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        out[(row["archive_name"], row["status"])] = int(row["n"])
    return out


def _print_recent_errors(stdout, archive_qs, limit: int) -> None:
    samples: Iterable[ArchiveImportItem] = (
        archive_qs.filter(
            status__in=[
                ArchiveImportItemStatus.FAILED_TRANSIENT,
                ArchiveImportItemStatus.FAILED_PERMANENT,
            ]
        )
        .exclude(last_error="")
        .order_by("-last_attempted_at")[:limit]
    )
    samples = list(samples)
    if not samples:
        return
    stdout.write(f"  recent errors (up to {limit}):")
    for item in samples:
        ts = item.last_attempted_at.isoformat() if item.last_attempted_at else "-"
        msg = (item.last_error or "").splitlines()[0][:160]
        stdout.write(f"    PR #{item.pr_number:<7} [{item.status:<18}] @ {ts}: {msg}")
