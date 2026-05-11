"""Helpers for the archive backfill worklist bootstrap (design doc 043).

The bootstrap path enumerates the per-PR directories under ``data/`` in one
of the legacy archive repos via the GitHub ``git/trees`` REST API. We hit
the API exactly twice per archive: once to read the repo's root tree (and
locate the ``data/`` sub-tree's SHA), once to enumerate that sub-tree's
entries. Each entry is one per-PR directory whose ``path`` is the PR
number; the directory's tree SHA is recorded on the ``ArchiveImportItem``
row as ``archive_blob_sha`` so a future re-bootstrap can detect upstream
content changes for a PR cheaply.

The fetcher is parameterised so tests can stub it without touching real
HTTP. Production code passes :func:`default_tree_fetcher`, which uses
``requests`` and an optional bearer token from ``GH_TOKEN`` /
``GITHUB_TOKEN`` for the higher rate limit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterable

import requests


@dataclass(frozen=True)
class ArchivePREntry:
    """One per-PR directory entry as returned by the bootstrap enumeration."""

    pr_number: int
    archive_path: str
    blob_sha: str | None


TreeFetcher = Callable[[str, str, str], dict]
"""Signature: ``fetcher(owner, repo, tree_ref) -> parsed JSON of /git/trees``."""


def default_tree_fetcher(owner: str, repo: str, tree_ref: str) -> dict:
    """Fetch ``/repos/{owner}/{repo}/git/trees/{tree_ref}`` from GitHub.

    ``tree_ref`` can be either a tree SHA or a ref name (e.g. ``master``);
    GitHub's API accepts both for this endpoint. Uses the optional GitHub
    token for higher rate limits but works unauthenticated for the public
    archive repos.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{tree_ref}"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        token = token.split(",", 1)[0].strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def enumerate_archive_pr_entries(
    *,
    owner: str,
    archive: str,
    branch: str = "master",
    fetcher: TreeFetcher | None = None,
) -> list[ArchivePREntry]:
    """Return one entry per ``data/<N>/`` sub-directory in the archive repo.

    Two REST calls: root tree at ``branch`` to locate the ``data/`` entry,
    then the ``data/`` tree itself. The returned list is filtered to entries
    whose path parses as an int (PR number) and whose type is ``tree``;
    everything else is skipped silently.

    Raises ``RuntimeError`` if the archive repo has no ``data/`` directory
    or the API response indicates truncation (the doc's transport plan
    relies on the un-truncated single-page response and we'd rather fail
    loudly than silently enroll a partial worklist).
    """
    fetch = fetcher or default_tree_fetcher

    root = fetch(owner, archive, branch)
    if root.get("truncated"):
        raise RuntimeError(f"git/trees response for {owner}/{archive}@{branch} was truncated")
    data_sha: str | None = None
    for entry in root.get("tree") or []:
        if entry.get("path") == "data" and entry.get("type") == "tree":
            data_sha = entry.get("sha")
            break
    if not data_sha:
        raise RuntimeError(f"No data/ directory in {owner}/{archive}@{branch}")

    data_tree = fetch(owner, archive, data_sha)
    if data_tree.get("truncated"):
        raise RuntimeError(f"git/trees response for {owner}/{archive} data/ was truncated")

    return list(_iter_pr_entries(data_tree.get("tree") or []))


def _iter_pr_entries(entries: Iterable[dict]) -> Iterable[ArchivePREntry]:
    for entry in entries:
        if entry.get("type") != "tree":
            continue
        path = entry.get("path") or ""
        try:
            pr_number = int(path)
        except (TypeError, ValueError):
            continue
        if pr_number <= 0:
            continue
        yield ArchivePREntry(
            pr_number=pr_number,
            archive_path=f"data/{pr_number}/pr_info.json",
            blob_sha=entry.get("sha"),
        )
