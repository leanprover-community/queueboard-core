from __future__ import annotations

from typing import Iterable

from django.core.management.base import BaseCommand

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
            "--offset",
            type=int,
            default=0,
            help="Skip the first N PRs (for batching).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete rows (default: dry-run).",
        )
        parser.add_argument(
            "--progress-every",
            type=int,
            default=100,
            help="Log progress every N PRs (default: 100; 0 to disable).",
        )

    def _latest_snapshot_ids_for_pr(self, pr_id: int) -> list[int]:
        qs = StatusContext.objects.filter(pull_request_id=pr_id, github_node_id__isnull=False)
        return list(
            qs.order_by("head_sha", "name", "-gh_created_at", "-id").distinct("head_sha", "name").values_list("id", flat=True)
        )

    def _iter_prs(
        self,
        *,
        repo: str | None,
        include_closed: bool,
        limit: int | None,
        offset: int,
    ) -> Iterable[PullRequest]:
        qs = PullRequest.objects.select_related("repository").order_by("id")
        if not include_closed:
            qs = qs.filter(state="open")
        if repo:
            if "/" not in repo:
                raise ValueError("repo must be in OWNER/NAME format")
            owner, name = repo.split("/", 1)
            qs = qs.filter(repository__owner=owner, repository__name=name)
        offset_val = int(offset or 0)
        if offset_val < 0:
            offset_val = 0
        if limit:
            limit_val = int(limit)
            qs = qs[offset_val : offset_val + limit_val]
        elif offset_val:
            qs = qs[offset_val:]
        return qs.iterator()

    def handle(self, *args, **options) -> None:
        repo = options.get("repo")
        include_closed = bool(options.get("include_closed"))
        limit = options.get("limit_prs")
        offset = options.get("offset", 0)
        apply = bool(options.get("apply"))
        progress_every = int(options.get("progress_every") or 0)

        total_prs = 0
        total_deleted = 0
        for pr in self._iter_prs(repo=repo, include_closed=include_closed, limit=limit, offset=offset):
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
            if progress_every and total_prs % progress_every == 0:
                self.stdout.write(f"Processed PRs: {total_prs} (stale rows so far: {total_deleted})")

        mode = "deleted" if apply else "would_delete"
        self.stdout.write(f"Processed PRs: {total_prs}")
        self.stdout.write(f"{mode}: {total_deleted}")
