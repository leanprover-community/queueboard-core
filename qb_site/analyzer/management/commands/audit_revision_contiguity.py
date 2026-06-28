from __future__ import annotations

import itertools
from datetime import datetime
from typing import Iterable, Optional

from django.core.management.base import BaseCommand, CommandError

from analyzer.models import PRRevision
from core.models import Repository


def classify_revision_windows(windows: list[tuple[Optional[datetime], Optional[datetime]]]) -> set[str]:
    """Return the set of contiguity-violation categories in a PR's revision windows.

    ``windows`` is the PR's ``(from_ts, to_ts)`` rows ordered by ``(from_ts, seq, id)``.
    Mirrors the invariant in ``revisions._revisions_need_recontiguation`` (design
    decisions 048 + 049): an empty set means the chain is contiguous and well-formed.

    Categories:
      - ``backward``: a window with ``to_ts <= from_ts`` (backwards or zero-width).
      - ``gap``:      a non-final window ending before the next window starts.
      - ``overlap``:  a non-final window ending after the next window starts.
      - ``open_mid``: a non-final window left open-ended (``to_ts`` is null).
    """
    violations: set[str] = set()
    last = len(windows) - 1
    for i, (from_ts, to_ts) in enumerate(windows):
        if to_ts is not None and to_ts <= from_ts:
            violations.add("backward")
        if i == last:
            continue
        next_from = windows[i + 1][0]
        if to_ts is None:
            violations.add("open_mid")
        elif to_ts < next_from:
            violations.add("gap")
        elif to_ts > next_from:
            violations.add("overlap")
    return violations


_CATS = ("gap", "overlap", "backward", "open_mid")


