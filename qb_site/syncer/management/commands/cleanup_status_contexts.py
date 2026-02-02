from __future__ import annotations

from typing import Iterable

from django.core.management.base import BaseCommand
from django.db import connection

from syncer.models import PullRequest, StatusContext


class Command(BaseCommand):
    help = "Delete outdated StatusContext snapshot rows, keeping the latest per (head_sha, name)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--repo",
            help="Limit to a single repository (owner/name).",
            default=None,
        )
        parser.add_argument(
            "--include-closed",
            action="store_true",
            help="Include closed PRs (default: only open).",
        )
        parser.add_argument(
            "--limit-prs",
            type=int,
            default=None,
            help="Limit number of PRs processed (for testing).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete rows (default: dry-run).",
        )

    def _latest_snapshot_ids_for_pr(self, pr_id: int) -> list[int]:
        qs = StatusContext.objects.filter(pull_request_id=pr_id, github_node_id__isnull=False)
        if connection.vendor == "postgresql":
            return list(
                qs.order_by("head_sha", "name", "-gh_created_at", "-id")
                .distinct("head_sha", "name")
                .values_list("id", flat=True)
            )
        latest_ids: list[int] = []
        last_key: tuple[str, str] | None = None
        for row in qs.values("id", "head_sha", "name", "gh_created_at").order_by(
            "head_sha", "name", "-gh_created_at", "-id"
        ):
            key = (row.get("head_sha") or "", (row.get("name") or "").lower())
            if key == last_key:
                continue
            latest_ids.append(int(row["id"]))
            last_key = key
        return latest_ids

    def _iter_prs(self, *, repo: str | None, include_closed: bool, limit: int | None) -> Iterable[PullRequest]:
        qs = PullRequest.objects.select_related("repository").order_by("id")
        if not include_closed:
            qs = qs.filter(state="open")
        if repo:
            if "/" not in repo:
                raise ValueError("repo must be in OWNER/NAME format")
            owner, name = repo.split("/", 1)
            qs = qs.filter(repository__owner=owner, repository__name=name)
        if limit:
            qs = qs[: int(limit)]
        return qs.iterator()

    def handle(self, *args, **options) -> None:
        repo = options.get("repo")
        include_closed = bool(options.get("include_closed"))
        limit = options.get("limit_prs")
        apply = bool(options.get("apply"))

        total_prs = 0
        total_deleted = 0
        for pr in self._iter_prs(repo=repo, include_closed=include_closed, limit=limit):
            total_prs += 1
            latest_ids = self._latest_snapshot_ids_for_pr(pr.id)
            stale_qs = StatusContext.objects.filter(
                pull_request_id=pr.id,
                github_node_id__isnull=False,
            )
            if latest_ids:
                stale_qs = stale_qs.exclude(id__in=latest_ids)
            stale_count = stale_qs.count()
            if stale_count and apply:
                stale_qs.delete()
            total_deleted += stale_count

        mode = "deleted" if apply else "would_delete"
        self.stdout.write(f"Processed PRs: {total_prs}")
        self.stdout.write(f"{mode}: {total_deleted}")
