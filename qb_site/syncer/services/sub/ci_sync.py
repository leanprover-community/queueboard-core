from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from dateutil import parser as dtparser
from django.utils import timezone
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.db.models import Q

from analyzer.models import PRRevisionBuildState
from analyzer.services.revisions import mark_pr_revision_dirty_if_earlier
from syncer.models.commit_check_run import CommitCheckRun
from syncer.models.commit_status_context import CommitStatusContext
from syncer.models.pull_request import PullRequest
from core.utils.db import update_if_changed, upsert_if_changed
from django.db import transaction
import logging

log = logging.getLogger(__name__)


@dataclass
class CISyncResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    eligible: int = 0
    filtered: int = 0


@dataclass
class _RevisionSignal:
    name_key: str
    row_ts: timezone.datetime
    signal_ts: timezone.datetime


def _bump_latest_ci_synced_at(pr: PullRequest, now: timezone.datetime) -> None:
    """Advance PRRevisionBuildState.latest_ci_synced_at to ``now`` if newer.

    Drives the queue-window sweep's CI-staleness predicate (doc 045).
    Idempotent and monotone, even under concurrent CI sub-syncs for the
    same PR. Called once per CI sub-sync invocation that actually wrote
    anything (created or updated > 0 from this sub-sync's perspective),
    rather than per row, to avoid N+1 writes.

    A naive read-modify-write (``state = get_or_create(...); if now >
    state.x: state.x = now; state.save()``) has a lost-update race when
    two CI sub-syncs for the same PR run concurrently. The conditional
    UPDATE below resolves it inside the database. The WHERE clause also
    skips the write entirely when ``now <= latest_ci_synced_at``,
    matching the "no-op = no-write" precedent from rebuild-churn fixes
    (commits 088434e / 78c29cc / 73d0446) — failing this invariant
    would cause active PRs to be re-rebuilt on every sweep tick.
    ``auto_now=True`` on ``updated_at`` only fires on ``save()``, so
    the UPDATE sets it explicitly. The ``__lt OR __isnull`` pair
    handles the first-write case (column starts null).
    """
    PRRevisionBuildState.objects.get_or_create(pull_request=pr)
    PRRevisionBuildState.objects.filter(pull_request=pr).filter(
        Q(latest_ci_synced_at__lt=now) | Q(latest_ci_synced_at__isnull=True)
    ).update(latest_ci_synced_at=now, updated_at=now)


def _parse_iso(val: str | None):
    if not val:
        return None
    dt = dtparser.isoparse(val)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _parse_allowlist(val: Any) -> List[str]:
    if not val:
        return []
    s = str(val)
    return [tok.strip().lower() for tok in s.split(",") if tok.strip()]


def _effective_allowlist_for_checkruns(pr: PullRequest) -> List[str]:
    """Return the allowlist patterns for CheckRun contexts for a PR's repository."""
    repo_patterns = getattr(pr.repository, "ci_tracked_checkrun_names", None) or []
    if repo_patterns:
        return [str(p).strip().lower() for p in repo_patterns if str(p).strip()]
    mode = getattr(settings, "SYNCER_CI_FILTER_MODE", "all")
    if mode == "allowlist":
        return _parse_allowlist(getattr(settings, "SYNCER_CI_ALLOW_CHECKRUN_NAMES", ""))
    return []


def _effective_allowlist_for_status(pr: PullRequest) -> List[str]:
    """Return the allowlist patterns for StatusContext contexts for a PR's repository."""
    repo_patterns = getattr(pr.repository, "ci_tracked_status_names", None) or []
    if repo_patterns:
        return [str(p).strip().lower() for p in repo_patterns if str(p).strip()]
    mode = getattr(settings, "SYNCER_CI_FILTER_MODE", "all")
    if mode == "allowlist":
        return _parse_allowlist(getattr(settings, "SYNCER_CI_ALLOW_STATUS_NAMES", ""))
    return []


