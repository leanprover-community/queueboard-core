from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from syncer.models import CheckRun, PullRequest, PRTimelineEvent, PRTimelineEventType, StatusContext
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


def _collect_ci_first_seen(pr: PullRequest) -> dict[str, datetime]:
    """Collect earliest CI timestamps per head_sha for a PR.

    This helper underpins both the no-force-push CI window builder and the
    force-push-aware builder by returning a mapping:
        head_sha -> earliest provider timestamp (aware) for that SHA.
    """
    first_seen: dict[str, datetime] = {}

    def _update_first(head_sha: str, ts: Optional[datetime]) -> None:
        if not head_sha or ts is None:
            return
        if timezone.is_naive(ts):
            ts = timezone.make_aware(ts)
        cur = first_seen.get(head_sha)
        if cur is None or ts < cur:
            first_seen[head_sha] = ts

    for cr in (
        CheckRun.objects.filter(pull_request=pr)
        .exclude(head_sha="")
        .only(
            "head_sha",
            "gh_started_at",
            "gh_completed_at",
        )
    ):
        ts = cr.gh_started_at or cr.gh_completed_at
        _update_first(cr.head_sha, ts)

    for sc in (
        StatusContext.objects.filter(pull_request=pr)
        .exclude(head_sha="")
        .only(
            "head_sha",
            "gh_created_at",
        )
    ):
        _update_first(sc.head_sha, sc.gh_created_at)

    return first_seen


def _build_ci_head_windows(
    pr: PullRequest,
    first_seen: dict[str, datetime],
) -> List[Tuple[datetime, str, Optional[datetime]]]:
    """Build head windows from CI snapshots when no force-push events exist.

    Heuristics (best-effort):
    - `first_seen` maps head_sha -> earliest provider timestamp.
    - Sort distinct head_shas by that time and build windows:
      [created_at, first_ts(next_sha)) for the first head; then [first_ts(sha_i), first_ts(sha_{i+1})) and
      finally [first_ts(last_sha), None).

    Notes
    - Only used when no HEAD_FORCE_PUSHED events exist for the PR.
    - If no CI snapshots exist, returns an empty list.
    """
    if not first_seen:
        return []

    start = pr.gh_created_at if pr.gh_created_at else timezone.now()
    # Sort head_shas by earliest timestamp.
    ordered = sorted(first_seen.items(), key=lambda item: item[1])
    windows: List[Tuple[datetime, str, Optional[datetime]]] = []
    for i, (sha, ts) in enumerate(ordered):
        if i == 0:
            from_ts = start
        else:
            from_ts = ts
        if i + 1 < len(ordered):
            next_ts = ordered[i + 1][1]
            to_ts: Optional[datetime] = next_ts
        else:
            to_ts = None
        windows.append((from_ts, sha, to_ts))
    return windows


def _build_force_push_head_windows(
    pr: PullRequest,
    fps: List[PRTimelineEvent],
    ci_first_seen: dict[str, datetime],
) -> List[Tuple[datetime, str, Optional[datetime]]]:
    """Build head windows combining force-push segments with CI-derived head changes.

    Semantics
    - HEAD_FORCE_PUSHED events define hard segment boundaries and baseline heads:
      - [created_at, first_fp.occurred_at) with head=fps[0].before_sha
      - [fp_i.occurred_at, fp_{i+1}.occurred_at) with head=fps[i].after_sha
      - [last_fp.occurred_at, None) with head=last_fp.after_sha
    - Within each segment, additional CI heads are incorporated:
      - For each distinct head_sha with earliest CI timestamp in the segment,
        we create a new window starting at that timestamp.
      - The baseline head remains active from the segment start until the first
        CI-derived head change (if any).
    """
    if not fps:
        return []

    start = pr.gh_created_at if pr.gh_created_at else timezone.now()

    # Build segments: (segment_start, segment_end, baseline_head_sha)
    segments: List[Tuple[datetime, Optional[datetime], str]] = []
    # Before the first force-push: baseline head is the first before_sha.
    first_fp = fps[0]
    segments.append((start, first_fp.occurred_at, first_fp.before_sha or ""))
    # Between and after force-pushes: baseline head is the prior after_sha.
    for i, ev in enumerate(fps):
        seg_start = ev.occurred_at
        if i + 1 < len(fps):
            seg_end: Optional[datetime] = fps[i + 1].occurred_at
        else:
            seg_end = None
        baseline = ev.after_sha or ""
        segments.append((seg_start, seg_end, baseline))

    windows: List[Tuple[datetime, str, Optional[datetime]]] = []

    for seg_start, seg_end, baseline in segments:
        # Always start the segment with the baseline head, even if CI never
        # runs for that SHA.
        heads: List[Tuple[str, datetime]] = [(baseline, seg_start)]

        # Incorporate CI heads whose earliest timestamp falls within the segment.
        for sha, ts in ci_first_seen.items():
            if sha == baseline:
                continue
            # Constrain to [seg_start, seg_end) (or open-ended if seg_end is None).
            if ts < seg_start:
                continue
            if seg_end is not None and ts >= seg_end:
                continue
            heads.append((sha, ts))

        # Sort heads by their first-seen timestamps to derive boundaries.
        heads.sort(key=lambda item: item[1])

        for idx, (sha, first_ts) in enumerate(heads):
            from_ts = seg_start if idx == 0 else first_ts
            if idx + 1 < len(heads):
                next_ts = heads[idx + 1][1]
                to_ts = next_ts
            else:
                to_ts = seg_end
            windows.append((from_ts, sha, to_ts))

    return windows


