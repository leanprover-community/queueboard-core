from __future__ import annotations

from typing import Iterable, List, Optional

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from core.models import Repository
from syncer.models import PullRequest
from analyzer.services.ci_backfill import plan_missing_ci_shas, enqueue_ci_by_shas


class Command(BaseCommand):
    help = (
        "Plan or enqueue CI-by-SHA backfills for PRs using Analyzer revision windows.\n"
        "By default, prints the missing head SHAs per PR. Use --enqueue to submit\n"
        "syncer.sync_ci_for_shas tasks."
    )

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument(
            "--pr",
            nargs="*",
            type=int,
            default=None,
            help="Optional list of PR numbers to restrict the operation",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=2,
            help="Max number of missing SHAs to request per PR (default: 2)",
        )
        parser.add_argument(
            "--pages-per-sha",
            type=int,
            default=getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1),
            help="Max CI pages to fetch per SHA (default: SYNCER_CI_BY_SHA_PAGES)",
        )
        parser.add_argument(
            "--enqueue",
            action="store_true",
            default=False,
            help="Enqueue syncer.sync_ci_for_shas tasks instead of dry-run printing",
        )
        parser.add_argument(
            "--require-assoc",
            action="store_true",
            default=False,
            help=(
                "Require commit to be associated with the PR when enqueuing (conservative). "
                "Typically leave off for historical heads after force-pushes."
            ),
        )

    def handle(self, *args, **options):  # type: ignore[override]
        repo_str: str = options["repo"]
        pr_numbers: Optional[List[int]] = options["pr"]
        limit: int = int(options["limit"])
        pages_per_sha: int = int(options["pages_per_sha"]) if options.get("pages_per_sha") else 1
        do_enqueue: bool = bool(options["enqueue"]) or False
        require_assoc: bool = bool(options["require_assoc"]) or False

        if "/" not in repo_str:
            raise CommandError("--repo must be in 'owner/name' format")
        owner, name = repo_str.split("/", 1)
        repo = Repository.objects.filter(owner=owner, name=name).first()
        if not repo:
            raise CommandError(f"Repository not found: {owner}/{name}")

        plan = plan_missing_ci_shas(repo=repo, pr_numbers=pr_numbers, limit_per_pr=limit)
        if not plan:
            self.stdout.write(self.style.WARNING("No matching PRs found."))
            return

        planned_total = 0
        enqueued_total = 0
        rows: list[str] = []

        for item in plan:
            pr = item.pr
            shas = item.shas
            planned_total += len(item.shas)
            if do_enqueue:
                try:
                    task_id = enqueue_ci_by_shas(
                        pr=pr,
                        shas=shas,
                        pages_per_sha=pages_per_sha,
                        require_pr_association=require_assoc,
                    )
                except Exception as e:  # pragma: no cover - best effort enqueue
                    rows.append(
                        f"PR #{pr.number}: enqueue failed for {len(shas)} SHAs ({', '.join(shas[:5])}{'...' if len(shas) > 5 else ''}): {e}"
                    )
                else:
                    enqueued_total += len(item.shas)
                    rows.append(f"PR #{pr.number}: enqueued {len(item.shas)} SHAs (task_id={task_id})")
            else:
                rows.append(
                    f"PR #{pr.number}: missing {len(item.shas)} SHAs → {', '.join(item.shas[:5])}{'...' if len(item.shas) > 5 else ''}"
                )

        header = f"Plan CI backfill for {owner}/{name}"
        self.stdout.write(self.style.MIGRATE_HEADING(header))
        if rows:
            for line in rows:
                self.stdout.write(" - " + line)
        else:
            self.stdout.write("No work: no missing CI heads found.")

        if do_enqueue:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Enqueued total: {enqueued_total} SHAs across {len(plan)} PR(s); pages_per_sha={pages_per_sha}; require_assoc={require_assoc}"
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry-run: planned total {planned_total} SHAs across {len(plan)} PR(s); use --enqueue to submit tasks."
                )
            )
