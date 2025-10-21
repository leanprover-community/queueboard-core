from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Repository
from syncer.services.sub.pull_request_sync import upsert_pull_request
from syncer.services.sub.labels_sync import sync_label_catalog, sync_pr_labels
from syncer.services.sub.timeline_sync import sync_timeline_events
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts
from syncer.services.sub.core_entities_sync import upsert_repo_metadata


class Command(BaseCommand):
    help = "Ingest a single PR bundle JSON (from GraphQL) into syncer tables"

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument("--file", required=True, help="Path to PR bundle JSON output")
        parser.add_argument(
            "--create-missing-repo-default-branch",
            default="master",
            help="Default branch to use when creating a missing repository row",
        )
        parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")

    def handle(self, *args, **opts):  # type: ignore[override]
        owner_name: str = opts["repo"]
        file_path = Path(opts["file"]).resolve()
        dry_run: bool = bool(opts["dry_run"])  # control transactional rollback
        default_branch: str = opts["create_missing_repo_default_branch"]

        if "/" not in owner_name:
            raise CommandError("--repo must be in the form owner/name")
        owner, name = owner_name.split("/", 1)

        if not file_path.exists():
            raise CommandError(f"File not found: {file_path}")
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise CommandError(f"Failed to parse JSON: {e}")

        repo_node = (data.get("data") or {}).get("repository", {})
        pr = repo_node.get("pullRequest")
        if not pr:
            raise CommandError("Input JSON missing data.repository.pullRequest")

        repo = Repository.objects.filter(owner=owner, name=name).first()
        if repo is None:
            repo = Repository(owner=owner, name=name, default_branch=default_branch, is_active=True)
            repo.save()
        # If GraphQL repository id is present, persist it to core.Repository
        # Update repo node id and default branch metadata if present
        default_branch = None
        dbr = repo_node.get("defaultBranchRef") or {}
        if isinstance(dbr, dict):
            default_branch = dbr.get("name")
        upsert_repo_metadata(
            repo,
            repo_gid=repo_node.get("id"),
            owner_login=(repo_node.get("owner") or {}).get("login"),
            name=repo_node.get("name"),
            default_branch=default_branch,
            allow_rename=False,
        )

        changed_counts = self._ingest_pr_bundle(repo, pr, dry_run=dry_run)
        self.stdout.write(
            self.style.SUCCESS(
                f"Ingested PR bundle: repo={repo} PR #{pr.get('number')}; "
                f"labels +{changed_counts['labels_created']}/~{changed_counts['labels_updated']} "
                f"attachments +{changed_counts['prlabels_created']}/-{changed_counts['prlabels_deleted']}; "
                f"events +{changed_counts['events_created']}; "
                f"checkruns +{changed_counts['checkruns_upserted']}; "
                f"statusctx +{changed_counts['statusctx_upserted']}"
            )
        )

    @transaction.atomic
    def _ingest_pr_bundle(self, repo: Repository, pr: Dict[str, Any], *, dry_run: bool = False) -> Dict[str, int]:
        # Upsert core PR via service
        upsert_res = upsert_pull_request(pr, repo)
        pr_obj = upsert_res.pr

        # Labels via services
        bundle_labels = [n for n in ((pr.get("labels") or {}).get("nodes") or []) if isinstance(n, dict)]
        lab_res = sync_label_catalog(repo, bundle_labels)
        label_names = [str(n.get("name")) for n in bundle_labels if isinstance(n, dict) and n.get("name")]
        attach_res = sync_pr_labels(pr_obj, label_names)

        # Timeline events via service
        tl_nodes = (pr.get("timelineItems") or {}).get("nodes") or []
        tl_res = sync_timeline_events(pr_obj, tl_nodes)

        # CI snapshots per commit via services
        checkruns_upserted = 0
        statusctx_upserted = 0
        for cnode in (pr.get("commits") or {}).get("nodes") or []:
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
            # Roll back all DB writes made inside this atomic block.
            transaction.set_rollback(True)
        return result