@transaction.atomic
def rebuild_pr_revisions(pr: PullRequest) -> RebuildResult:
    """Rebuild head revision windows for a PR from timeline events.

    Preconditions
    - Requires full timeline backfill (`pr.timeline_backfill_done is True`). If not met,
      performs no work and returns created=deleted=0.

    Behavior (idempotent)
    - Computes the complete window set from HEAD_FORCE_PUSHED events and `gh_created_at`:
      [created_at, first.occurred_at) with head=first.before_sha; then [ev_i, ev_{i+1}) with
      head=ev_i.after_sha; final window [last.occurred_at, None) with head=last.after_sha.
      Within each segment, incorporates additional head-change signals from CI:
      distinct head_shas with earliest CI timestamps inside the segment begin new windows.
    - If no events exist, seeds one or more windows from CI snapshots grouped by head_sha.
      If no CI exists, seeds a single open-ended window from the most recent CI snapshot head_sha
      (CheckRun preferred, else StatusContext). If still no head can be inferred, no rows are created.
    - Upserts windows keyed by (pull_request, from_ts) and deletes any stale rows whose from_ts
      is not in the computed set. Only updates head_sha/to_ts/seq when they changed.
    """
    if not getattr(pr, "timeline_backfill_done", False):
        return RebuildResult(created=0, deleted=0)

    fps: List[PRTimelineEvent] = list(
        PRTimelineEvent.objects.filter(pull_request=pr, type=PRTimelineEventType.HEAD_FORCE_PUSHED).order_by(
            "occurred_at",
            "id",
        )
    )
    ci_first_seen = _collect_ci_first_seen(pr)
    expected: List[Tuple[datetime, str, Optional[datetime]]] = []
    if fps:
        expected.extend(_build_force_push_head_windows(pr, fps, ci_first_seen))
    else:
        # When no force-push events exist, attempt to build head windows from CI
        # snapshots grouped by head_sha. If no CI is available, fall back to the
        # previous heuristic of seeding a single open-ended window from the most
        # recent snapshot.
        ci_windows = _build_ci_head_windows(pr, ci_first_seen)
        if ci_windows:
            expected.extend(ci_windows)
        else:
            seed = _infer_seed_sha(pr)
            if seed:
                start = pr.gh_created_at if pr.gh_created_at else timezone.now()
                expected.append((start, seed, None))

    created = 0
    for seq, (from_ts, head_sha, to_ts) in enumerate(expected):
        obj, was_created = PRRevision.objects.get_or_create(
            pull_request=pr,
            from_ts=from_ts,
            defaults={"head_sha": head_sha, "to_ts": to_ts, "seq": seq},
        )
        if was_created:
            created += 1
        else:
            changed = False
            if obj.head_sha != head_sha:
                obj.head_sha = head_sha
                changed = True
            if obj.to_ts != to_ts:
                obj.to_ts = to_ts
                changed = True
            if obj.seq != seq:
                obj.seq = seq
                changed = True
            if changed:
                obj.save(update_fields=["head_sha", "to_ts", "seq"])

    expected_starts = [ft for (ft, _, _) in expected]
    qs = PRRevision.objects.filter(pull_request=pr)
    if expected_starts:
        qs = qs.exclude(from_ts__in=expected_starts)
    deleted, _ = qs.delete()

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
