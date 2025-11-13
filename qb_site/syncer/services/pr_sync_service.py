from __future__ import annotations

from typing import Any, Dict, Callable, Optional
from datetime import timedelta, timezone as pytimezone

from django.db import transaction
from django.utils import timezone

from core.models.repository import Repository
from syncer.services.github_client import GitHubClient
from syncer.services.sub.pull_request_sync import upsert_pull_request
from syncer.services.sub.labels_sync import sync_label_catalog, sync_pr_labels
from syncer.services.sub.timeline_sync import sync_timeline_events
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts
from syncer.services.sub.core_entities_sync import upsert_repo_metadata
from syncer.models.pull_request import PullRequest


class PRSyncService:
    """Orchestrates ingestion of a single PR.

    Provides two entry points:
      - sync_pull_request_bundle: ingest from an already-fetched GraphQL bundle (easy to test)
      - sync_pull_request: fetch bundle via GitHub client and ingest
    """

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

        # CI snapshots per commit
        checkruns_upserted = 0
        statusctx_upserted = 0
        for cnode in (pr_bundle.get("commits") or {}).get("nodes") or []:
            commit = (cnode or {}).get("commit") or {}
            sha = commit.get("oid") or ""
            contexts = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
            cr_contexts = [c for c in contexts if isinstance(c, dict) and c.get("__typename") == "CheckRun"]
            sc_contexts = [c for c in contexts if isinstance(c, dict) and c.get("__typename") == "StatusContext"]
            cr_res = sync_check_runs(pr_obj, cr_contexts, sha)
            sc_res = sync_status_contexts(pr_obj, sc_contexts, sha)
            checkruns_upserted += cr_res.created + cr_res.updated
            statusctx_upserted += sc_res.created + sc_res.updated

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
    ) -> Dict[str, int]:
        # Determine timeline since cutoff.
        # If an explicit override is provided (e.g., historical backfill), prefer it.
        # Otherwise use last_synced_at - epsilon when available.
        timeline_since_iso: Optional[str] = timeline_since_iso_override
        if timeline_since_iso is None:
            existing = PullRequest.objects.filter(repository=repo, number=number).only("last_synced_at").first()
            if existing and existing.last_synced_at:
                # subtract a small epsilon to avoid boundary-equal misses
                dt = existing.last_synced_at - timedelta(seconds=2)
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
        # Seed timeline backfill state from bundle pageInfo if missing
        tl_conn0 = pr.get("timelineItems") or {}
        page0 = tl_conn0.get("pageInfo") or {}
        if not pr_obj.timeline_backfill_cursor:
            start_cur = page0.get("startCursor")
            if start_cur:
                pr_obj.timeline_backfill_cursor = start_cur
                pr_obj.timeline_backfill_done = not bool(page0.get("hasPreviousPage"))
                pr_obj.save(update_fields=["timeline_backfill_cursor", "timeline_backfill_done"])
        # Log bundle query cost if available
        if rate_log is not None:
            rl = client.get_last_rate_limit()
            if isinstance(rl, dict):
                rate_log("pr_bundle", rl)

        # Optional pagination/backfill (capped)
        if max_timeline_pages > 0 or max_commit_pages > 0 or backfill_timeline_pages > 0:
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
                before = pr_obj.timeline_backfill_cursor
                pages = 0
                while pages < backfill_timeline_pages and before and not pr_obj.timeline_backfill_done:
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
            if max_commit_pages > 0:
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
                    if rate_log is not None:
                        rl = client.get_last_rate_limit()
                        if isinstance(rl, dict):
                            rate_log("commits_page", rl)
                    pinfo = commits.get("pageInfo") or {}
                    has_prev = bool(pinfo.get("hasPreviousPage"))
                    before = pinfo.get("startCursor")
                    pages += 1

        return result