def _archive_mode_upsert(
    model: type,
    lookup: dict[str, Any],
    commit_values: dict[str, Any],
    fallback_lookup: dict[str, Any] | None,
) -> tuple[Any, bool, bool, tuple[str, ...]]:
    """Archive-mode CI upsert with NULL-stripping on the UPDATE path.

    Resolves the "merge-don't-overwrite" requirement from design doc 043
    §"CI upsert: merge-don't-overwrite for archive mode". Live ingest may
    have written ``external_id`` / ``gh_started_at`` / ``gh_completed_at``;
    a legacy archive payload arrives with those NULL because the legacy
    fragment doesn't request them. Standard ``update_if_changed`` would
    treat the NULLs as "changed" and downgrade the live row.

    On UPDATE, we strip NULLs from the values dict before the diff. On
    CREATE, we keep NULLs (best info we have, and the synthesized
    ``gh_created_at`` for StatusContext covers the NOT NULL constraint).

    ``fallback_lookup`` covers the live composite-key fallback path
    exercised by ``_upsert_commit_check_run`` (the repo+sha+name+external_id
    constraint). StatusContext does not have an analogous composite, so
    callers pass None there.
    """
    existing = model.objects.filter(**lookup).first()
    if existing is None and fallback_lookup is not None:
        existing = model.objects.filter(**fallback_lookup).first()
    if existing is not None:
        update_values = {k: v for k, v in commit_values.items() if v is not None}
        was_updated, updated_fields = update_if_changed(existing, update_values)
        return existing, False, was_updated, updated_fields
    try:
        with transaction.atomic():
            obj = model.objects.create(**lookup, **commit_values)
        return obj, True, False, tuple()
    except IntegrityError:
        # Lost a race with a concurrent writer; fall back to update path.
        existing = model.objects.filter(**lookup).first()
        if existing is None and fallback_lookup is not None:
            existing = model.objects.filter(**fallback_lookup).first()
        if existing is None:
            raise
        update_values = {k: v for k, v in commit_values.items() if v is not None}
        was_updated, updated_fields = update_if_changed(existing, update_values)
        return existing, False, was_updated, updated_fields


def _incoming_check_run_is_older(existing: CommitCheckRun, commit_values: dict[str, Any]) -> bool:
    """True when the incoming snapshot predates the stored row's timestamps.

    Used by the composite-conflict merge: GitHub Actions re-run attempts share
    the job's deterministic ``external_id``, and the rollup can carry both the
    superseded and the current attempt. Whichever attempt is processed second
    lands in the conflict fallback, so without this guard a superseded
    attempt's facts (e.g. the pre-re-run FAILURE) could clobber the newer
    attempt's row. Rows or snapshots without timestamps can't be ordered;
    they conservatively let the update proceed.
    """
    incoming = commit_values.get("gh_started_at") or commit_values.get("gh_completed_at")
    current = existing.gh_started_at or existing.gh_completed_at
    return incoming is not None and current is not None and incoming < current


