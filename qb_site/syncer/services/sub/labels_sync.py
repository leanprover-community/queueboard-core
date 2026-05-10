from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from django.db import transaction

from core.models.repository import Repository
from syncer.models.label_def import LabelDef
from syncer.models.pr_label import PRLabel
from syncer.models.pull_request import PullRequest
from core.utils.db import update_if_changed


@dataclass
class LabelSyncResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0


def sync_label_catalog(repo: Repository, labels: Iterable[Dict[str, Any]]) -> LabelSyncResult:
    """Upsert LabelDef rows from the bundle's label nodes.

    Each item should include: {"name": str, "color": str}

    Returns counts of created/updated rows. Deletions are not tracked here.
    """
    created = 0
    updated = 0
    for node in labels:
        if not isinstance(node, dict):
            continue
        name = (node.get("name") or "").strip()
        if not name:
            continue
        color = (node.get("color") or "").strip().lower()
        # Case-insensitive match within repo
        ld = LabelDef.objects.filter(repository=repo, name__iexact=name).first()
        if ld is None:
            LabelDef.objects.create(repository=repo, name=name, color=color or "000000")
            created += 1
        else:
            new_color = (color or ld.color or "").lower()
            cur_color = (ld.color or "").lower()
            if cur_color != new_color:
                _, fields = update_if_changed(ld, {"color": new_color})
                if fields:
                    updated += 1
    return LabelSyncResult(created=created, updated=updated, deleted=0)


def fetch_repo_label_catalog(
    repo: Repository,
    page_fetcher: Callable[[Optional[str]], Dict[str, Any]],
    *,
    max_pages: int = 50,
) -> List[Dict[str, str]]:
    """Page through GitHub's full label catalog for a repo.

    ``page_fetcher`` accepts an ``after`` cursor and returns a raw GraphQL response.
    Returns the merged list of ``{"name", "color"}`` nodes. Raises if any page is
    missing the expected ``repository.labels`` shape so callers can abort the
    destructive sync rather than truncate the catalog on a partial response.
    """
    after: Optional[str] = None
    out: List[Dict[str, str]] = []
    for _ in range(max_pages):
        data = page_fetcher(after)
        labels = ((data.get("data") or {}).get("repository") or {}).get("labels")
        if not isinstance(labels, dict):
            raise RuntimeError("repo labels page missing repository.labels payload")
        for node in labels.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            name = (node.get("name") or "").strip()
            if not name:
                continue
            color = (node.get("color") or "").strip().lower() or "000000"
            out.append({"name": name, "color": color})
        page_info = labels.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return out
        after = page_info.get("endCursor")
        if not after:
            return out
    raise RuntimeError(f"repo labels pagination exceeded max_pages={max_pages} for repo={repo.owner}/{repo.name}")


def sync_full_label_catalog(repo: Repository, labels: Iterable[Dict[str, Any]]) -> LabelSyncResult:
    """Reconcile LabelDef rows for ``repo`` against an authoritative list.

    Unlike ``sync_label_catalog`` (which is incremental — driven by labels
    attached to a single PR bundle), this expects ``labels`` to be the full
    label catalog as currently defined on GitHub. Rows whose name does not
    appear in the input (case-insensitive) are deleted, which cascades to
    ``PRLabel`` rows attaching them.

    Callers must pass a complete list — partial inputs will silently truncate
    the catalog. Use ``fetch_repo_label_catalog`` to assemble it from paginated
    GraphQL responses; that helper raises on incomplete pages.
    """
    desired: Dict[str, Dict[str, str]] = {}
    for node in labels:
        if not isinstance(node, dict):
            continue
        name = (node.get("name") or "").strip()
        if not name:
            continue
        color = (node.get("color") or "").strip().lower() or "000000"
        # Last write wins on case-insensitive collision (mirrors LabelDef's
        # unique-on-lower-name constraint and matches GitHub's own behavior).
        desired[name.lower()] = {"name": name, "color": color}

    created = 0
    updated = 0
    deleted = 0
    with transaction.atomic():
        existing = list(LabelDef.objects.select_for_update().filter(repository=repo))
        existing_by_lower = {ld.name.lower(): ld for ld in existing}

        for key, node in desired.items():
            ld = existing_by_lower.get(key)
            if ld is None:
                LabelDef.objects.create(repository=repo, name=node["name"], color=node["color"])
                created += 1
                continue
            updates: Dict[str, Any] = {}
            if ld.name != node["name"]:
                updates["name"] = node["name"]
            if (ld.color or "").lower() != node["color"]:
                updates["color"] = node["color"]
            if updates:
                _, fields = update_if_changed(ld, updates)
                if fields:
                    updated += 1

        stale_ids = [ld.id for key, ld in existing_by_lower.items() if key not in desired]
        if stale_ids:
            deleted, _ = LabelDef.objects.filter(id__in=stale_ids).delete()
            # ``delete()`` returns the total number of rows removed including cascaded
            # PRLabel rows; project back to LabelDef count for the caller's summary.
            deleted = len(stale_ids)

    return LabelSyncResult(created=created, updated=updated, deleted=deleted)


def sync_pr_labels(
    pr: PullRequest,
    label_names: Iterable[str],
    *,
    additive_only: bool = False,
) -> LabelSyncResult:
    """Reconcile the current PRLabel attachments for a PR.

    - Resolve LabelDef rows by case-insensitive name within the PR's repository.
    - Compute the set difference vs existing PRLabel rows.
    - Bulk create missing attachments; delete extras.

    When ``additive_only`` is True (archive-mode ingest, design doc 043), the
    detach pass is skipped: labels present on the live row but absent from the
    archive snapshot are NOT removed (the archive is older and would silently
    drop labels added since the snapshot). Additionally, archive label names
    that have no matching ``LabelDef`` for the repo are dropped silently —
    the live syncer is the catalog source of truth, and creating a LabelDef
    from archive data could resurrect a label that GitHub has since deleted.
    """
    # Resolve desired LabelDef ids by case-insensitive name
    names = [n for n in label_names]
    # Build query per name to respect case-insensitive matching
    desired_ids = set()
    for n in names:
        ld = LabelDef.objects.filter(repository=pr.repository, name__iexact=n).first()
        if ld:
            desired_ids.add(ld.id)

    existing = list(PRLabel.objects.filter(pull_request=pr))
    existing_ids = {pl.label_def_id for pl in existing}
    to_add = desired_ids - existing_ids
    to_del: set[int] = set() if additive_only else (existing_ids - desired_ids)
    if to_add:
        PRLabel.objects.bulk_create([PRLabel(pull_request=pr, label_def_id=lid) for lid in to_add], ignore_conflicts=True)
    if to_del:
        PRLabel.objects.filter(pull_request=pr, label_def_id__in=list(to_del)).delete()
    return LabelSyncResult(created=len(to_add), updated=0, deleted=len(to_del))
