from __future__ import annotations

from typing import Any, Callable, Optional

import logging

from syncer.models import PullRequest
from syncer.services.github_client import GitHubClient
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts

log = logging.getLogger(__name__)


def sync_ci_for_sha(
    pr: PullRequest,
    sha: str,
    *,
    client: GitHubClient,
    max_pages: int = 1,
    rate_log: Optional[Callable[[str, dict], None]] = None,
    require_pr_association: bool = False,
) -> dict[str, int]:
    """Fetch CI contexts for a commit SHA and upsert them for the given PR.

    Strategy
    - Prefer the PR's head repository (head_repo_owner_login/head_repo_name).
    - Fallback to the base repository (repository.owner/name) when the object is missing.
    - Paginate contexts(first:100, after) up to max_pages and upsert CheckRun/StatusContext rows.

    Returns a dict with created/updated counts.
    """
    created_cr = 0
    updated_cr = 0
    created_sc = 0
    updated_sc = 0

    def _fetch(owner: str, name: str, after: Optional[str] = None) -> dict[str, Any]:
        return client.get_ci_by_commit(owner=owner, name=name, sha=sha, first=100, after=after)

    # Try head repo first, then base repo
    repo_candidates: list[tuple[str, str]] = []
    if pr.head_repo_owner_login and pr.head_repo_name:
        repo_candidates.append((pr.head_repo_owner_login, pr.head_repo_name))
    repo_candidates.append((pr.repository.owner, pr.repository.name))

    # Collect contexts from any candidate repo that has the commit.
    assoc_prs: list[dict[str, Any]] = []
    context_pages: list[list[dict[str, Any]]] = []
    found_repos: list[tuple[str, str]] = []
    for o, n in repo_candidates:
        d = _fetch(o, n, after=None)
        repo_node = (d.get("data") or {}).get("repository") or {}
        obj = repo_node.get("object")
        if obj and isinstance(obj, dict) and obj.get("__typename") == "Commit":
            found_repos.append((o, n))
            contexts_conn = (obj.get("statusCheckRollup") or {}).get("contexts") or {}
            aprs = (obj.get("associatedPullRequests") or {}).get("nodes") or []
            assoc_prs.extend([ap for ap in aprs if isinstance(ap, dict)])

            after = None
            pages = 0
            while pages < max_pages and contexts_conn is not None:
                nodes = contexts_conn.get("nodes") or []
                if nodes:
                    context_pages.append(nodes)
                if rate_log is not None:
                    rl = client.get_last_rate_limit()
                    if isinstance(rl, dict):
                        rate_log("ci_by_sha_page", rl)
                page_info = contexts_conn.get("pageInfo") or {}
                if page_info.get("hasNextPage"):
                    after = page_info.get("endCursor")
                    d = _fetch(o, n, after=after)
                    repo_node = (d.get("data") or {}).get("repository") or {}
                    obj = repo_node.get("object") or {}
                    contexts_conn = (obj.get("statusCheckRollup") or {}).get("contexts") or {}
                    pages += 1
                else:
                    break
        else:
            log.debug("CI by SHA: commit %s not found in %s/%s", sha, o, n)

    if not context_pages:
        log.debug(
            "CI by SHA: no contexts found for %s sha=%s (candidates=%s)",
            pr,
            sha,
            [(o, n) for o, n in repo_candidates],
        )
        return {"checkruns_created": 0, "checkruns_updated": 0, "status_created": 0, "status_updated": 0}

    # Optional association guard to avoid writing CI unrelated to this PR
    if require_pr_association:
        pr_owner = pr.repository.owner
        pr_name = pr.repository.name
        pr_number = int(pr.number)
        is_associated = False
        for ap in assoc_prs:
            try:
                repo = ap.get("repository") or {}
                owner_login = (repo.get("owner") or {}).get("login")
                name_login = repo.get("name")
                num = int(ap.get("number")) if ap.get("number") is not None else None
            except Exception:
                owner_login = name_login = None
                num = None
            if owner_login == pr_owner and name_login == pr_name and num == pr_number:
                is_associated = True
                break
        if not is_associated:
            log.debug(
                "CI by SHA: skipping sha=%s for %s due to missing associated PR in GitHub payload",
                sha,
                pr,
            )
            # Skip ingestion
            return {"checkruns_created": 0, "checkruns_updated": 0, "status_created": 0, "status_updated": 0}

    # Ingest contexts, deduping by github node id across repos/pages.
    seen_cr_ids: set[str] = set()
    seen_sc_ids: set[str] = set()
    for page_idx, nodes in enumerate(context_pages):
        cr_contexts: list[dict[str, Any]] = []
        sc_contexts: list[dict[str, Any]] = []
        for c in nodes:
            if not isinstance(c, dict):
                continue
            if c.get("__typename") == "CheckRun":
                cid = c.get("id")
                if not cid or cid in seen_cr_ids:
                    continue
                seen_cr_ids.add(cid)
                cr_contexts.append(c)
            elif c.get("__typename") == "StatusContext":
                sid = c.get("id")
                if not sid or sid in seen_sc_ids:
                    continue
                seen_sc_ids.add(sid)
                sc_contexts.append(c)
        log.debug(
            "CI by SHA: processing sha=%s pr=%s page=%s (checkruns=%s, status=%s)",
            sha,
            pr,
            page_idx,
            len(cr_contexts),
            len(sc_contexts),
        )
        cr_res = sync_check_runs(pr, cr_contexts, sha)
        sc_res = sync_status_contexts(pr, sc_contexts, sha)
        if cr_res.created == 0 and cr_res.updated == 0 and cr_contexts:
            log.debug(
                "CI by SHA: no CheckRun upserts for sha=%s pr=%s (ids=%s)",
                sha,
                pr,
                [c.get("id") for c in cr_contexts],
            )
        if sc_res.created == 0 and sc_res.updated == 0 and sc_contexts:
            log.debug(
                "CI by SHA: no StatusContext upserts for sha=%s pr=%s (ids=%s)",
                sha,
                pr,
                [c.get("id") for c in sc_contexts],
            )
        created_cr += cr_res.created
        updated_cr += cr_res.updated
        created_sc += sc_res.created
        updated_sc += sc_res.updated

    log.debug(
        "CI by SHA: completed sha=%s pr=%s (cr_created=%s cr_updated=%s sc_created=%s sc_updated=%s)",
        sha,
        pr,
        created_cr,
        updated_cr,
        created_sc,
        updated_sc,
    )
    return {
        "checkruns_created": created_cr,
        "checkruns_updated": updated_cr,
        "status_created": created_sc,
        "status_updated": updated_sc,
    }
