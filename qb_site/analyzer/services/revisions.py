from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from syncer.models import CheckRun, PullRequest, PRTimelineEvent, PRTimelineEventType, StatusContext
from syncer.models.check_run import CheckRunStatus
from syncer.models.status_context import StatusContextState
from analyzer.models import PRRevision, PRRevisionBuildState

PR_REVISION_BUILDER_VERSION = 1


@dataclass
class RebuildResult:
    created: int
    deleted: int
    strategy: str = "full"


def mark_pr_revision_dirty_if_earlier(pr: PullRequest, signal_ts: Optional[datetime]) -> bool:
    """Mark the PR's revision build-state dirty if a signal predates built_through_ts.

    Returns True if the state was updated. If there is no build state yet, or no
    `built_through_ts` recorded, or the signal is not earlier, this is a no-op.
    """
    if signal_ts is None:
        return False
    if timezone.is_naive(signal_ts):
        signal_ts = timezone.make_aware(signal_ts)

    state, _ = PRRevisionBuildState.objects.get_or_create(pull_request=pr)
    built_through = state.built_through_ts
    if built_through is None:
        return False
    if signal_ts >= built_through:
        return False

    if state.dirty_from_ts is None or signal_ts < state.dirty_from_ts:
        state.dirty_from_ts = signal_ts
        state.save(update_fields=["dirty_from_ts", "updated_at"])
        return True
    return False


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


def _collect_ci_first_seen(pr: PullRequest) -> tuple[dict[str, datetime], Optional[datetime]]:
    """Collect earliest CI timestamps per head_sha for a PR, plus the latest CI timestamp overall.

    This helper underpins both the no-force-push CI window builder and the
    force-push-aware builder by returning:
        - head_sha -> earliest provider timestamp (aware) for that SHA.
        - latest provider timestamp observed across CI rows, used to advance build state.
    """
    first_seen: dict[str, datetime] = {}
    latest_ts: Optional[datetime] = None

    def _update_first(head_sha: str, ts: Optional[datetime]) -> None:
        if not head_sha or ts is None:
            return
        if timezone.is_naive(ts):
            ts = timezone.make_aware(ts)
        cur = first_seen.get(head_sha)
        if cur is None or ts < cur:
            first_seen[head_sha] = ts

    def _update_latest(ts: Optional[datetime]) -> None:
        nonlocal latest_ts
        if ts is None:
            return
        if timezone.is_naive(ts):
            ts = timezone.make_aware(ts)
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts

    for cr in (
        CheckRun.objects.filter(pull_request=pr)
        .exclude(head_sha="")
        .only(
            "head_sha",
            "gh_started_at",
            "gh_completed_at",
        )
    ):
        start_ts = cr.gh_started_at
        end_ts = cr.gh_completed_at
        ts = start_ts or end_ts
        _update_first(cr.head_sha, ts)
        _update_latest(start_ts)
        _update_latest(end_ts)

    for sc in (
        StatusContext.objects.filter(pull_request=pr)
        .exclude(head_sha="")
        .only(
            "head_sha",
            "gh_created_at",
        )
    ):
        _update_first(sc.head_sha, sc.gh_created_at)
        _update_latest(sc.gh_created_at)

    return first_seen, latest_ts


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


def _compute_built_through_ts(
    pr: PullRequest,
    fps: list[PRTimelineEvent],
    ci_first_seen: dict[str, datetime],
    ci_latest: Optional[datetime],
) -> Optional[datetime]:
    """Return the latest timestamp among signals we considered for this rebuild."""
    candidates: list[datetime] = []
    if pr.gh_created_at:
        candidates.append(pr.gh_created_at)
    for ev in fps:
        if ev.occurred_at:
            candidates.append(ev.occurred_at)
    for ts in ci_first_seen.values():
        candidates.append(ts)
    if ci_latest:
        candidates.append(ci_latest)
    if not candidates:
        return None
    return max(candidates)


