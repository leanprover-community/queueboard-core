from __future__ import annotations

import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import requests
from dateutil import parser as dtparser
from django.conf import settings
from django.utils import timezone

from core.services.github_operation_tokens import resolve_github_app_operation_token
from syncer.services.rate_budget import choose_token, throttle_request_slot, token_fingerprint


class GitHubClient:
    """Tiny GraphQL client for GitHub v4.

    - Reads token from GH_TOKEN or GITHUB_TOKEN (comma-separated list allowed; picks one based on cached rate budget) if not provided.
    - Provides helpers to load and execute the PR bundle query.

    Network calls are centralized here to make higher-level services easy to test
    by swapping in a fake client.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        endpoint: str = "https://api.github.com/graphql",
        operation: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ):
        self.endpoint = endpoint
        self.operation = operation
        self.owner = owner
        self.repo = repo
        self.token = self._choose_token(token)
        if not self.token:
            raise RuntimeError("GitHub token not found; set GH_TOKEN/GITHUB_TOKEN or pass token explicitly")
        self.token_id = token_fingerprint(self.token)
        self._last_rate_limit: Optional[Dict[str, Any]] = None

    def _choose_token(self, provided: Optional[str]) -> Optional[str]:
        if provided:
            return provided
        if self.operation and self.owner and self.repo:
            operation_token = resolve_github_app_operation_token(
                operation=self.operation,
                owner=self.owner,
                repo=self.repo,
            )
            if operation_token:
                return operation_token
        env_tokens = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not env_tokens:
            return None
        tokens = [t.strip() for t in env_tokens.split(",") if t.strip()]
        if not tokens:
            return None
        chosen = choose_token(tokens)
        return chosen or random.choice(tokens)

    def execute(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a GraphQL query against the GitHub v4 API.

        Notes
        - Writes any returned `rateLimit` snapshot to Redis via `set_rate_snapshot` for
          cross-process token coordination (best-effort).
        """
        throttle_request_slot(
            getattr(settings, "SYNCER_GH_THROTTLE_MS", 0), getattr(settings, "SYNCER_GH_THROTTLE_MAX_WAIT_MS", 5000)
        )
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }
        resp = requests.post(self.endpoint, json={"query": query, "variables": variables}, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data and data["errors"]:
            # Surface GraphQL errors in a readable way for the caller/command.
            msgs = "; ".join(str(e.get("message")) for e in data["errors"])
            raise RuntimeError(f"GraphQL error(s): {msgs}")
        # Capture rateLimit snapshot when present
        rl = (data.get("data") or {}).get("rateLimit")
        if isinstance(rl, dict):
            self._last_rate_limit = rl
            # Persist to Redis for cross-process coordination (best-effort)
            try:  # local import to avoid import-time Redis coupling in tests
                from syncer.services.rate_budget import set_rate_snapshot

                set_rate_snapshot(rl, token_id=self.token_id)
            except Exception:
                pass
        return data

    def _read_file(self, rel_path_from_repo_root: str) -> str:
        # Resolve repo root from this file (…/qb_site/syncer/services/github_client.py)
        here = Path(__file__).resolve()
        repo_root = here.parents[3]  # up to repo root
        qpath = repo_root / rel_path_from_repo_root
        return qpath.read_text(encoding="utf-8")

    def get_pr_bundle(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        timelineK: int = 150,
        commitsM: int = 15,
        inlineCommentsPerReview: Optional[int] = None,
        query_path: str = "qb_site/syncer/queries/pr_bundle.graphql",
        timeline_since_iso: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch the single-PR GraphQL bundle as a dict."""
        query = self._read_file(query_path)
        if inlineCommentsPerReview is None:
            inlineCommentsPerReview = int(getattr(settings, "SYNCER_INLINE_COMMENTS_PER_REVIEW", 20))
        variables = {
            "owner": owner,
            "name": name,
            "number": int(number),
            "timelineK": int(timelineK),
            "commitsM": int(commitsM),
            "inlineCommentsPerReview": int(inlineCommentsPerReview),
            "timelineSince": timeline_since_iso,
        }
        return self.execute(query, variables)

    def get_pr_header(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        query_path: str = "qb_site/syncer/queries/pr_header.graphql",
    ) -> Dict[str, Any]:
        """Fetch a lightweight header for a PR to check updatedAt quickly."""
        query = self._read_file(query_path)
        variables = {"owner": owner, "name": name, "number": int(number)}
        return self.execute(query, variables)

    def get_timeline_page(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        first: int,
        after: Optional[str] = None,
        inlineCommentsPerReview: Optional[int] = None,
        query_path: str = "qb_site/syncer/queries/timeline_page.graphql",
        since_iso: Optional[str] = None,
    ) -> Dict[str, Any]:
        query = self._read_file(query_path)
        if inlineCommentsPerReview is None:
            inlineCommentsPerReview = int(getattr(settings, "SYNCER_INLINE_COMMENTS_PER_REVIEW", 20))
        variables = {
            "owner": owner,
            "name": name,
            "number": int(number),
            "first": int(first),
            "after": after,
            "inlineCommentsPerReview": int(inlineCommentsPerReview),
            "since": since_iso,
        }
        return self.execute(query, variables)

    def get_timeline_page_back(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        last: int,
        before: Optional[str] = None,
        inlineCommentsPerReview: Optional[int] = None,
        query_path: str = "qb_site/syncer/queries/timeline_page_back.graphql",
    ) -> Dict[str, Any]:
        """Fetch an older page of timeline items using last/before for backfill."""
        query = self._read_file(query_path)
        if inlineCommentsPerReview is None:
            inlineCommentsPerReview = int(getattr(settings, "SYNCER_INLINE_COMMENTS_PER_REVIEW", 20))
        variables = {
            "owner": owner,
            "name": name,
            "number": int(number),
            "last": int(last),
            "before": before,
            "inlineCommentsPerReview": int(inlineCommentsPerReview),
        }
        return self.execute(query, variables)

    NODES_IDS_MAX = 100  # GitHub's hard cap on `nodes(ids: [...])`

    def get_timeline_actors_by_node_ids(
        self,
        *,
        ids: Sequence[str],
        query_path: str = "qb_site/syncer/queries/actor_types_by_node_ids.graphql",
    ) -> Dict[str, Any]:
        """Re-resolve the acting account for stored timeline item node ids.

        Callers read ``data.nodes[]``, each carrying ``__typename``, ``id`` and
        either ``actor`` or ``author`` (``null`` for ids that no longer
        resolve). Used by the actor-type backfill (design doc 051).
        """
        query = self._read_file(query_path)
        node_ids = list(ids)
        if len(node_ids) > self.NODES_IDS_MAX:
            raise ValueError(f"nodes(ids:) accepts at most {self.NODES_IDS_MAX} ids, got {len(node_ids)}")
        return self.execute(query, {"ids": node_ids})

    def get_last_rate_limit(self) -> Optional[Dict[str, Any]]:
        """Return the last seen rateLimit snapshot (if any)."""
        return self._last_rate_limit

    def get_rate_limit(self) -> Dict[str, Any]:
        """Fetch a bare rateLimit snapshot."""
        query = "query { rateLimit { cost remaining resetAt used } }"
        return self.execute(query, {})

    def get_commits_page(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        last: int,
        before: Optional[str] = None,
        query_path: str = "qb_site/syncer/queries/commits_page.graphql",
    ) -> Dict[str, Any]:
        query = self._read_file(query_path)
        variables = {
            "owner": owner,
            "name": name,
            "number": int(number),
            "last": int(last),
            "before": before,
        }
        return self.execute(query, variables)

    def get_ci_by_commit(
        self,
        *,
        owner: str,
        name: str,
        sha: str,
        first: int = 100,
        after: Optional[str] = None,
        query_path: str = "qb_site/syncer/queries/ci_by_commit.graphql",
    ) -> Dict[str, Any]:
        """Fetch CI contexts (CheckRuns and StatusContexts) for a commit SHA.

        The caller should inspect data.repository.object; when null or not a Commit,
        there is no data for that (owner,name,sha) tuple.
        """
        query = self._read_file(query_path)
        variables = {
            "owner": owner,
            "name": name,
            "sha": sha,
            "first": int(max(1, min(first, 100))),
            "after": after,
        }
        return self.execute(query, variables)

    def get_commit_history_from_sha(
        self,
        *,
        owner: str,
        name: str,
        sha: str,
        first: int = 50,
        after: Optional[str] = None,
        since: Optional[str] = None,
        query_path: str = "qb_site/syncer/queries/commit_history_from_sha.graphql",
    ) -> Dict[str, Any]:
        """Fetch git commit history starting from `sha` (walk back)."""
        query = self._read_file(query_path)
        variables = {
            "owner": owner,
            "name": name,
            "sha": sha,
            "first": int(max(1, min(first, 100))),
            "after": after,
            "since": since,
        }
        return self.execute(query, variables)

    def get_changed_pr_numbers(
        self,
        *,
        owner: str,
        name: str,
        since_iso: str,
        states: Optional[Sequence[str]] = ("OPEN",),
        limit: int = 50,
        per_page: int = 100,
        max_pages: Optional[int] = None,
        after: Optional[str] = None,
    ) -> list[int]:
        """Enumerate PR numbers updated on or after ``since_iso``.

        - Uses repository.pullRequests ordered by UPDATED_AT DESC and pages until:
          - updatedAt < since cutoff, or
          - we collected ``limit`` PR numbers, or
          - no next page.
        - ``states`` defaults to ["OPEN"]. Pass e.g. ("OPEN","MERGED","CLOSED") to broaden.
        - Returns numbers in descending updatedAt order.
        """
        result = self.discover_changed_pr_numbers(
            owner=owner,
            name=name,
            since_iso=since_iso,
            states=states,
            limit=limit,
            per_page=per_page,
            max_pages=max_pages,
            after=after,
        )
        return result.numbers

    @dataclass
    class ChangedPRDiscoveryResult:
        numbers: list[int]
        next_cursor: Optional[str]
        reached_cutoff: bool
        hit_limit: bool

    def discover_changed_pr_numbers(
        self,
        *,
        owner: str,
        name: str,
        since_iso: str,
        states: Optional[Sequence[str]] = ("OPEN",),
        limit: int = 50,
        per_page: int = 100,
        max_pages: Optional[int] = None,
        after: Optional[str] = None,
    ) -> ChangedPRDiscoveryResult:
        """Enumerate changed PR numbers with continuation metadata.

        Returns:
        - ``numbers``: discovered PR numbers in UPDATED_AT DESC order.
        - ``next_cursor``: cursor to continue from when the scan stopped early.
        - ``reached_cutoff``: whether ``updatedAt < since_iso`` was encountered.
        - ``hit_limit``: whether local page/count caps stopped the scan before completion.
        """
        cutoff = dtparser.isoparse(since_iso)
        if timezone.is_naive(cutoff):
            cutoff = timezone.make_aware(cutoff)

        per_page = max(1, min(int(per_page), 100))
        remaining = max(1, int(limit))
        page_count = 0
        out: list[int] = []
        next_cursor: Optional[str] = None
        reached_cutoff = False
        hit_limit = False

        query = (
            "query PRList($owner: String!, $name: String!, $first: Int!, $after: String, $states: [PullRequestState!]) {\n"
            "  rateLimit { cost remaining resetAt used }\n"
            "  repository(owner: $owner, name: $name) {\n"
            "    pullRequests(states: $states, orderBy: {field: UPDATED_AT, direction: DESC}, first: $first, after: $after) {\n"
            "      pageInfo { hasNextPage endCursor }\n"
            "      nodes { number updatedAt state }\n"
            "    }\n"
            "  }\n"
            "}"
        )

        while remaining > 0:
            if max_pages is not None and page_count >= max_pages:
                hit_limit = True
                break
            request_first = min(per_page, remaining)
            variables: Dict[str, Any] = {
                "owner": owner,
                "name": name,
                "first": request_first,
                "after": after,
                "states": list(states) if states else None,
            }
            data = self.execute(query, variables)
            repo = (data.get("data") or {}).get("repository")
            if not repo:
                break
            conn = repo.get("pullRequests") or {}
            nodes = conn.get("nodes") or []
            stop = False
            for n in nodes:
                try:
                    updated = dtparser.isoparse(n.get("updatedAt"))
                    if timezone.is_naive(updated):
                        updated = timezone.make_aware(updated)
                except Exception:
                    # If parsing fails, keep item conservatively
                    updated = cutoff
                if updated < cutoff:
                    stop = True
                    reached_cutoff = True
                    break
                out.append(int(n.get("number")))
                remaining -= 1
                if remaining <= 0:
                    hit_limit = True
                    break

            page_count += 1
            page = conn.get("pageInfo") or {}
            page_has_next = bool(page.get("hasNextPage"))
            page_end_cursor = page.get("endCursor")

            if stop:
                break
            if remaining <= 0:
                next_cursor = page_end_cursor if page_has_next else None
                break
            if not page_has_next:
                break
            after = page_end_cursor
            if not after:
                break
            next_cursor = after

        if reached_cutoff:
            next_cursor = None
            hit_limit = False

        return self.ChangedPRDiscoveryResult(
            numbers=out,
            next_cursor=next_cursor,
            reached_cutoff=reached_cutoff,
            hit_limit=hit_limit,
        )

    def get_repo_labels_page(
        self,
        *,
        owner: str,
        name: str,
        first: int = 100,
        after: Optional[str] = None,
        query_path: str = "qb_site/syncer/queries/repo_labels.graphql",
    ) -> Dict[str, Any]:
        """Fetch one page of the repository label catalog.

        Returns the raw GraphQL response. Callers can read:
          data.repository.labels.nodes[]   -> { name, color }
          data.repository.labels.pageInfo  -> { hasNextPage, endCursor }
        """
        query = self._read_file(query_path)
        variables: Dict[str, Any] = {
            "owner": owner,
            "name": name,
            "first": int(max(1, min(first, 100))),
            "after": after,
        }
        return self.execute(query, variables)

    def get_prs_created_page(
        self,
        *,
        owner: str,
        name: str,
        first: int,
        after: Optional[str] = None,
        states: Optional[Sequence[str]] = None,
        query_path: str = "qb_site/syncer/queries/prs_created_page.graphql",
    ) -> Dict[str, Any]:
        """Fetch a page of PRs ordered by CREATED_AT ASC with minimal fields.

        Returns the raw GraphQL response. Callers can read:
          data.repository.pullRequests.nodes[] with fields
            { number, createdAt, closedAt, mergedAt, isDraft, state }
          and pageInfo { hasNextPage, endCursor }.
        """
        query = self._read_file(query_path)
        variables: Dict[str, Any] = {
            "owner": owner,
            "name": name,
            "first": int(max(1, min(first, 100))),
            "after": after,
            "states": list(states) if states else None,
        }
        return self.execute(query, variables)
