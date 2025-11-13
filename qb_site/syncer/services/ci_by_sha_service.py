from __future__ import annotations

from typing import Any, Callable, Optional

from syncer.models import PullRequest
from syncer.services.github_client import GitHubClient
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts


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

    data: dict[str, Any] = {}
    contexts_conn: dict[str, Any] | None = None
    assoc_prs: list[dict[str, Any]] = []
    owner: str | None = None
    name: str | None = None

    # Find a repository where the object(oid) exists
    for o, n in repo_candidates:
        d = _fetch(o, n, after=None)
        repo_node = (d.get("data") or {}).get("repository") or {}
        obj = repo_node.get("object")
        if obj and isinstance(obj, dict) and obj.get("__typename") == "Commit":
            data = d
            owner, name = o, n
            contexts_conn = (obj.get("statusCheckRollup") or {}).get("contexts") or {}
            aprs = (obj.get("associatedPullRequests") or {}).get("nodes") or []
            assoc_prs = [ap for ap in aprs if isinstance(ap, dict)]
            break

    if not contexts_conn:
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
            # Skip ingestion
            return {"checkruns_created": 0, "checkruns_updated": 0, "status_created": 0, "status_updated": 0}

    after = None
    pages = 0
    while pages < max_pages and contexts_conn is not None:
        nodes = contexts_conn.get("nodes") or []
        cr_contexts = [c for c in nodes if isinstance(c, dict) and c.get("__typename") == "CheckRun"]
        sc_contexts = [c for c in nodes if isinstance(c, dict) and c.get("__typename") == "StatusContext"]
        cr_res = sync_check_runs(pr, cr_contexts, sha)
        sc_res = sync_status_contexts(pr, sc_contexts, sha)
        created_cr += cr_res.created
        updated_cr += cr_res.updated
        created_sc += sc_res.created
        updated_sc += sc_res.updated
        if rate_log is not None:
            rl = client.get_last_rate_limit()
            if isinstance(rl, dict):
                rate_log("ci_by_sha_page", rl)
        page_info = contexts_conn.get("pageInfo") or {}
        if page_info.get("hasNextPage") and owner and name:
            after = page_info.get("endCursor")
            d = _fetch(owner, name, after=after)
            repo_node = (d.get("data") or {}).get("repository") or {}
            obj = repo_node.get("object") or {}
            contexts_conn = (obj.get("statusCheckRollup") or {}).get("contexts") or {}
            pages += 1
        else:
            break

    return {
        "checkruns_created": created_cr,
        "checkruns_updated": updated_cr,
        "status_created": created_sc,
        "status_updated": updated_sc,
    }