def _upsert_commit_check_run(
    pr: PullRequest,
    values: dict[str, Any],
    gid: str,
    now: timezone.datetime,
    *,
    archive_mode: bool = False,
) -> tuple[bool, bool, tuple[str, ...]]:
    # GitHub's API occasionally delivers a non-null conclusion alongside a non-COMPLETED
    # status (e.g. IN_PROGRESS + CANCELLED) as a race condition during cancellation.
    # A non-null conclusion is authoritative: the run has ended, so normalise to COMPLETED.
    status = values["status"]
    if values.get("conclusion") is not None and status != "COMPLETED":
        status = "COMPLETED"
    commit_values = {
        "repository": pr.repository,
        "github_node_id": gid,
        "head_sha": values["head_sha"],
        "name": values["name"],
        "status": status,
        "conclusion": values["conclusion"],
        "details_url": values["details_url"],
        "external_id": values["external_id"],
        "gh_started_at": values["gh_started_at"],
        "gh_completed_at": values["gh_completed_at"],
    }
    if archive_mode:
        ext_id = values.get("external_id")
        fallback_lookup = (
            {
                "repository": pr.repository,
                "head_sha": values["head_sha"],
                "name": values["name"],
                "external_id": ext_id,
            }
            if ext_id
            else None
        )
        # commit_values already contains the lookup field (github_node_id);
        # peel it off so we don't pass it twice to model.objects.create.
        archive_values = {k: v for k, v in commit_values.items() if k != "github_node_id"}
        commit_obj, was_created, was_updated, updated_fields = _archive_mode_upsert(
            CommitCheckRun,
            {"github_node_id": gid},
            archive_values,
            fallback_lookup,
        )
        CommitCheckRun.objects.filter(pk=commit_obj.pk).update(last_synced_at=now)
        return was_created, was_updated, updated_fields
    try:
        commit_obj, was_created, was_updated, updated_fields = upsert_if_changed(
            CommitCheckRun,
            {"github_node_id": gid},
            commit_values,
        )
    except (IntegrityError, ObjectDoesNotExist):
        # Two shapes land here; both mean the composite (repo, sha, name,
        # external_id) is already owned by a row with a different node id:
        #   - CREATE collided: gid unseen, e.g. a re-run attempt sharing the
        #     job's deterministic external_id with the row that owns it.
        #   - UPDATE collided: gid matched a row whose composite differs —
        #     typically an archive-created duplicate with external_id NULL
        #     (doc 043: legacy payloads carry no externalId, so the importer
        #     could not composite-match and created a second row).
        fallback_obj = None
        ext_id = values.get("external_id")
        if ext_id:
            fallback_obj = CommitCheckRun.objects.filter(
                repository=pr.repository,
                head_sha=values["head_sha"],
                name=values["name"],
                external_id=ext_id,
            ).first()
        if fallback_obj is None:
            log.warning(
                "CommitCheckRun dual-write conflict without fallback row for %s sha=%s gid=%s name=%s external_id=%s",
                pr.repository,
                values.get("head_sha"),
                gid,
                values.get("name"),
                ext_id,
            )
            return False, False, tuple()
        # Merge: the composite owner survives. Delete any other row still
        # holding this gid (the UPDATE-collision loser) so the owner can take
        # over the node id without tripping cckr_nodeid_uniq. Its FKs are all
        # SET_NULL, matching what the daily superseded-row expiry would do.
        CommitCheckRun.objects.filter(github_node_id=gid).exclude(pk=fallback_obj.pk).delete()
        if _incoming_check_run_is_older(fallback_obj, commit_values):
            was_created = False
            was_updated = False
            updated_fields = tuple()
            commit_obj = fallback_obj
        else:
            try:
                was_updated, updated_fields = update_if_changed(fallback_obj, commit_values, savepoint=True)
                was_created = False
                commit_obj = fallback_obj
            except IntegrityError:
                log.warning(
                    "CommitCheckRun fallback update conflict for %s sha=%s gid=%s name=%s external_id=%s",
                    pr.repository,
                    values.get("head_sha"),
                    gid,
                    values.get("name"),
                    ext_id,
                )
                return False, False, tuple()
    CommitCheckRun.objects.filter(pk=commit_obj.pk).update(last_synced_at=now)
    return was_created, was_updated, updated_fields


def _upsert_commit_status_context(
    pr: PullRequest,
    values: dict[str, Any],
    gid: str,
    now: timezone.datetime,
    *,
    archive_mode: bool = False,
) -> tuple[bool, bool, tuple[str, ...]]:
    commit_values = {
        "repository": pr.repository,
        "github_node_id": gid,
        "head_sha": values["head_sha"],
        "name": values["name"],
        "state": values["state"],
        "target_url": values["target_url"],
        "description": values["description"],
        "gh_created_at": values["gh_created_at"],
    }
    if archive_mode:
        archive_values = {k: v for k, v in commit_values.items() if k != "github_node_id"}
        commit_obj, was_created, was_updated, updated_fields = _archive_mode_upsert(
            CommitStatusContext,
            {"github_node_id": gid},
            archive_values,
            None,  # No composite-key fallback for StatusContext (doc 043 §out of scope).
        )
        CommitStatusContext.objects.filter(pk=commit_obj.pk).update(last_synced_at=now)
        return was_created, was_updated, updated_fields
    try:
        commit_obj, was_created, was_updated, updated_fields = upsert_if_changed(
            CommitStatusContext,
            {"github_node_id": gid},
            commit_values,
        )
    except (IntegrityError, ObjectDoesNotExist):
        fallback_obj = CommitStatusContext.objects.filter(github_node_id=gid).first()
        if fallback_obj is None:
            log.warning(
                "CommitStatusContext dual-write conflict without fallback row for %s sha=%s gid=%s name=%s",
                pr.repository,
                values.get("head_sha"),
                gid,
                values.get("name"),
            )
            return False, False, tuple()
        try:
            was_updated, updated_fields = update_if_changed(fallback_obj, commit_values, savepoint=True)
            was_created = False
            commit_obj = fallback_obj
        except IntegrityError:
            log.warning(
                "CommitStatusContext fallback update conflict for %s sha=%s gid=%s name=%s",
                pr.repository,
                values.get("head_sha"),
                gid,
                values.get("name"),
            )
            return False, False, tuple()
    CommitStatusContext.objects.filter(pk=commit_obj.pk).update(last_synced_at=now)
    return was_created, was_updated, updated_fields


