"""Temporary diagnostic script for Bug B (UNKNOWN closed_by attribution).
Delete this file before committing.
Run with: heroku run -a <app-name> python tmp_diag_bug_b.py
"""
import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qb_site.settings.production")
import sys
sys.path.insert(0, "/app/qb_site")
django.setup()

from analyzer.models import PRQueueWindow, PRRevision
from analyzer.services.queue_windows import _normalize_label
from syncer.models import PRTimelineEvent, PRTimelineEventType, CommitCheckRun, CommitStatusContext
from django.db.models import Q, F
from collections import Counter

bug_b_qs = (
    PRQueueWindow.objects.filter(closed_by_event_type="UNKNOWN", to_ts__isnull=False)
    .exclude(Q(pull_request__closed_at=F("to_ts")) | Q(pull_request__merged_at=F("to_ts")))
    .select_related("pull_request", "pull_request__repository", "rule_set")
    .order_by("id")[:300]
)

co_occurrence = Counter()
label_relevance = Counter()

for w in bug_b_qs:
    rs = w.rule_set
    required = {_normalize_label(n) for n in (rs.required_label_names or [])}
    forbidden = {_normalize_label(n) for n in (rs.forbidden_label_names or [])}
    label_evs = list(
        PRTimelineEvent.objects.filter(
            pull_request=w.pull_request,
            occurred_at=w.to_ts,
            type__in=[PRTimelineEventType.LABELED, PRTimelineEventType.UNLABELED],
        ).values_list("label_name", flat=True)
    )
    for name in label_evs:
        norm = _normalize_label(name or "")
        if norm in required:
            label_relevance["required"] += 1
        elif norm in forbidden:
            label_relevance["forbidden"] += 1
        else:
            label_relevance["irrelevant"] += 1
    has_irrelevant = any(
        _normalize_label(n or "") not in required and _normalize_label(n or "") not in forbidden
        for n in label_evs
    )
    if not has_irrelevant:
        co_occurrence["no_irrelevant_label"] += 1
        continue
    repo = w.pull_request.repository
    has_rev = PRRevision.objects.filter(pull_request=w.pull_request, from_ts=w.to_ts).exists()
    has_cr = CommitCheckRun.objects.filter(repository=repo, gh_completed_at=w.to_ts).exists()
    has_sc = CommitStatusContext.objects.filter(repository=repo, gh_created_at=w.to_ts).exists()
    key = "+".join(filter(None, [
        "rev" if has_rev else "",
        "check_run" if has_cr else "",
        "status_ctx" if has_sc else "",
    ])) or "nothing_else"
    co_occurrence[key] += 1

print("Label relevance at to_ts:")
for k, v in label_relevance.most_common():
    print(f"  {k}: {v}")
print("\nCo-occurring events alongside irrelevant label (what caused the actual flip):")
for k, v in co_occurrence.most_common():
    print(f"  {k}: {v}")
