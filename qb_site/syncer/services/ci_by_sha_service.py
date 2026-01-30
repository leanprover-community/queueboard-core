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
) -> dict[str, Any]:
    """Fetch CI contexts for a commit SHA and upsert them for the given PR.

    Strategy
    - Prefer the PR's head repository (head_repo_owner_login/head_repo_name).
    - Fallback to the base repository (repository.owner/name) when the object is missing.
    - Paginate contexts(first:100, after) up to max_pages and upsert CheckRun/StatusContext rows.

    Returns a dict with created/updated counts plus classification metadata.
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
    repos_checked: list[dict[str, Any]] = []
    found_repos: list[tuple[str, str]] = []
    repo_used: Optional[tuple[str, str]] = None
    for o, n in repo_candidates:
        d = _fetch(o, n, after=None)
        repo_node = (d.get("data") or {}).get("repository") or {}
        obj = repo_node.get("object")
        found_commit = bool(obj and isinstance(obj, dict) and obj.get("__typename") == "Commit")
        repos_checked.append({"owner": o, "name": n, "found_commit": found_commit})
        if found_commit:
            found_repos.append((o, n))
            contexts_conn = (obj.get("statusCheckRollup") or {}).get("contexts") or {}
            aprs = (obj.get("associatedPullRequests") or {}).get("nodes") or []
            assoc_prs.extend([ap for ap in aprs if isinstance(ap, dict)])

            after = None
            pages = 0
            while pages < max_pages and contexts_conn is not None:
                nodes = contexts_conn.get("nodes") or []
                if nodes:
                    if repo_used is None:
                        repo_used = (o, n)
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

    found_commit_any = bool(found_repos)
    found_contexts = bool(context_pages)

    # Optional association guard to avoid writing CI unrelated to this PR
    association_required = bool(require_pr_association)
    association_matched = False
    if association_required:
        pr_owner = pr.repository.owner
        pr_name = pr.repository.name
        pr_number = int(pr.number)
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
                association_matched = True
                break
        if not association_matched:
            log.debug(
                "CI by SHA: skipping sha=%s for %s due to missing associated PR in GitHub payload",
                sha,
                pr,
            )

    if not found_commit_any:
        log.debug(
            "CI by SHA: commit not found for %s sha=%s (candidates=%s)",
            pr,
            sha,
            [(o, n) for o, n in repo_candidates],
        )
        return {
            "checkruns_created": 0,
            "checkruns_updated": 0,
            "status_created": 0,
            "status_updated": 0,
            "result": "not_found",
            "found_commit": False,
            "found_contexts": False,
            "repos_checked": repos_checked,
            "repo_used": None,
            "pages_fetched": 0,
            "assoc_prs_count": len(assoc_prs),
            "association_required": association_required,
            "association_matched": association_matched,
            "sha": sha,
            "pr": f"{pr.repository.owner}/{pr.repository.name}#{pr.number}",
        }

    if association_required and not association_matched:
        return {
            "checkruns_created": 0,
            "checkruns_updated": 0,
            "status_created": 0,
            "status_updated": 0,
            "result": "skipped_association",
            "found_commit": found_commit_any,
            "found_contexts": found_contexts,
            "repos_checked": repos_checked,
            "repo_used": {"owner": repo_used[0], "name": repo_used[1]} if repo_used else None,
            "pages_fetched": len(context_pages),
            "assoc_prs_count": len(assoc_prs),
            "association_required": association_required,
            "association_matched": association_matched,
            "sha": sha,
            "pr": f"{pr.repository.owner}/{pr.repository.name}#{pr.number}",
        }

    if not found_contexts:
        log.debug(
            "CI by SHA: no contexts found for %s sha=%s (candidates=%s)",
            pr,
            sha,
            [(o, n) for o, n in repo_candidates],
        )
        return {
            "checkruns_created": 0,
            "checkruns_updated": 0,
            "status_created": 0,
            "status_updated": 0,
            "result": "empty",
            "found_commit": True,
            "found_contexts": False,
            "repos_checked": repos_checked,
            "repo_used": None,
            "pages_fetched": 0,
            "assoc_prs_count": len(assoc_prs),
            "association_required": association_required,
            "association_matched": association_matched,
            "sha": sha,
            "pr": f"{pr.repository.owner}/{pr.repository.name}#{pr.number}",
        }

    # Ingest contexts, deduping by github node id across repos/pages.
    seen_cr_ids: set[str] = set()
    seen_sc_ids: set[str] = set()
    raw_contexts_total = 0
    saved_contexts_total = 0
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
        raw_contexts_total += len(cr_contexts) + len(sc_contexts)
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
        saved_contexts_total += cr_res.created + cr_res.updated + sc_res.created + sc_res.updated
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
    result = "ok"
    if raw_contexts_total > 0 and saved_contexts_total == 0:
        result = "filtered"
    return {
        "checkruns_created": created_cr,
        "checkruns_updated": updated_cr,
        "status_created": created_sc,
        "status_updated": updated_sc,
        "result": result,
        "found_commit": found_commit_any,
        "found_contexts": found_contexts,
        "repos_checked": repos_checked,
        "repo_used": {"owner": repo_used[0], "name": repo_used[1]} if repo_used else None,
        "pages_fetched": len(context_pages),
        "raw_contexts_total": raw_contexts_total,
        "saved_contexts_total": saved_contexts_total,
        "assoc_prs_count": len(assoc_prs),
        "association_required": association_required,
        "association_matched": association_matched,
        "sha": sha,
        "pr": f"{pr.repository.owner}/{pr.repository.name}#{pr.number}",
    }