def sync_check_runs(
    pr: PullRequest,
    contexts: Iterable[Dict[str, Any]],
    head_sha: str,
    *,
    archive_mode: bool = False,
) -> CISyncResult:
    """Upsert snapshot CheckRun rows from a commit's status.contexts entries.

    Inputs are the subset of contexts where __typename == "CheckRun" with keys:
      {"id": str, "name": str, "status": str, "conclusion": str | None,
       "startedAt": str | None, "completedAt": str | None, "detailsUrl": str | None,
       "externalId": str | None}

    The head_sha for these contexts must be passed alongside and stored on each CheckRun row.

    Archive mode (design doc 043): when ``archive_mode=True``, the underlying
    upsert strips NULL values from the update path so a legacy payload's
    missing ``external_id`` / ``startedAt`` / ``completedAt`` does not
    overwrite live's non-null values. CREATE path is unchanged (NULLs are
    persisted as-is, since they're the best info the archive carries).
    """
    created = 0
    updated = 0
    eligible = 0
    filtered = 0
    allow = _effective_allowlist_for_checkruns(pr)
    if allow:
        log.debug("CI sync: using CheckRun allowlist for %s (patterns=%s)", pr.repository, allow)
    now = timezone.now()
    earliest_ts = None
    latest_by_name: dict[str, timezone.datetime] = {}
    revision_signals: list[_RevisionSignal] = []
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        gid = ctx.get("id")
        if not gid:
            continue
        # Optional allow-list filter by name (case-insensitive substring)
        if allow:
            nm = (ctx.get("name") or "").lower()
            if not any(pat in nm for pat in allow):
                log.debug("CI sync: skipping CheckRun %s due to allowlist (pat=%s)", nm, allow)
                filtered += 1
                continue
        if (ctx.get("conclusion") or "").upper() == "SKIPPED":
            log.debug("CI sync: skipping CheckRun %s due to SKIPPED conclusion", ctx.get("name"))
            filtered += 1
            continue
        eligible += 1
        values = {
            "head_sha": head_sha,
            "name": ctx.get("name") or "",
            "status": ctx.get("status") or "",
            "conclusion": ctx.get("conclusion"),
            "details_url": ctx.get("detailsUrl") or None,
            "external_id": ctx.get("externalId") or None,
            "gh_started_at": _parse_iso(ctx.get("startedAt")),
            "gh_completed_at": _parse_iso(ctx.get("completedAt")),
        }
        was_created = False
        was_updated = False
        updated_fields: tuple[str, ...] = tuple()
        commit_created, commit_updated, commit_updated_fields = _upsert_commit_check_run(
            pr, values, gid, now, archive_mode=archive_mode
        )
        was_created = commit_created
        was_updated = commit_updated
        updated_fields = commit_updated_fields

        created += 1 if was_created else 0
        updated += 1 if was_updated else 0

        # Only treat CI as a revision-boundary signal when evidence changed:
        # newly-seen rows or updates that affect head/timestamps.
        touches_revision_signal = was_created or bool({"head_sha", "gh_started_at", "gh_completed_at"} & set(updated_fields))
        ts = values["gh_completed_at"] or values["gh_started_at"]
        name_key = (values["name"] or "").strip().lower()
        row_ts = values["gh_completed_at"] or values["gh_started_at"]
        signal_ts = values["gh_started_at"] or values["gh_completed_at"]
        if touches_revision_signal and name_key and signal_ts is not None and row_ts is not None:
            revision_signals.append(_RevisionSignal(name_key=name_key, row_ts=row_ts, signal_ts=signal_ts))
        if name_key and ts is not None:
            current_latest = latest_by_name.get(name_key)
            if current_latest is None or ts > current_latest:
                latest_by_name[name_key] = ts

    # Only use revision signals from the newest snapshot per context name.
    # The GraphQL rollup can include older rows that we prune below; those
    # should not repeatedly dirty revision state.
    for signal in revision_signals:
        latest_for_name = latest_by_name.get(signal.name_key)
        if latest_for_name is None or signal.row_ts != latest_for_name:
            continue
        if earliest_ts is None or signal.signal_ts < earliest_ts:
            earliest_ts = signal.signal_ts

    if earliest_ts:
        mark_pr_revision_dirty_if_earlier(pr, earliest_ts)

    if created > 0 or updated > 0:
        _bump_latest_ci_synced_at(pr, now)

    return CISyncResult(created=created, updated=updated, deleted=0, eligible=eligible, filtered=filtered)