def _latest_signal_ts(pr: PullRequest) -> Optional[datetime]:
    """Return the latest timestamp across timeline and CI snapshots for the PR."""
    candidates: list[datetime] = []
    tl_latest = (
        PRTimelineEvent.objects.filter(pull_request=pr).aggregate(m=Max("occurred_at")).get("m")  # type: ignore[arg-type]
    )
    if tl_latest:
        candidates.append(tl_latest)
    cr_latest = (
        CheckRun.objects.filter(pull_request=pr)
        .aggregate(m=Max("gh_completed_at"))  # type: ignore[arg-type]
        .get("m")
    )
    if cr_latest:
        candidates.append(cr_latest)
    sc_latest = (
        StatusContext.objects.filter(pull_request=pr)
        .aggregate(m=Max("gh_created_at"))  # type: ignore[arg-type]
        .get("m")
    )
    if sc_latest:
        candidates.append(sc_latest)
    if not candidates:
        return None
    # Ensure aware
    latest = max(candidates)
    if timezone.is_naive(latest):
        latest = timezone.make_aware(latest)
    return latest


@transaction.atomic
def rebuild_pr_revisions(pr: PullRequest, latest_signal_ts: Optional[datetime] = None) -> RebuildResult:
    """Rebuild head revision windows for a PR from timeline events and CI.

    Preconditions
    - Requires full timeline backfill (`pr.timeline_backfill_done is True`). If not met,
      performs no work and returns created=deleted=0. Build state is left unchanged.

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
    - Updates PRRevisionBuildState to record builder_version, built_through_ts (latest signal
      considered), cleared dirty flag, tail pointer, and last_built_at. When state is clean and
      signals are strictly forward-only, only the tail windows are rewritten (append strategy).
    """
    state, _ = PRRevisionBuildState.objects.get_or_create(pull_request=pr)
    if latest_signal_ts is not None and timezone.is_naive(latest_signal_ts):
        latest_signal_ts = timezone.make_aware(latest_signal_ts)
    if latest_signal_ts is None:
        latest_signal_ts = _latest_signal_ts(pr) or state.built_through_ts

    if not getattr(pr, "timeline_backfill_done", False):
        return RebuildResult(created=0, deleted=0, strategy="skipped")

    needs_full = False
    strategy = "full"
    if state.builder_version != PR_REVISION_BUILDER_VERSION:
        needs_full = True
    if state.dirty_from_ts is not None:
        needs_full = True

    has_existing = PRRevision.objects.filter(pull_request=pr).exists()
    if not needs_full and state.built_through_ts and latest_signal_ts and latest_signal_ts <= state.built_through_ts:
        # Nothing new beyond what we have already processed; no work.
        return RebuildResult(created=0, deleted=0, strategy="noop")
    if not needs_full and state.built_through_ts is not None and has_existing:
        strategy = "append"

    fps: List[PRTimelineEvent] = list(
        PRTimelineEvent.objects.filter(pull_request=pr, type=PRTimelineEventType.HEAD_FORCE_PUSHED).order_by(
            "occurred_at",
            "id",
        )
    )
    ci_first_seen, ci_latest = _collect_ci_first_seen(pr)
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

    # When appending, preserve prefix windows and only rewrite from the current tail onward.
    seq_offset = 0
    append_from_ts: Optional[datetime] = None
    if strategy == "append":
        tail = PRRevision.objects.filter(pull_request=pr).order_by("-from_ts", "-seq", "-id").first()
        if tail:
            append_from_ts = tail.from_ts
            seq_offset = PRRevision.objects.filter(pull_request=pr, from_ts__lt=append_from_ts).count()
            expected = [(ft, sha, tt) for (ft, sha, tt) in expected if ft >= append_from_ts]
            if not expected:
                strategy = "noop"
    if strategy == "noop":
        return RebuildResult(created=0, deleted=0, strategy="noop")

    for seq, (from_ts, head_sha, to_ts) in enumerate(expected, start=seq_offset):
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
    if append_from_ts is not None:
        qs = qs.filter(from_ts__gte=append_from_ts)
    if expected_starts:
        qs = qs.exclude(from_ts__in=expected_starts)
    deleted, _ = qs.delete()

    tail = PRRevision.objects.filter(pull_request=pr).order_by("-from_ts", "-seq", "-id").first()
    tail_from_ts = tail.from_ts if tail else None

    built_through_ts = _compute_built_through_ts(pr, fps, ci_first_seen, ci_latest)
    now_ts = timezone.now()
    state.builder_version = PR_REVISION_BUILDER_VERSION
    state.built_through_ts = built_through_ts
    state.dirty_from_ts = None
    state.tail_revision = tail
    state.tail_from_ts = tail_from_ts
    state.last_built_at = now_ts
    state.revision_version = (state.revision_version or 0) + 1
    state.ci_checked_revision_version = None
    state.ci_checked_at = None
    state.windows_built_revision_version = None
    state.windows_built_at = None
    state.save(
        update_fields=[
            "builder_version",
            "built_through_ts",
            "dirty_from_ts",
            "tail_revision",
            "tail_from_ts",
            "last_built_at",
            "revision_version",
            "ci_checked_revision_version",
            "ci_checked_at",
            "windows_built_revision_version",
            "windows_built_at",
            "updated_at",
        ],
    )

    return RebuildResult(created=created, deleted=deleted, strategy=strategy)


