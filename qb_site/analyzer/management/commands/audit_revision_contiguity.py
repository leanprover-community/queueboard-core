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


class Command(BaseCommand):
    help = (
        "Read-only audit of PRRevision window contiguity (design decision 049).\n"
        "Counts PRs whose persisted revision windows have gaps, overlaps, backward, or\n"
        "mid-chain open-ended windows. Makes no writes; use this to size a one-off backfill."
    )

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        parser.add_argument("--repo", default=None, help="Restrict to owner/name (default: all active repos)")
        parser.add_argument("--sample", type=int, default=10, help="Sample violating PR numbers to print per repo (default 10)")

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

    def _audit_repo(self, repo: Repository) -> tuple[int, int, dict[str, int], list[tuple[int, list[str]]]]:
        rows: Iterable[tuple[int, int, Optional[datetime], Optional[datetime]]] = (
            PRRevision.objects.filter(pull_request__repository=repo)
            .order_by("pull_request_id", "from_ts", "seq", "id")
            .values_list("pull_request_id", "pull_request__number", "from_ts", "to_ts")
            .iterator(chunk_size=2000)
        )
        total_prs = 0
        viol_prs = 0
        by_cat: dict[str, int] = {"gap": 0, "overlap": 0, "backward": 0, "open_mid": 0}
        samples: list[tuple[int, list[str]]] = []
        for _pid, group in itertools.groupby(rows, key=lambda r: r[0]):
            grp = list(group)
            total_prs += 1
            windows = [(r[2], r[3]) for r in grp]
            cats = classify_revision_windows(windows)
            if not cats:
                continue
            viol_prs += 1
            for cat in cats:
                by_cat[cat] += 1
            return_number = grp[0][1]
            samples.append((int(return_number), sorted(cats)))
        return total_prs, viol_prs, by_cat, samples

    def handle(self, *args, **options):  # type: ignore[override]
        repo_str: Optional[str] = options.get("repo")
        sample: int = int(options["sample"])
        repos = self._repos(repo_str)

        grand_total = 0
        grand_viol = 0
        grand_by_cat: dict[str, int] = {"gap": 0, "overlap": 0, "backward": 0, "open_mid": 0}

        self.stdout.write(self.style.MIGRATE_HEADING("PRRevision contiguity audit (read-only)"))
        for repo in repos:
            total_prs, viol_prs, by_cat, samples = self._audit_repo(repo)
            grand_total += total_prs
            grand_viol += viol_prs
            for cat, n in by_cat.items():
                grand_by_cat[cat] += n
            cat_str = ", ".join(f"{cat}={by_cat[cat]}" for cat in ("gap", "overlap", "backward", "open_mid"))
            self.stdout.write(f" - {repo.owner}/{repo.name}: {viol_prs}/{total_prs} PRs violate contiguity ({cat_str})")
            for number, cats in samples[:sample]:
                self.stdout.write(f"     PR #{number}: {', '.join(cats)}")
            if viol_prs > sample:
                self.stdout.write(f"     … and {viol_prs - min(sample, len(samples))} more")

        grand_cat_str = ", ".join(f"{cat}={grand_by_cat[cat]}" for cat in ("gap", "overlap", "backward", "open_mid"))
        style = self.style.WARNING if grand_viol else self.style.SUCCESS
        self.stdout.write(style(f"Total: {grand_viol}/{grand_total} PRs violate contiguity ({grand_cat_str})"))
