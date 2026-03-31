from __future__ import annotations

from typing import Any, Dict, Callable, Optional
from datetime import datetime, timedelta, timezone as pytimezone

from dateutil import parser as dtparser
from django.db import transaction
from django.utils import timezone
from django.conf import settings

from core.models.repository import Repository
from syncer.services.github_client import GitHubClient
from syncer.services.sub.pull_request_sync import upsert_pull_request
from syncer.services.sub.labels_sync import sync_label_catalog, sync_pr_labels
from syncer.services.sub.timeline_sync import sync_timeline_events
from analyzer.models import ReviewerOptOut
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts
from syncer.services.sub.core_entities_sync import upsert_repo_metadata
from syncer.models.pull_request import PullRequest


class PRSyncService:
    """Orchestrates ingestion of a single PR.

    Provides two entry points:
      - sync_pull_request_bundle: ingest from an already-fetched GraphQL bundle (easy to test)
      - sync_pull_request: fetch bundle via GitHub client and ingest
    """

    @staticmethod
    def _normalize_login(login: str | None) -> str:
        return (login or "").strip().lower()

    @staticmethod
    def _parse_iso(val: str | None):
        if not val:
            return None
        try:
            dt = dtparser.isoparse(val)
        except Exception:
            return None
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt

    def _apply_assignment_opt_outs(self, pr_obj: PullRequest, events: list[dict]) -> None:
        if not events:
            return
        last_seen = pr_obj.last_assignment_event_at
        updates: list[tuple[datetime, str, str]] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            typename = ev.get("__typename")
            if typename not in ("AssignedEvent", "UnassignedEvent"):
                continue
            assignee = (ev.get("assignee") or {}).get("login")
            if not assignee:
                continue
            occurred_at = self._parse_iso(ev.get("createdAt"))
            if occurred_at is None:
                continue
            if last_seen is not None and occurred_at <= last_seen:
                continue
            updates.append((occurred_at, typename, assignee))

        if not updates:
            return

        updates.sort(key=lambda row: row[0])
        max_seen = last_seen
        for occurred_at, typename, assignee in updates:
            login = self._normalize_login(assignee)
            if not login:
                continue
            if typename == "UnassignedEvent":
                ReviewerOptOut.objects.update_or_create(
                    repository=pr_obj.repository,
                    pr_number=pr_obj.number,
                    reviewer_login=login,
                    defaults={
                        "active": True,
                        "opted_out_at": occurred_at,
                        "cleared_at": None,
                    },
                )
            else:
                ReviewerOptOut.objects.filter(
                    repository=pr_obj.repository,
                    pr_number=pr_obj.number,
                    reviewer_login=login,
                    active=True,
                ).update(active=False, cleared_at=occurred_at)
            if max_seen is None or occurred_at > max_seen:
                max_seen = occurred_at
        if max_seen is not None and max_seen != last_seen:
            pr_obj.last_assignment_event_at = max_seen
            pr_obj.save(update_fields=["last_assignment_event_at", "updated_at"])

    @transaction.atomic
    def sync_pull_request_bundle(self, repo: Repository, pr_bundle: Dict[str, Any], *, dry_run: bool = False) -> Dict[str, int]:
        # Upsert PR core
        upsert_res = upsert_pull_request(pr_bundle, repo)
        pr_obj = upsert_res.pr

        # Labels
        bundle_labels = [n for n in ((pr_bundle.get("labels") or {}).get("nodes") or []) if isinstance(n, dict)]
        lab_res = sync_label_catalog(repo, bundle_labels)
        label_names = [str(n.get("name")) for n in bundle_labels if isinstance(n, dict) and n.get("name")]
        attach_res = sync_pr_labels(pr_obj, label_names)

        # Timeline events
        tl_nodes = (pr_bundle.get("timelineItems") or {}).get("nodes") or []
        tl_res = sync_timeline_events(pr_obj, tl_nodes)
        self._apply_assignment_opt_outs(pr_obj, tl_nodes)

        # CI snapshots per commit
        checkruns_upserted = 0
        statusctx_upserted = 0
        head_ci_state: str | None = None
        head_commit_at = None
        head_oid = pr_bundle.get("headRefOid") or ""

        for cnode in (pr_bundle.get("commits") or {}).get("nodes") or []:
            commit = (cnode or {}).get("commit") or {}
            sha = commit.get("oid") or ""
            contexts = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
            rollup_state = (commit.get("statusCheckRollup") or {}).get("state")
            committed_at = self._parse_iso(commit.get("committedDate"))
            if head_oid and sha == head_oid:
                if committed_at is not None:
                    head_commit_at = committed_at
                if rollup_state is not None:
                    head_ci_state = str(rollup_state)
            cr_contexts = [c for c in contexts if isinstance(c, dict) and c.get("__typename") == "CheckRun"]
            sc_contexts = [c for c in contexts if isinstance(c, dict) and c.get("__typename") == "StatusContext"]
            cr_res = sync_check_runs(pr_obj, cr_contexts, sha)
            sc_res = sync_status_contexts(pr_obj, sc_contexts, sha)
            checkruns_upserted += cr_res.created + cr_res.updated
            statusctx_upserted += sc_res.created + sc_res.updated

        # Engagement fields: files, assignees, approvals, commenters, comment totals.
        extras: Dict[str, Any] = {}
        now_ts = timezone.now()

        # If the bundle was fetched successfully but no head rollup exists, stamp UNAVAILABLE for stale PRs
        if head_ci_state is None:
            head_activity_ts = head_commit_at
            updated_at = pr_obj.gh_updated_at
            if updated_at is not None and timezone.is_naive(updated_at):
                updated_at = timezone.make_aware(updated_at)
            if updated_at is not None and (head_activity_ts is None or updated_at > head_activity_ts):
                head_activity_ts = updated_at
            created_at = pr_obj.gh_created_at
            if created_at is not None and timezone.is_naive(created_at):
                created_at = timezone.make_aware(created_at)
            if created_at is not None and (head_activity_ts is None or created_at > head_activity_ts):
                head_activity_ts = created_at
            if head_activity_ts is not None and now_ts - head_activity_ts >= timedelta(days=365):
                head_ci_state = "UNAVAILABLE"

        files_conn = pr_bundle.get("files") or {}
        file_nodes = [n for n in (files_conn.get("nodes") or []) if isinstance(n, dict)]
        file_paths = [str(n.get("path")) for n in file_nodes if n.get("path")]
        files_has_more = bool((files_conn.get("pageInfo") or {}).get("hasNextPage"))
        extras["files"] = file_paths
        extras["files_incomplete"] = files_has_more or (pr_obj.changed_files_count > len(file_paths))

        assignees_conn = pr_bundle.get("assignees") or {}
        assignee_nodes = [n for n in (assignees_conn.get("nodes") or []) if isinstance(n, dict)]
        assignees = [str(n.get("login")) for n in assignee_nodes if n.get("login")]
        assignees_total = assignees_conn.get("totalCount")
        assignees_has_more = bool((assignees_conn.get("pageInfo") or {}).get("hasNextPage"))
        extras["assignees"] = assignees
        extras["assignees_incomplete"] = bool(
            assignees_has_more or (isinstance(assignees_total, int) and assignees_total > len(assignees))
        )
        old_assignees = {self._normalize_login(a) for a in (pr_obj.assignees or []) if a}
        new_assignees = {self._normalize_login(a) for a in assignees if a}
        added_assignees = new_assignees - old_assignees
        if added_assignees:
            ReviewerOptOut.objects.filter(
                repository=repo,
                pr_number=pr_obj.number,
                reviewer_login__in=sorted(added_assignees),
                active=True,
            ).update(active=False, cleared_at=now_ts)

        reviews_conn = pr_bundle.get("reviews") or {}
        review_nodes = [n for n in (reviews_conn.get("nodes") or []) if isinstance(n, dict)]
        review_authors = []
        approvals = []
        for node in review_nodes:
            author = node.get("author") or {}
            login = author.get("login")
            if login:
                review_authors.append(login)
                if str(node.get("state", "")).upper() == "APPROVED":
                    approvals.append(login)
        reviews_total = reviews_conn.get("totalCount")
        reviews_has_more = bool((reviews_conn.get("pageInfo") or {}).get("hasNextPage"))
        extras["approvals"] = sorted(set(approvals))
        extras["reviews_incomplete"] = bool(
            reviews_has_more or (isinstance(reviews_total, int) and reviews_total > len(review_nodes))
        )

        comments_conn = pr_bundle.get("comments") or {}
        comment_nodes = [n for n in (comments_conn.get("nodes") or []) if isinstance(n, dict)]
        comment_authors = [
            str((n.get("author") or {}).get("login")) for n in comment_nodes if (n.get("author") or {}).get("login")
        ]
        issue_comments_total = comments_conn.get("totalCount")
        comments_has_more = bool((comments_conn.get("pageInfo") or {}).get("hasNextPage"))
        issue_comments_count = issue_comments_total if isinstance(issue_comments_total, int) else len(comment_nodes)

        review_threads_conn = pr_bundle.get("reviewThreads") or {}
        review_threads_nodes = [n for n in (review_threads_conn.get("nodes") or []) if isinstance(n, dict)]
        review_threads_total = review_threads_conn.get("totalCount")
        review_threads_has_more = bool((review_threads_conn.get("pageInfo") or {}).get("hasNextPage"))
        review_comments_count = 0
        for thread in review_threads_nodes:
            comments = thread.get("comments") or {}
            t_total = comments.get("totalCount")
            if isinstance(t_total, int):
                review_comments_count += t_total
        extras["comments_incomplete"] = bool(
            comments_has_more
            or review_threads_has_more
            or (isinstance(issue_comments_total, int) and issue_comments_total > len(comment_nodes))
            or (isinstance(review_threads_total, int) and review_threads_total > len(review_threads_nodes))
        )
        commenters = sorted(set(comment_authors + review_authors))
        extras["commenters"] = commenters
        total_comments = None
        if isinstance(issue_comments_count, int) and isinstance(review_comments_count, int):
            total_comments = int(issue_comments_count) + int(review_comments_count)
        extras["number_total_comments"] = total_comments

        extras["engagement_synced_at"] = now_ts

        update_fields: list[str] = []
        ci_update_fields: list[str] = []
        if head_ci_state is not None and pr_obj.head_ci_state != head_ci_state:
            pr_obj.head_ci_state = head_ci_state
            ci_update_fields.append("head_ci_state")

        for field, value in extras.items():
            if getattr(pr_obj, field) != value:
                setattr(pr_obj, field, value)
                update_fields.append(field)
        if "engagement_synced_at" not in update_fields:
            pr_obj.engagement_synced_at = now_ts
            update_fields.append("engagement_synced_at")
        # Advance last_synced_at here, after all engagement fields are prepared, so
        # that the skip check never sees a PR as up-to-date when assignees (or other
        # engagement data) were not yet persisted.
        pr_obj.last_synced_at = now_ts
        if update_fields or ci_update_fields:
            pr_obj.save(update_fields=update_fields + ci_update_fields + ["updated_at", "last_synced_at"])

        result = {
            "labels_created": lab_res.created,
            "labels_updated": lab_res.updated,
            "prlabels_created": attach_res.created,
            "prlabels_deleted": attach_res.deleted,
            "events_created": tl_res.created,
            "checkruns_upserted": checkruns_upserted,
            "statusctx_upserted": statusctx_upserted,
        }

        if dry_run:
            transaction.set_rollback(True)
        return result

    def sync_pull_request(
        self,
        repo: Repository,
        *,
        number: int,
        client: GitHubClient,
        timelineK: int = 150,
        commitsM: int = 15,
        max_timeline_pages: int = 0,
        max_commit_pages: int = 0,
        dry_run: bool = False,
        rate_log: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        timeline_since_iso_override: Optional[str] = None,
        backfill_timeline_pages: int = 0,
        backfill_commit_pages: int = 0,
    ) -> Dict[str, int]:
        # Determine timeline since cutoff.
        # If an explicit override is provided (e.g., historical backfill), prefer it.
        # Otherwise use last_synced_at - epsilon when available.
        timeline_since_iso: Optional[str] = timeline_since_iso_override
        if timeline_since_iso is None:
            existing = PullRequest.objects.filter(repository=repo, number=number).only("last_synced_at").first()
            if existing and existing.last_synced_at:
                # subtract a small epsilon to avoid boundary-equal misses
                eps = int(getattr(settings, "SYNCER_LAST_SYNC_EPSILON_SECONDS", 2))
                dt = existing.last_synced_at - timedelta(seconds=max(0, eps))
                if timezone.is_naive(dt):
                    dt = timezone.make_aware(dt)
                dt_utc = dt.astimezone(pytimezone.utc)
                timeline_since_iso = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Fetch bundle and delegate to bundle-based ingestion
        data = client.get_pr_bundle(
            owner=repo.owner,
            name=repo.name,
            number=number,
            timelineK=timelineK,
            commitsM=commitsM,
            timeline_since_iso=timeline_since_iso,
        )
        repo_node = (data.get("data") or {}).get("repository", {})
        # Persist repository node id if present
        dbr = repo_node.get("defaultBranchRef") or {}
        default_branch = dbr.get("name") if isinstance(dbr, dict) else None
        upsert_repo_metadata(
            repo,
            repo_gid=repo_node.get("id"),
            owner_login=(repo_node.get("owner") or {}).get("login"),
            name=repo_node.get("name"),
            default_branch=default_branch,
            allow_rename=False,
        )
        pr = repo_node.get("pullRequest")
        if not pr:
            raise RuntimeError("GraphQL response missing data.repository.pullRequest")
        # First page ingestion
        result = self.sync_pull_request_bundle(repo, pr, dry_run=dry_run)
        # Load PR object for possible paging/backfill steps
        pr_obj = PullRequest.objects.get(repository=repo, number=number)
        # Seed timeline backfill state from bundle pageInfo if missing.
        # Only mark done when the bundle is unfiltered (no timelineSince) so we
        # don't treat a filtered window as full history.
        tl_conn0 = pr.get("timelineItems") or {}
        page0 = tl_conn0.get("pageInfo") or {}
        timeline_seed_updates: list[str] = []
        if not pr_obj.timeline_backfill_cursor:
            start_cur = page0.get("startCursor")
            if start_cur:
                pr_obj.timeline_backfill_cursor = start_cur
                timeline_seed_updates.append("timeline_backfill_cursor")
        if timeline_since_iso is None and not bool(page0.get("hasPreviousPage")) and not pr_obj.timeline_backfill_done:
            pr_obj.timeline_backfill_done = True
            timeline_seed_updates.append("timeline_backfill_done")
        if timeline_seed_updates:
            pr_obj.save(update_fields=timeline_seed_updates)
        # Seed commits backfill state from bundle pageInfo if missing.
        # Note: we only mark commits_backfill_done=True here when the bundle
        # already includes the entire commits connection (hasPreviousPage=False),
        # and never force it back to False (monotone semantics).
        c_conn0 = pr.get("commits") or {}
        c_page0 = c_conn0.get("pageInfo") or {}
        commit_seed_updates: list[str] = []
        if not pr_obj.commits_backfill_cursor:
            c_start = c_page0.get("startCursor")
            if c_start:
                pr_obj.commits_backfill_cursor = c_start
                commit_seed_updates.append("commits_backfill_cursor")
        if not bool(c_page0.get("hasPreviousPage")) and not pr_obj.commits_backfill_done:
            pr_obj.commits_backfill_done = True
            commit_seed_updates.append("commits_backfill_done")
        if commit_seed_updates:
            pr_obj.save(update_fields=commit_seed_updates)
        # Log bundle query cost if available
        if rate_log is not None:
            rl = client.get_last_rate_limit()
            if isinstance(rl, dict):
                rate_log("pr_bundle", rl)

        # Optional pagination/backfill (capped)
        if max_timeline_pages > 0 or max_commit_pages > 0 or backfill_timeline_pages > 0 or backfill_commit_pages > 0:
            # Rate guard: skip pagination/backfill when remaining budget is low
            try:
                rl_now = client.get_last_rate_limit() or {}
                remaining_now = rl_now.get("remaining") if isinstance(rl_now, dict) else None
                threshold_now = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))
                if isinstance(remaining_now, int) and remaining_now <= threshold_now:
                    return result
            except Exception:
                pass
            # Timeline paging
            if max_timeline_pages > 0:
                tl_conn = pr.get("timelineItems") or {}
                page = tl_conn.get("pageInfo") or {}
                after = page.get("endCursor")
                has_next = bool(page.get("hasNextPage"))
                pages = 0
                while has_next and pages < max_timeline_pages:
                    tdata = client.get_timeline_page(
                        owner=repo.owner,
                        name=repo.name,
                        number=number,
                        first=timelineK,
                        after=after,
                        since_iso=timeline_since_iso,
                    )
                    tpr = ((tdata.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
                    titems = tpr.get("timelineItems") or {}
                    nodes = titems.get("nodes") or []
                    tl_res = sync_timeline_events(pr_obj, nodes)
                    self._apply_assignment_opt_outs(pr_obj, nodes)
                    result["events_created"] += tl_res.created
                    if rate_log is not None:
                        rl = client.get_last_rate_limit()
                        if isinstance(rl, dict):
                            rate_log("timeline_page", rl)
                    pinfo = titems.get("pageInfo") or {}
                    has_next = bool(pinfo.get("hasNextPage"))
                    after = pinfo.get("endCursor")
                    pages += 1

            # Backfill older timeline pages with a fixed budget
            if backfill_timeline_pages > 0 and not pr_obj.timeline_backfill_done:
                # Allow seeding when cursor is missing by calling with before=None
                before = pr_obj.timeline_backfill_cursor
                pages = 0
                while pages < backfill_timeline_pages and not pr_obj.timeline_backfill_done:
                    tdata = client.get_timeline_page_back(
                        owner=repo.owner,
                        name=repo.name,
                        number=number,
                        last=timelineK,
                        before=before,
                    )
                    tpr = ((tdata.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
                    titems = tpr.get("timelineItems") or {}
                    nodes = titems.get("nodes") or []
                    tl_res = sync_timeline_events(pr_obj, nodes)
                    result["events_created"] += tl_res.created
                    # Update earliest timestamp if present
                    if nodes:
                        try:
                            times = [n.get("createdAt") for n in nodes if isinstance(n, dict) and n.get("createdAt")]
                            if times:
                                from dateutil import parser as _dtp

                                ts = [_dtp.isoparse(x) for x in times]
                                ts = [timezone.make_aware(t) if timezone.is_naive(t) else t for t in ts]
                                mn = min(ts)
                                if not pr_obj.timeline_earliest_synced_at or mn < pr_obj.timeline_earliest_synced_at:
                                    pr_obj.timeline_earliest_synced_at = mn
                        except Exception:
                            pass
                    # Update cursor/done flags
                    pinfo = titems.get("pageInfo") or {}
                    pr_obj.timeline_backfill_done = not bool(pinfo.get("hasPreviousPage"))
                    before = pinfo.get("startCursor")
                    pr_obj.timeline_backfill_cursor = before
                    pr_obj.save(
                        update_fields=["timeline_backfill_cursor", "timeline_backfill_done", "timeline_earliest_synced_at"]
                    )
                    if rate_log is not None:
                        rl = client.get_last_rate_limit()
                        if isinstance(rl, dict):
                            rate_log("timeline_page_back", rl)
                    pages += 1

            # Commits paging (older via before) — capped pages only (no time-based cutoff in V1)
            if max_commit_pages > 0 and not pr_obj.commits_backfill_done:
                c_conn = pr.get("commits") or {}
                c_page = c_conn.get("pageInfo") or {}
                before = c_page.get("startCursor")
                has_prev = bool(c_page.get("hasPreviousPage"))
                pages = 0
                while has_prev and pages < max_commit_pages:
                    cdata = client.get_commits_page(owner=repo.owner, name=repo.name, number=number, last=commitsM, before=before)
                    cpr = ((cdata.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
                    commits = cpr.get("commits") or {}
                    nodes = commits.get("nodes") or []
                    earliest_candidates: list[str] = []
                    for cnode in nodes:
                        commit = (cnode or {}).get("commit") or {}
                        sha = commit.get("oid") or ""
                        contexts = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
                        cr_contexts = [c for c in contexts if isinstance(c, dict) and c.get("__typename") == "CheckRun"]
                        sc_contexts = [c for c in contexts if isinstance(c, dict) and c.get("__typename") == "StatusContext"]
                        cr_res = sync_check_runs(pr_obj, cr_contexts, sha)
                        sc_res = sync_status_contexts(pr_obj, sc_contexts, sha)
                        result["checkruns_upserted"] += cr_res.created + cr_res.updated
                        result["statusctx_upserted"] += sc_res.created + sc_res.updated
                        # collect earliest CI timestamps on this page
                        for ctx in cr_contexts:
                            if isinstance(ctx, dict):
                                if ctx.get("completedAt"):
                                    earliest_candidates.append(ctx.get("completedAt"))
                                elif ctx.get("startedAt"):
                                    earliest_candidates.append(ctx.get("startedAt"))
                        for ctx in sc_contexts:
                            if isinstance(ctx, dict) and ctx.get("createdAt"):
                                earliest_candidates.append(ctx.get("createdAt"))
                    if rate_log is not None:
                        rl = client.get_last_rate_limit()
                        if isinstance(rl, dict):
                            rate_log("commits_page", rl)
                    pinfo = commits.get("pageInfo") or {}
                    has_prev = bool(pinfo.get("hasPreviousPage"))
                    before = pinfo.get("startCursor")
                    # Update PR commit backfill flags and earliest timestamp (monotone done flag)
                    try:
                        pr_obj.commits_backfill_cursor = before
                        if not has_prev and not pr_obj.commits_backfill_done:
                            pr_obj.commits_backfill_done = True
                        if earliest_candidates:
                            from dateutil import parser as _dtp

                            ts = [_dtp.isoparse(x) for x in earliest_candidates if x]
                            ts = [timezone.make_aware(t) if timezone.is_naive(t) else t for t in ts]
                            mn = min(ts) if ts else None
                            if mn is not None and (
                                pr_obj.commits_earliest_synced_at is None or mn < pr_obj.commits_earliest_synced_at
                            ):
                                pr_obj.commits_earliest_synced_at = mn
                        pr_obj.save(
                            update_fields=[
                                "commits_backfill_cursor",
                                "commits_backfill_done",
                                "commits_earliest_synced_at",
                            ]
                        )
                    except Exception:
                        pass
                    pages += 1

            # Backfill older commit pages with a fixed budget
            if backfill_commit_pages > 0 and not pr_obj.commits_backfill_done:
                before = pr_obj.commits_backfill_cursor
                pages = 0
                has_prev = True
                while pages < backfill_commit_pages and not pr_obj.commits_backfill_done:
                    cdata = client.get_commits_page(
                        owner=repo.owner,
                        name=repo.name,
                        number=number,
                        last=commitsM,
                        before=before,
                    )
                    cpr = ((cdata.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
                    commits = cpr.get("commits") or {}
                    nodes = commits.get("nodes") or []
                    earliest_candidates: list[str] = []
                    for cnode in nodes:
                        commit = (cnode or {}).get("commit") or {}
                        sha = commit.get("oid") or ""
                        contexts = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
                        cr_contexts = [c for c in contexts if isinstance(c, dict) and c.get("__typename") == "CheckRun"]
                        sc_contexts = [c for c in contexts if isinstance(c, dict) and c.get("__typename") == "StatusContext"]
                        cr_res = sync_check_runs(pr_obj, cr_contexts, sha)
                        sc_res = sync_status_contexts(pr_obj, sc_contexts, sha)
                        result["checkruns_upserted"] += cr_res.created + cr_res.updated
                        result["statusctx_upserted"] += sc_res.created + sc_res.updated
                        for ctx in cr_contexts:
                            if isinstance(ctx, dict):
                                if ctx.get("completedAt"):
                                    earliest_candidates.append(ctx.get("completedAt"))
                                elif ctx.get("startedAt"):
                                    earliest_candidates.append(ctx.get("startedAt"))
                        for ctx in sc_contexts:
                            if isinstance(ctx, dict) and ctx.get("createdAt"):
                                earliest_candidates.append(ctx.get("createdAt"))
                    if rate_log is not None:
                        rl = client.get_last_rate_limit()
                        if isinstance(rl, dict):
                            rate_log("commits_page", rl)
                    pinfo = commits.get("pageInfo") or {}
                    has_prev = bool(pinfo.get("hasPreviousPage"))
                    before = pinfo.get("startCursor")
                    pr_obj.commits_backfill_cursor = before
                    try:
                        if earliest_candidates:
                            from dateutil import parser as _dtp

                            ts = [_dtp.isoparse(x) for x in earliest_candidates if x]
                            ts = [timezone.make_aware(t) if timezone.is_naive(t) else t for t in ts]
                            mn = min(ts) if ts else None
                            if mn is not None and (
                                pr_obj.commits_earliest_synced_at is None or mn < pr_obj.commits_earliest_synced_at
                            ):
                                pr_obj.commits_earliest_synced_at = mn
                    except Exception:
                        pass
                    if not has_prev and not pr_obj.commits_backfill_done:
                        pr_obj.commits_backfill_done = True
                    pr_obj.save(
                        update_fields=[
                            "commits_backfill_cursor",
                            "commits_backfill_done",
                            "commits_earliest_synced_at",
                        ]
                    )
                    pages += 1

        return result
