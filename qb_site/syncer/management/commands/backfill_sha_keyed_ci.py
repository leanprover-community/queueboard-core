from __future__ import annotations

from contextlib import nullcontext
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Repository
from syncer.models import CheckRun, StatusContext
from syncer.services.ci_storage_backfill import BackfillModelStats, BackfillStats, backfill_commit_ci_rows


class Command(BaseCommand):
    help = "Backfill SHA-keyed CI tables (CommitCheckRun/CommitStatusContext) from legacy PR-keyed CheckRun/StatusContext rows."

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        parser.add_argument("--repo", help="Optional repository filter in owner/name format.")
        parser.add_argument("--batch-size", type=int, default=1000, help="Rows per batch per model (default: 1000).")
        parser.add_argument("--checkrun-start-id", type=int, default=0, help="Resume cursor for CheckRun rows (default: 0).")
        parser.add_argument(
            "--status-start-id",
            type=int,
            default=0,
            help="Resume cursor for StatusContext rows (default: 0).",
        )
        parser.add_argument("--max-checkruns", type=int, default=None, help="Optional max CheckRun rows to process.")
        parser.add_argument(
            "--max-status-contexts",
            type=int,
            default=None,
            help="Optional max StatusContext rows to process.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Preview counts and next cursors without persisting writes.")

    def handle(self, *args, **options) -> None:  # type: ignore[override]
        repo_id: int | None = None
        repo_str = options.get("repo")
        if repo_str:
            if "/" not in repo_str:
                raise CommandError("--repo must be in owner/name format")
            owner, name = str(repo_str).split("/", 1)
            repo = Repository.objects.filter(owner=owner, name=name).only("id").first()
            if repo is None:
                raise CommandError(f"Repository not found: {owner}/{name}")
            repo_id = repo.id

        batch_size = int(options.get("batch_size") or 0)
        if batch_size <= 0:
            raise CommandError("--batch-size must be > 0")

        checkrun_start_id = int(options.get("checkrun_start_id") or 0)
        status_start_id = int(options.get("status_start_id") or 0)
        max_checkruns = options.get("max_checkruns")
        max_status_contexts = options.get("max_status_contexts")
        dry_run = bool(options.get("dry_run"))

        cr_qs = CheckRun.objects.filter(id__gt=checkrun_start_id)
        sc_qs = StatusContext.objects.filter(id__gt=status_start_id)
        if repo_id is not None:
            cr_qs = cr_qs.filter(pull_request__repository_id=repo_id)
            sc_qs = sc_qs.filter(pull_request__repository_id=repo_id)

        planned_checkruns = cr_qs.count()
        planned_status = sc_qs.count()
        if max_checkruns is not None:
            planned_checkruns = min(planned_checkruns, int(max_checkruns))
        if max_status_contexts is not None:
            planned_status = min(planned_status, int(max_status_contexts))
        planned_total = planned_checkruns + planned_status

        self.stdout.write(
            f"Planned rows: total={planned_total} (check_runs={planned_checkruns}, status_contexts={planned_status})"
        )

        cr_cursor = checkrun_start_id
        sc_cursor = status_start_id
        remaining_cr = planned_checkruns
        remaining_sc = planned_status
        progress_step = 1000
        next_progress_mark = progress_step
        cumulative = BackfillStats(
            check_runs=BackfillModelStats(next_start_id=checkrun_start_id),
            status_contexts=BackfillModelStats(next_start_id=status_start_id),
        )

        tx_ctx = transaction.atomic() if dry_run else nullcontext()
        with tx_ctx:
            while remaining_cr > 0 or remaining_sc > 0:
                run_cr = min(batch_size, remaining_cr)
                run_sc = min(batch_size, remaining_sc)
                chunk = backfill_commit_ci_rows(
                    checkrun_start_id=cr_cursor,
                    status_start_id=sc_cursor,
                    batch_size=batch_size,
                    max_checkruns=run_cr,
                    max_status_contexts=run_sc,
                    repo_id=repo_id,
                )
                cumulative.check_runs.scanned += chunk.check_runs.scanned
                cumulative.check_runs.inserted += chunk.check_runs.inserted
                cumulative.check_runs.updated += chunk.check_runs.updated
                cumulative.check_runs.skipped_duplicate += chunk.check_runs.skipped_duplicate
                cumulative.check_runs.skipped_invalid += chunk.check_runs.skipped_invalid
                cumulative.check_runs.skipped_conflict += chunk.check_runs.skipped_conflict
                cumulative.status_contexts.scanned += chunk.status_contexts.scanned
                cumulative.status_contexts.inserted += chunk.status_contexts.inserted
                cumulative.status_contexts.updated += chunk.status_contexts.updated
                cumulative.status_contexts.skipped_duplicate += chunk.status_contexts.skipped_duplicate
                cumulative.status_contexts.skipped_invalid += chunk.status_contexts.skipped_invalid
                cumulative.status_contexts.skipped_conflict += chunk.status_contexts.skipped_conflict

                if chunk.check_runs.scanned > 0:
                    cr_cursor = chunk.check_runs.next_start_id
                    cumulative.check_runs.next_start_id = cr_cursor
                if chunk.status_contexts.scanned > 0:
                    sc_cursor = chunk.status_contexts.next_start_id
                    cumulative.status_contexts.next_start_id = sc_cursor

                remaining_cr -= chunk.check_runs.scanned
                remaining_sc -= chunk.status_contexts.scanned

                processed = cumulative.check_runs.scanned + cumulative.status_contexts.scanned
                while processed >= next_progress_mark:
                    self.stdout.write(
                        "Progress: "
                        f"total={next_progress_mark}/{planned_total} "
                        f"check_runs={cumulative.check_runs.scanned}/{planned_checkruns} "
                        f"status_contexts={cumulative.status_contexts.scanned}/{planned_status}"
                    )
                    next_progress_mark += progress_step

                if chunk.check_runs.scanned == 0 and chunk.status_contexts.scanned == 0:
                    break

            if dry_run:
                transaction.set_rollback(True)

        mode = "DRY-RUN" if dry_run else "APPLY"
        self.stdout.write(self.style.MIGRATE_HEADING(f"SHA-keyed CI backfill ({mode})"))
        if repo_str:
            self.stdout.write(f"repo: {repo_str}")
        self.stdout.write(json.dumps(cumulative.to_dict(), indent=2, sort_keys=True))
