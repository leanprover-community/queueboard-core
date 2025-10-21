from __future__ import annotations

from typing import Any, Dict

from django.db import transaction

from core.models.repository import Repository
from syncer.services.github_client import GitHubClient
from syncer.services.sub.pull_request_sync import upsert_pull_request
from syncer.services.sub.labels_sync import sync_label_catalog, sync_pr_labels
from syncer.services.sub.timeline_sync import sync_timeline_events
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts
from syncer.services.sub.core_entities_sync import upsert_repo_metadata


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
        dry_run: bool = False,
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
        return self.sync_pull_request_bundle(repo, pr, dry_run=dry_run)
