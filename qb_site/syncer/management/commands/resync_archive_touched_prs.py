from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import Repository
from syncer.services.archive_import import (
    archive_touched_live_prs_queryset,
    archive_touched_resync_targets,
)
from syncer.tasks.sync_tasks import sync_pr_task


class Command(BaseCommand):
    help = (
        "Force-resync the live PRs the archive importer's UPDATE path processed "
        "(design doc 043 follow-up). These pre-existing PRs may have had un-gated "
        "core fields (gh_updated_at, additions/deletions, refs, author) rewound to "
        "an older archive snapshot, and they are a superset of the resurrected-label "
        "PRs. A forced sync re-fetches GitHub truth and heals both in one pass. "
        "PRs already re-synced live since their last archive touch are excluded by "
        "default (they are provably healed); pass --include-healed to keep them. "
        "Ordering is open PRs first, then stalest last_synced_at, so repeated "
        "--limit batches prioritize user-visible rows and make monotone progress. "
        "Dry-run by default; pass --apply to enqueue sync_pr(force=True) tasks. "
        "Operational note: each forced sync costs GitHub GraphQL budget and the "
        "tasks share the default Celery queue, so drip-feed with --limit (~1000/h) "
        "instead of enqueueing the full set at once — or, for an unattended drain, "
        "enable the beat task syncer.resync_archive_touched_tick by setting "
        "ARCHIVE_RESYNC_PER_TICK > 0."
    )

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", help="Limit to a single repository in owner/name format")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Enqueue the forced sync tasks (default: dry-run count/sample only)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Cap the number of PRs enqueued (0 = no cap)",
        )
        parser.add_argument(
            "--include-healed",
            action="store_true",
            help=(
                "Also target PRs whose last_synced_at is newer than their last archive "
                "touch (a live sync already re-fetched GitHub truth for those)"
            ),
        )

    def handle(self, *args, **opts):  # type: ignore[override]
        repo: Repository | None = None
        repo_opt = opts.get("repo")
        if repo_opt:
            if "/" not in repo_opt:
                raise CommandError("--repo must be in the form owner/name")
            owner, name = repo_opt.split("/", 1)
            repo = Repository.objects.filter(owner=owner, name=name).first()
            if repo is None:
                raise CommandError(f"Repository not found: {repo_opt}")

        include_healed = bool(opts.get("include_healed"))
        qs = archive_touched_resync_targets(repo, include_healed=include_healed)

        limit = int(opts.get("limit") or 0)
        values = qs.values_list("repository_id", "number")
        rows = list(values[:limit] if limit > 0 else values)

        if not include_healed:
            touched_total = archive_touched_live_prs_queryset(repo).count()
            remaining_total = qs.count() if (limit > 0) else len(rows)
            healed_total = touched_total - remaining_total
            self.stdout.write(
                f"{touched_total} archive-touched live PR(s); {healed_total} already healed by a live sync "
                f"since their last archive touch (excluded), {remaining_total} remaining"
            )

        if not rows:
            self.stdout.write(self.style.SUCCESS("No archive-touched live PRs to resync."))
            return

        apply = bool(opts.get("apply"))
        self.stdout.write(
            self.style.WARNING(
                f"{len(rows)} archive-touched live PR(s) to force-resync"
                + (f" (limited from {qs.count()})" if limit > 0 else "")
                + ("" if apply else " (dry-run; nothing enqueued)")
            )
        )

        if not apply:
            sample = rows[:20]
            for repo_id, number in sample:
                self.stdout.write(f"  repo_id={repo_id} #{number}")
            if len(rows) > len(sample):
                self.stdout.write(f"  … and {len(rows) - len(sample)} more")
            self.stdout.write("Re-run with --apply to enqueue forced syncs.")
            return

        enqueued = 0
        for repo_id, number in rows:
            sync_pr_task.delay(repo_id, number, force=True)
            enqueued += 1
        self.stdout.write(self.style.SUCCESS(f"Enqueued {enqueued} sync_pr(force=True) task(s)."))
