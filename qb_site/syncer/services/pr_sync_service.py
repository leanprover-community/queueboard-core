from __future__ import annotations

from typing import Any, Dict, Callable, Optional

from django.db import transaction

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
    ) -> Dict[str, int]:
        # Fetch bundle and delegate to bundle-based ingestion
        data = client.get_pr_bundle(owner=repo.owner, name=repo.name, number=number, timelineK=timelineK, commitsM=commitsM)
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
        # Log bundle query cost if available
        if rate_log is not None:
            rl = client.get_last_rate_limit()
            if isinstance(rl, dict):
                rate_log("pr_bundle", rl)

        # Optional pagination (capped, no cutoff yet)
        if max_timeline_pages > 0 or max_commit_pages > 0:
            pr_obj = PullRequest.objects.get(repository=repo, number=number)

            # Timeline paging
            if max_timeline_pages > 0:
                tl_conn = (pr.get("timelineItems") or {})
                page = tl_conn.get("pageInfo") or {}
                after = page.get("endCursor")
                has_next = bool(page.get("hasNextPage"))
                pages = 0
                while has_next and pages < max_timeline_pages:
                    tdata = client.get_timeline_page(owner=repo.owner, name=repo.name, number=number, first=timelineK, after=after)
                    tpr = ((tdata.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
                    titems = (tpr.get("timelineItems") or {})
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

            # Commits paging (older via before)
            if max_commit_pages > 0:
                c_conn = (pr.get("commits") or {})
                c_page = c_conn.get("pageInfo") or {}
                before = c_page.get("startCursor")
                has_prev = bool(c_page.get("hasPreviousPage"))
                pages = 0
                while has_prev and pages < max_commit_pages:
                    cdata = client.get_commits_page(owner=repo.owner, name=repo.name, number=number, last=commitsM, before=before)
                    cpr = ((cdata.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
                    commits = (cpr.get("commits") or {})
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
