from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable

from core.models.repository import Repository
from syncer.models.label_def import LabelDef
from syncer.models.pr_label import PRLabel
from syncer.models.pull_request import PullRequest


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
            new_color = color or ld.color
            if (ld.color or "").lower() != (new_color or "").lower():
                ld.color = new_color
                ld.save(update_fields=["color", "updated_at"])
                updated += 1
    return LabelSyncResult(created=created, updated=updated, deleted=0)


def sync_pr_labels(pr: PullRequest, label_names: Iterable[str]) -> LabelSyncResult:
    """Reconcile the current PRLabel attachments for a PR.

    - Resolve LabelDef rows by case-insensitive name within the PR's repository.
    - Compute the set difference vs existing PRLabel rows.
    - Bulk create missing attachments; delete extras.
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
    to_del = existing_ids - desired_ids
    if to_add:
        PRLabel.objects.bulk_create([PRLabel(pull_request=pr, label_def_id=lid) for lid in to_add], ignore_conflicts=True)
    if to_del:
        PRLabel.objects.filter(pull_request=pr, label_def_id__in=list(to_del)).delete()
    return LabelSyncResult(created=len(to_add), updated=0, deleted=len(to_del))
