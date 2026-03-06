from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from syncer.models import PullRequest
from syncer.services.ci_by_sha_service import sync_ci_for_sha
from syncer.services.github_client import GitHubClient


def run_ci_sync_for_pr_shas(
    *,
    pr: PullRequest,
    shas: Sequence[str],
    client: GitHubClient,
    max_pages_per_sha: int,
    dry_run: bool,
    require_pr_association: bool,
    budget_threshold: int,
    per_sha_cap: int = 50,
    rate_log: Optional[Callable[[str, dict[str, Any]], None]] = None,
    on_sha_result: Optional[Callable[[str, str], None]] = None,
) -> dict[str, Any]:
    """Run CI-by-SHA sync for one PR and return normalized execution details.

    Return shape intentionally supports task-level orchestration for both PR-scoped
    and SHA-first task entrypoints.
    """

    todo: list[str] = [sha for sha in shas if sha]
    done: list[str] = []
    total_counts = {"checkruns_created": 0, "checkruns_updated": 0, "status_created": 0, "status_updated": 0}
    per_sha_results: list[dict[str, Any]] = []
    results_by_result: dict[str, int] = {}

    rl0 = client.get_rate_limit() or {}
    remaining0 = rl0.get("remaining") if isinstance(rl0, dict) else None
    if isinstance(remaining0, int) and remaining0 <= budget_threshold:
        return {
            "status": "deferred",
            "done": done,
            "remaining_shas": todo,
            "counts": total_counts,
            "results_by_result": results_by_result,
            "per_sha_results": per_sha_results,
            "per_sha_results_truncated": False,
            "reset_at": rl0.get("resetAt") if isinstance(rl0, dict) else None,
        }

    for idx, sha in enumerate(todo):
        rl_now = client.get_last_rate_limit() or {}
        remaining_now = rl_now.get("remaining") if isinstance(rl_now, dict) else None
        reset_at = rl_now.get("resetAt") if isinstance(rl_now, dict) else None
        if isinstance(remaining_now, int) and remaining_now <= budget_threshold:
            return {
                "status": "deferred",
                "done": done,
                "remaining_shas": todo[idx:],
                "counts": total_counts,
                "results_by_result": results_by_result,
                "per_sha_results": per_sha_results,
                "per_sha_results_truncated": len(todo) > per_sha_cap,
                "reset_at": reset_at,
            }

        if dry_run:
            if len(per_sha_results) < per_sha_cap:
                per_sha_results.append({"sha": sha, "result": "dry_run"})
            results_by_result["dry_run"] = results_by_result.get("dry_run", 0) + 1
            done.append(sha)
            continue

        res = sync_ci_for_sha(
            pr,
            sha,
            client=client,
            max_pages=max_pages_per_sha,
            rate_log=rate_log,
            require_pr_association=require_pr_association,
        )
        result = str(res.get("result") or "ok")
        if on_sha_result is not None:
            on_sha_result(sha, result)
        if len(per_sha_results) < per_sha_cap:
            per_sha_results.append(
                {
                    "sha": sha,
                    "result": result,
                    "found_commit": bool(res.get("found_commit")),
                    "found_contexts": bool(res.get("found_contexts")),
                    "counts": {k: int(res.get(k, 0)) for k in total_counts.keys()},
                }
            )
        results_by_result[result] = results_by_result.get(result, 0) + 1
        for k in total_counts.keys():
            total_counts[k] += int(res.get(k, 0))
        done.append(sha)

    return {
        "status": "ok",
        "done": done,
        "remaining_shas": [],
        "counts": total_counts,
        "results_by_result": results_by_result,
        "per_sha_results": per_sha_results,
        "per_sha_results_truncated": len(todo) > per_sha_cap,
        "reset_at": None,
    }