class Command(BaseCommand):
    help = (
        "Audit (and optionally heal) PRRevision window contiguity (design decision 049).\n"
        "Without --fix this is read-only: it counts PRs whose persisted revision windows\n"
        "have gaps, overlaps, backward, or mid-chain open-ended windows. With --fix it\n"
        "rebuilds each violating PR (revisions, then queue windows) to restore contiguity."
    )

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        parser.add_argument("--repo", default=None, help="Restrict to owner/name (default: all active repos)")
        parser.add_argument("--sample", type=int, default=10, help="Sample violating PR numbers to print per repo (default 10)")
        parser.add_argument(
            "--fix",
            action="store_true",
            default=False,
            help="Rebuild violating PRs to heal them (mutating). Default: read-only audit.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="With --fix, max violating PRs to heal per repo (0 = all).",
        )
        parser.add_argument(
            "--skip-windows",
            action="store_true",
            default=False,
            help="With --fix, only rebuild revisions; skip the queue-window rebuild.",
        )

    def _repos(self, repo_str: Optional[str]) -> list[Repository]:
        if repo_str:
            if "/" not in repo_str:
                raise CommandError("--repo must be in 'owner/name' format")
            owner, name = repo_str.split("/", 1)
            repos = list(Repository.objects.filter(owner=owner, name=name))
            if not repos:
                raise CommandError(f"Repository not found: {repo_str}")
            return repos
        return list(Repository.objects.filter(is_active=True).order_by("owner", "name"))

    def _audit_repo(self, repo: Repository) -> tuple[int, dict[str, int], list[tuple[int, int, list[str]]]]:
        """Return ``(total_prs, by_category_counts, violators)``.

        ``violators`` is a list of ``(pull_request_id, number, sorted_categories)``.
        """
        rows: Iterable[tuple[int, int, Optional[datetime], Optional[datetime]]] = (
            PRRevision.objects.filter(pull_request__repository=repo)
            .order_by("pull_request_id", "from_ts", "seq", "id")
            .values_list("pull_request_id", "pull_request__number", "from_ts", "to_ts")
            .iterator(chunk_size=2000)
        )
        total_prs = 0
        by_cat: dict[str, int] = {cat: 0 for cat in _CATS}
        violators: list[tuple[int, int, list[str]]] = []
        for pid, group in itertools.groupby(rows, key=lambda r: r[0]):
            grp = list(group)
            total_prs += 1
            cats = classify_revision_windows([(r[2], r[3]) for r in grp])
            if not cats:
                continue
            for cat in cats:
                by_cat[cat] += 1
            violators.append((int(pid), int(grp[0][1]), sorted(cats)))
        return total_prs, by_cat, violators

    def _fix_repo(
        self, repo: Repository, violators: list[tuple[int, int, list[str]]], *, limit: int, rebuild_windows: bool
    ) -> tuple[int, int, int]:
        """Rebuild violating PRs. Returns ``(fixed, noop, failed)`` counts."""
        from django.utils import timezone

        from analyzer.models import PRRevisionBuildState, QueueRuleSet
        from analyzer.services.queue_window_build_state import record_queue_window_build_states
        from analyzer.services.queue_windows import rebuild_queue_windows_for_pr
        from analyzer.services.revisions import rebuild_pr_revisions
        from syncer.models import PullRequest

        rule_sets = list(QueueRuleSet.objects.filter(repository=repo, is_active=True))
        targets = [v[0] for v in violators]
        if limit:
            targets = targets[:limit]

        fixed = noop = failed = 0
        for pr_id in targets:
            try:
                pr = PullRequest.objects.get(id=pr_id)
                # The deployed self-heal makes rebuild_pr_revisions detect the contiguity
                # violation and run a full rebuild past the noop short-circuit.
                res = rebuild_pr_revisions(pr)
                if res.strategy == "noop":
                    noop += 1
                    self.stderr.write(f"     PR #{pr.number}: rebuild was a noop (not healed)")
                    continue
                if rebuild_windows and rule_sets:
                    summary = rebuild_queue_windows_for_pr(pr=pr, rule_sets=rule_sets)
                    state, _ = PRRevisionBuildState.objects.get_or_create(pull_request=pr)
                    record_queue_window_build_states(
                        pr=pr,
                        rule_sets=rule_sets,
                        per_ruleset=summary.get("per_ruleset", {}),
                        revision_version=int(state.revision_version),
                        built_at=timezone.now(),
                    )
                fixed += 1
                if fixed % 100 == 0:
                    self.stdout.write(f"     … healed {fixed}/{len(targets)}")
            except Exception as exc:  # pragma: no cover - best-effort per-PR recovery
                failed += 1
                self.stderr.write(f"     PR id={pr_id}: fix failed: {exc}")
        return fixed, noop, failed

    def handle(self, *args, **options):  # type: ignore[override]
        repo_str: Optional[str] = options.get("repo")
        sample: int = int(options["sample"])
        do_fix: bool = bool(options["fix"])
        limit: int = int(options["limit"])
        rebuild_windows: bool = not bool(options["skip_windows"])
        repos = self._repos(repo_str)

        grand_total = 0
        grand_viol = 0
        grand_by_cat: dict[str, int] = {cat: 0 for cat in _CATS}
        grand_fixed = grand_noop = grand_failed = 0

        heading = "PRRevision contiguity audit" + (" + heal (--fix)" if do_fix else " (read-only)")
        self.stdout.write(self.style.MIGRATE_HEADING(heading))
        for repo in repos:
            total_prs, by_cat, violators = self._audit_repo(repo)
            viol_prs = len(violators)
            grand_total += total_prs
            grand_viol += viol_prs
            for cat in _CATS:
                grand_by_cat[cat] += by_cat[cat]
            cat_str = ", ".join(f"{cat}={by_cat[cat]}" for cat in _CATS)
            self.stdout.write(f" - {repo.owner}/{repo.name}: {viol_prs}/{total_prs} PRs violate contiguity ({cat_str})")
            for _pid, number, cats in violators[:sample]:
                self.stdout.write(f"     PR #{number}: {', '.join(cats)}")
            if viol_prs > sample:
                self.stdout.write(f"     … and {viol_prs - sample} more")

            if do_fix and violators:
                fixed, noop, failed = self._fix_repo(repo, violators, limit=limit, rebuild_windows=rebuild_windows)
                grand_fixed += fixed
                grand_noop += noop
                grand_failed += failed
                self.stdout.write(self.style.SUCCESS(f"   healed {fixed}, noop {noop}, failed {failed}"))

        grand_cat_str = ", ".join(f"{cat}={grand_by_cat[cat]}" for cat in _CATS)
        style = self.style.WARNING if grand_viol else self.style.SUCCESS
        self.stdout.write(style(f"Total: {grand_viol}/{grand_total} PRs violate contiguity ({grand_cat_str})"))
        if do_fix:
            self.stdout.write(self.style.SUCCESS(f"Healed {grand_fixed}, noop {grand_noop}, failed {grand_failed}"))
            self.stdout.write("Re-run without --fix to confirm violations dropped to 0.")