def sync_status_contexts(
    pr: PullRequest,
    contexts: Iterable[Dict[str, Any]],
    head_sha: str,
    *,
    archive_mode: bool = False,
) -> CISyncResult:
    """Upsert snapshot StatusContext rows from a commit's status.contexts entries.

    Inputs are the subset of contexts where __typename == "StatusContext" with keys:
      {"id": str, "context": str, "state": str, "targetUrl": str | None,
       "description": str | None, "createdAt": str}

    The head_sha for these contexts must be passed alongside and stored on each row.

    Archive mode (design doc 043): same NULL-stripping merge semantics as
    ``sync_check_runs``. The legacy fragment also lacks ``createdAt`` for
    StatusContext; callers must pre-fill ``gh_created_at`` to a placeholder
    (typically the per-PR archive timestamp) before invoking this function.
    """
    created = 0
    updated = 0
    eligible = 0
    filtered = 0
    allow = _effective_allowlist_for_status(pr)
    if allow:
        log.debug("CI sync: using StatusContext allowlist for %s (patterns=%s)", pr.repository, allow)
    now = timezone.now()
    earliest_ts = None
    latest_by_name: dict[str, timezone.datetime] = {}
    revision_signals: list[_RevisionSignal] = []
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        gid = ctx.get("id")
        if not gid:
            continue
        # Optional allow-list filter by context name (case-insensitive substring)
        if allow:
            nm = (ctx.get("context") or "").lower()
            if not any(pat in nm for pat in allow):
                log.debug("CI sync: skipping StatusContext %s due to allowlist (pat=%s)", nm, allow)
                filtered += 1
                continue
        eligible += 1
        values = {
            "head_sha": head_sha,
            "name": ctx.get("context") or "",
            "state": ctx.get("state") or "",
            "target_url": ctx.get("targetUrl") or None,
            "description": ctx.get("description") or None,
            "gh_created_at": _parse_iso(ctx.get("createdAt")) or timezone.now(),
        }
        was_created = False
        was_updated = False
        updated_fields: tuple[str, ...] = tuple()
        commit_created, commit_updated, commit_updated_fields = _upsert_commit_status_context(
            pr, values, gid, now, archive_mode=archive_mode
        )
        was_created = commit_created
        was_updated = commit_updated
        updated_fields = commit_updated_fields

        created += 1 if was_created else 0
        updated += 1 if was_updated else 0

        ts = values["gh_created_at"]
        name_key = (values["name"] or "").strip().lower()
        touches_revision_signal = was_created or bool({"head_sha", "gh_created_at"} & set(updated_fields))
        if touches_revision_signal and name_key and ts is not None:
            revision_signals.append(_RevisionSignal(name_key=name_key, row_ts=ts, signal_ts=ts))
        if name_key and ts is not None:
            current_latest = latest_by_name.get(name_key)
            if current_latest is None or ts > current_latest:
                latest_by_name[name_key] = ts

    # Only use revision signals from the newest snapshot per context name.
    # The GraphQL rollup can include older rows that we prune below; those
    # should not repeatedly dirty revision state.
    for signal in revision_signals:
        latest_for_name = latest_by_name.get(signal.name_key)
        if latest_for_name is None or signal.signal_ts != latest_for_name:
            continue
        if earliest_ts is None or signal.signal_ts < earliest_ts:
            earliest_ts = signal.signal_ts

    if earliest_ts:
        mark_pr_revision_dirty_if_earlier(pr, earliest_ts)

    if created > 0 or updated > 0:
        _bump_latest_ci_synced_at(pr, now)

    return CISyncResult(created=created, updated=updated, deleted=0, eligible=eligible, filtered=filtered)
