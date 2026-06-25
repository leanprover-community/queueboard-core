"""One-off backfill: re-fetch CI-by-SHA for commits that may have missed a
newly-tracked check run.

Context
-------
``Repository.ci_tracked_checkrun_names`` is a *persistence-time* allowlist: the
syncer always pulls the full ``statusCheckRollup.contexts`` for a commit, but
``syncer.services.sub.ci_sync.sync_check_runs`` only upserts contexts whose name
matches the allowlist. When a new CI job is added upstream but the repo's
allowlist is not updated in lockstep, that job is fetched and then dropped for
every commit synced in the gap.

Because GitHub retains check runs and our upsert is idempotent, simply re-running
the affected SHAs through the (now-corrected) allowlist persists the missing
checkrun. This script enumerates the candidate SHAs and feeds them to
``syncer.sync_ci_for_repo_shas`` -- which bypasses the CIShaFetchState backoff
(that backoff only gates the automatic sweeps), is rate-budget aware, and
self-continues if it runs low on GitHub quota.

The SHA universe is intentionally slightly over-inclusive (re-fetching a SHA that
already has the job is a harmless no-op):
  1. PRRevision head SHAs whose window was active on/after the cutoff: closed
     windows that ended at/after the cutoff, plus the trailing open-ended window
     of any PR touched (gh_updated_at) on/after the cutoff. (The trailing window
     is never closed by the builder, so it is bounded by the PR's gh_updated_at
     to avoid pulling in the current head of every PR ever.)
  2. PullRequest.head_sha for PRs touched (gh_updated_at) on/after the cutoff.

Modes
-----
- DRY RUN (default): print candidate count + sample, enqueue nothing.
- ENQUEUE (REQUEUE_DRY_RUN=0): fan the SHAs out to worker dynos via ``.delay()``
  -- scalable and rate-safe. The per-result tally lands in each task's return
  value (django_celery_results / admin), not inline here.
- SYNC (REQUEUE_SYNC=1): run the fetch *inline in this process* via ``.apply()``,
  aggregate the per-SHA result tally, and print a before/after check-name count.
  Best for the smoke test on a modest window, since GitHub calls run from this
  one-off dyno rather than the workers.

Two diagnostics are produced when enabled:
- A per-SHA result tally (ok / noop / filtered / empty / not_found). A high
  ``filtered`` count after fixing the allowlist usually means the new entry does
  not actually match the checkrun name (substring/case mismatch, wrong repo).
- A before/after count of ``CommitCheckRun`` rows whose name matches
  ``REQUEUE_CHECK_NAME`` -- the definitive proof the new job landed.

Usage (Heroku)
--------------
  # Dry run -- just the counts:
  heroku run -a YOUR_APP -- bash -c \
    'REQUEUE_CHECK_NAME=your-new-job REQUEUE_DRY_RUN=1 \
     python qb_site/manage.py shell < scripts/requeue_ci_by_sha.py'

  # Smoke test inline (prints tally + before/after), good for a 1-2 day window:
  heroku run -a YOUR_APP -- bash -c \
    'REQUEUE_CHECK_NAME=your-new-job REQUEUE_DRY_RUN=0 REQUEUE_SYNC=1 \
     python qb_site/manage.py shell < scripts/requeue_ci_by_sha.py'

  # Distribute across workers (large window):
  heroku run -a YOUR_APP -- bash -c \
    'REQUEUE_CHECK_NAME=your-new-job REQUEUE_DRY_RUN=0 \
     python qb_site/manage.py shell < scripts/requeue_ci_by_sha.py'

Environment knobs
-----------------
  REQUEUE_REPO        owner/name of the repository       (default leanprover-community/mathlib4)
  REQUEUE_CUTOFF      ISO8601 lower bound; the date the   (default 2026-06-23T00:00:00Z)
                      check run started running upstream
  REQUEUE_DRY_RUN     "1"/"0" -- print only vs act        (default 1)
  REQUEUE_SYNC        "1"/"0" -- run inline vs enqueue     (default 0)
  REQUEUE_CHECK_NAME  checkrun name (substring) to count   (default unset -> count skipped)
                      before/after
  REQUEUE_BATCH_SIZE  SHAs per task                       (default 100)
  REQUEUE_MAX_PAGES   contexts pages fetched per SHA       (default = SYNCER_CI_BY_SHA_PAGES)
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone as _dt_tz

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from core.models import Repository
from syncer.models import CommitCheckRun, PullRequest
from analyzer.models import PRRevision
from syncer.tasks.sync_tasks import sync_ci_for_repo_shas_task


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "")


REPO = os.environ.get("REQUEUE_REPO", "leanprover-community/mathlib4")
CUTOFF_ISO = os.environ.get("REQUEUE_CUTOFF", "2026-06-23T00:00:00Z")
DRY_RUN = _env_flag("REQUEUE_DRY_RUN", True)
SYNC = _env_flag("REQUEUE_SYNC", False)
BATCH_SIZE = int(os.environ.get("REQUEUE_BATCH_SIZE", "100"))
MAX_PAGES = int(os.environ.get("REQUEUE_MAX_PAGES", str(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1))))
CHECK_NAME = os.environ.get("REQUEUE_CHECK_NAME", "").strip()

owner, _, name = REPO.partition("/")
if not owner or not name:
    raise SystemExit(f"REQUEUE_REPO must be 'owner/name', got {REPO!r}")
repo = Repository.objects.get(owner=owner, name=name)

cutoff = datetime.fromisoformat(CUTOFF_ISO.replace("Z", "+00:00"))
if timezone.is_naive(cutoff):
    cutoff = timezone.make_aware(cutoff, _dt_tz.utc)


def check_name_count() -> int | None:
    """Count CommitCheckRun rows whose name matches REQUEUE_CHECK_NAME, or None if unset."""
    if not CHECK_NAME:
        return None
    return CommitCheckRun.objects.filter(repository=repo, name__icontains=CHECK_NAME).count()


# 1) Revision head SHAs whose window was active at or after the cutoff.
#    A closed window [from_ts, to_ts) is "live" past the cutoff iff it ended
#    at/after the cutoff (to_ts >= cutoff).
#
#    The trailing window of every PR is open-ended (to_ts IS NULL) -- the
#    revision builder never closes it, even after the PR is merged/closed. So
#    `to_ts IS NULL` alone matches the current head of *every PR ever*, not just
#    recently-active ones. Bound the open-ended branch by the PR's gh_updated_at
#    so it only contributes heads of PRs touched on/after the cutoff (matching
#    query 2's notion of "active").
rev_shas = (
    PRRevision.objects.filter(pull_request__repository=repo)
    .filter(Q(to_ts__gte=cutoff) | Q(to_ts__isnull=True, pull_request__gh_updated_at__gte=cutoff))
    .values_list("head_sha", flat=True)
)

# 2) PR head SHAs for PRs touched at/after the cutoff (covers heads that may not
#    yet be materialized into PRRevision rows).
pr_shas = (
    PullRequest.objects.filter(repository=repo, gh_updated_at__gte=cutoff)
    .exclude(head_sha__isnull=True)
    .exclude(head_sha="")
    .values_list("head_sha", flat=True)
)

shas = sorted({s.strip() for s in list(rev_shas) + list(pr_shas) if s and s.strip()})

mode = "dry_run" if DRY_RUN else ("sync" if SYNC else "enqueue")
print(
    f"repo={REPO} cutoff={cutoff.isoformat()} candidate_shas={len(shas)} "
    f"batch_size={BATCH_SIZE} max_pages={MAX_PAGES} mode={mode} check_name={CHECK_NAME or '(unset)'}"
)

before_count = check_name_count()
if before_count is not None:
    print(f"CommitCheckRun rows matching name~='{CHECK_NAME}' (before): {before_count}")


def print_tally(tally: Counter, totals: dict[str, int]) -> None:
    if tally:
        print("Per-SHA result tally:")
        for key, val in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {key:>18}: {val}")
        if tally.get("filtered"):
            print(
                f"  note: {tally['filtered']} SHA(s) had contexts but none matched the allowlist -- "
                "if you expected the new job here, double-check ci_tracked_checkrun_names matches its name."
            )
    print(
        "Rows written: "
        f"checkruns_created={totals['checkruns_created']} checkruns_updated={totals['checkruns_updated']} "
        f"status_created={totals['status_created']} status_updated={totals['status_updated']}"
    )


if DRY_RUN:
    for s in shas[:20]:
        print("  ", s)
    if len(shas) > 20:
        print(f"   ... (+{len(shas) - 20} more)")
    print("DRY RUN: nothing run/enqueued. Re-run with REQUEUE_DRY_RUN=0 (add REQUEUE_SYNC=1 to run inline).")

elif SYNC:
    tally: Counter = Counter()
    totals = {"checkruns_created": 0, "checkruns_updated": 0, "status_created": 0, "status_updated": 0}
    deferred_batches = 0
    for i in range(0, len(shas), BATCH_SIZE):
        batch = shas[i : i + BATCH_SIZE]
        eager = sync_ci_for_repo_shas_task.apply(
            args=[repo.id],
            kwargs=dict(
                shas=batch,
                max_pages_per_sha=MAX_PAGES,
                require_pr_association=False,
                trigger_analyzer_after_sync=True,
            ),
        )
        if not eager.successful():
            print(f"  batch {i // BATCH_SIZE + 1}: FAILED: {eager.result!r}")
            continue
        res = eager.result if isinstance(eager.result, dict) else {}
        for key, val in (res.get("results_by_result") or {}).items():
            tally[str(key)] += int(val)
        for key in totals:
            totals[key] += int((res.get("counts") or {}).get(key, 0))
        status = str(res.get("status") or "")
        if status == "deferred":
            deferred_batches += 1
        print(
            f"  batch {i // BATCH_SIZE + 1}: {len(batch)} SHAs status={status or 'ok'} "
            f"done={len(res.get('shas_done', []))} impacted_prs={res.get('impacted_pr_count', 0)}"
        )
    print_tally(tally, totals)
    if deferred_batches:
        print(
            f"WARNING: {deferred_batches} batch(es) hit the GitHub rate budget and deferred their remaining "
            "SHAs to worker dynos (they will continue at resetAt). Re-run to confirm completion."
        )
    after_count = check_name_count()
    if before_count is not None and after_count is not None:
        print(
            f"CommitCheckRun rows matching name~='{CHECK_NAME}': "
            f"before={before_count} after={after_count} (+{after_count - before_count})"
        )

else:  # enqueue
    enqueued = 0
    for i in range(0, len(shas), BATCH_SIZE):
        batch = shas[i : i + BATCH_SIZE]
        res = sync_ci_for_repo_shas_task.delay(
            repo.id,
            shas=batch,
            max_pages_per_sha=MAX_PAGES,
            require_pr_association=False,
            trigger_analyzer_after_sync=True,
        )
        enqueued += 1
        print(f"  batch {enqueued}: {len(batch)} SHAs -> task {res.id}")
    print(f"Enqueued {enqueued} task(s) covering {len(shas)} SHAs for {REPO}.")
    print(
        "Per-SHA result tally is in each task's return value (django_celery_results / admin); "
        "use REQUEUE_SYNC=1 to run inline and print it here instead."
    )
    if CHECK_NAME:
        print(
            f"After the workers drain, re-run with REQUEUE_DRY_RUN=1 to see the updated count "
            f"for name~='{CHECK_NAME}' (current: {before_count})."
        )
