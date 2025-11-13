from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from django.db import transaction
from django.utils import timezone

from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType, CheckRun, StatusContext
from analyzer.models import PRRevision


@dataclass
class RebuildResult:
    created: int
    deleted: int


def _infer_seed_sha(pr: PullRequest) -> str | None:
    """Infer a seed head SHA when no force-push events exist.

    Heuristics (best-effort):
    - Prefer the most recent CheckRun.head_sha for the PR.
    - Otherwise prefer the most recent StatusContext.head_sha for the PR.
    - Return None if neither exists.
    """
    cr = (
        CheckRun.objects.filter(pull_request=pr)
        .exclude(head_sha="")
        .order_by("-gh_completed_at", "-gh_started_at", "-id")
        .first()
    )
    if cr and cr.head_sha:
        return cr.head_sha
    sc = StatusContext.objects.filter(pull_request=pr).exclude(head_sha="").order_by("-gh_created_at", "-id").first()
    if sc and sc.head_sha:
        return sc.head_sha
    return None


@transaction.atomic
def rebuild_pr_revisions(pr: PullRequest) -> RebuildResult:
    """Rebuild head revision windows for a PR from timeline events.

    Expected behavior
    - Reads HEAD_FORCE_PUSHED events in chronological order and constructs windows:
      [created_at, first_event) with head=first.before_sha, then [event_i, event_{i+1}) with
      head=event_i.after_sha, and a final open-ended window [last_event, None).
    - If there are no force-push events, attempts to seed a single open-ended window from
      the most recent CI snapshot head_sha. If no seed SHA can be inferred, no rows are created.
    - Replaces existing PRRevision rows for this PR in a single transaction.
    """
    # Delete existing windows for idempotency
    deleted, _ = PRRevision.objects.filter(pull_request=pr).delete()

    fps: List[PRTimelineEvent] = list(
        PRTimelineEvent.objects.filter(pull_request=pr, type=PRTimelineEventType.HEAD_FORCE_PUSHED).order_by("occurred_at", "id")
    )
    created = 0
    seq = 0
    if fps:
        # Initial window from PR created_at to first force-push with before_sha
        start = pr.gh_created_at if pr.gh_created_at else timezone.now()
        PRRevision.objects.create(
            pull_request=pr, head_sha=fps[0].before_sha or "", from_ts=start, to_ts=fps[0].occurred_at, seq=seq
        )
        created += 1
        seq += 1
        # Windows for each force-push, head becomes after_sha
        for i, ev in enumerate(fps):
            end = fps[i + 1].occurred_at if i + 1 < len(fps) else None
            PRRevision.objects.create(pull_request=pr, head_sha=ev.after_sha or "", from_ts=ev.occurred_at, to_ts=end, seq=seq)
            created += 1
            seq += 1
    else:
        # Seed from CI if possible
        seed = _infer_seed_sha(pr)
        if seed:
            start = pr.gh_created_at if pr.gh_created_at else timezone.now()
            PRRevision.objects.create(pull_request=pr, head_sha=seed, from_ts=start, to_ts=None, seq=0)
            created = 1

    return RebuildResult(created=created, deleted=deleted)


def next_revision_backfill_shas(pr: PullRequest, limit: int = 2) -> list[str]:
    """Return up to `limit` head SHAs whose CI appears missing.

    Heuristic: select revision head SHAs (older first) where neither any CheckRun nor
    any StatusContext exists for that SHA. If `limit` <= 0 returns an empty list.
    """
    if limit <= 0:
        return []
    shas: list[str] = []
    # Order from oldest to newest for steady progress backwards
    for rev in PRRevision.objects.filter(pull_request=pr).order_by("from_ts", "seq", "id"):
        sha = rev.head_sha or ""
        if not sha:
            continue
        has_cr = CheckRun.objects.filter(pull_request=pr, head_sha=sha).exists()
        has_sc = StatusContext.objects.filter(pull_request=pr, head_sha=sha).exists()
        if not (has_cr or has_sc):
            shas.append(sha)
            if len(shas) >= limit:
                break
    return shas