def revision_candidate_shas(pr: PullRequest) -> list[str]:
    """Return ordered candidate head SHAs from force-push events and PRRevision rows."""
    seen: set[str] = set()
    candidates: list[tuple[datetime, str]] = []

    for ev in PRTimelineEvent.objects.filter(pull_request=pr, type=PRTimelineEventType.HEAD_FORCE_PUSHED).order_by(
        "occurred_at", "id"
    ):
        for sha in (ev.before_sha, ev.after_sha):
            if not sha or sha in seen:
                continue
            seen.add(sha)
            ts = ev.occurred_at or timezone.now()
            candidates.append((ts, sha))

    for rev in PRRevision.objects.filter(pull_request=pr).order_by("from_ts", "seq", "id"):
        sha = rev.head_sha or ""
        if not sha or sha in seen:
            continue
        seen.add(sha)
        candidates.append((rev.from_ts, sha))

    candidates.sort(key=lambda item: item[0])
    return [sha for _, sha in candidates]


def next_revision_backfill_shas(pr: PullRequest, limit: int = 2, *, skip_shas: set[str] | None = None) -> list[str]:
    """Return up to `limit` head SHAs whose CI appears missing or still pending.

    Heuristic: select candidate head SHAs (older first) where:
      - neither any CheckRun nor any StatusContext exists for that SHA, or
      - only pending StatusContexts exist (no completed states), or
      - any CheckRun is still queued or in-progress.
    Candidates include:
      - before/after SHAs from HEAD_FORCE_PUSHED timeline events
      - existing PRRevision heads
    If `limit` <= 0 returns an empty list.
    """
    if limit <= 0:
        return []
    candidates = revision_candidate_shas(pr)
    skip = skip_shas or set()

    # Snapshot CI presence up-front to avoid repeated queries per candidate.
    status_ctx_rows = StatusContext.objects.filter(pull_request=pr).values_list("head_sha", "state")
    sc_any: set[str] = set()
    sc_pending: set[str] = set()
    sc_completed: set[str] = set()
    for head_sha, state in status_ctx_rows:
        if not head_sha:
            continue
        sc_any.add(head_sha)
        if state == StatusContextState.PENDING:
            sc_pending.add(head_sha)
        else:
            sc_completed.add(head_sha)

    check_run_rows = CheckRun.objects.filter(pull_request=pr).values_list("head_sha", "status")
    cr_any: set[str] = set()
    cr_pending: set[str] = set()
    for head_sha, status in check_run_rows:
        if not head_sha:
            continue
        cr_any.add(head_sha)
        if status in (CheckRunStatus.QUEUED, CheckRunStatus.IN_PROGRESS):
            cr_pending.add(head_sha)

    shas: list[str] = []
    for sha in candidates:
        if sha in skip:
            continue
        has_cr = sha in cr_any
        has_sc = sha in sc_any
        pending_status_only = sha in sc_pending and sha not in sc_completed
        pending_check_run = sha in cr_pending
        missing_ci = not (has_cr or has_sc)
        if missing_ci or pending_status_only or pending_check_run:
            shas.append(sha)
            if len(shas) >= limit:
                break
    return shas
