from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Dict, Iterable, List, Sequence

from django.db.models import QuerySet

from analyzer.models import PRDependency, QueueRuleSet, QueueSnapshot
from core.models import Repository
from syncer.models import PRLabel, PullRequest
from syncer.models.pull_request import PullRequestState
from syncer.models.check_run import CheckRun, CheckRunConclusion, CheckRunStatus
from syncer.models.status_context import StatusContext, StatusContextState


DataStatus = str  # "valid" | "incomplete" | "missing"


def _isoformat(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _data_status(is_incomplete: bool, synced_at: datetime | None) -> DataStatus:
    if synced_at is None:
        return "missing"
    if is_incomplete:
        return "incomplete"
    return "valid"


def _ci_status_for_pr(pr_id: int, check_runs: Sequence[dict], status_contexts: Sequence[dict]) -> str:
    """Coarse CI rollup: running > fail > pass > missing."""
    has_any = False
    running = False
    failed = False

    for cr in check_runs:
        has_any = True
        status = cr["status"]
        conclusion = cr["conclusion"]
        if status != CheckRunStatus.COMPLETED:
            running = True
            continue
        if conclusion not in (
            CheckRunConclusion.SUCCESS,
            CheckRunConclusion.NEUTRAL,
            CheckRunConclusion.SKIPPED,
        ):
            failed = True

    for sc in status_contexts:
        has_any = True
        state = sc["state"]
        if state == StatusContextState.PENDING:
            running = True
        elif state in (StatusContextState.FAILURE, StatusContextState.ERROR):
            failed = True

    if running:
        return "running"
    if failed:
        return "fail"
    if has_any:
        return "pass"
    return "missing"


def _forbidden_queue_labels(default_branch: str) -> set[str]:
    base = {
        "blocked-by-other-pr",
        "blocked-by-core-pr",
        "blocked-by-batt-pr",
        "blocked-by-qq-pr",
        "awaiting-ci",
        "awaiting-author",
        "awaiting-zulip",
        "please-adopt",
        "help-wanted",
        "wip",
        "delegated",
        "auto-merge-after-ci",
        "ready-to-merge",
    }
    # Align with legacy queue filter: queue only considers default branch PRs.
    _ = default_branch  # placeholder for future branch-specific rules
    return base


def _label_url(repo: Repository, name: str) -> str:
    return f"https://github.com/{repo.owner}/{repo.name}/labels/{name}"


def _classify_pr_status(*, label_names: set[str], ci_status: str, is_draft: bool) -> str:
    """Approximate legacy PRStatus classification."""
    if is_draft or ci_status in {"fail", "fail-inessential", "running", "missing"}:
        return "NotReady"
    if "merge-conflict" in label_names:
        return "MergeConflict"
    if any(lbl in label_names for lbl in ("blocked-by-other-pr", "blocked-by-core-pr", "blocked-by-batt-pr", "blocked-by-qq-pr")):
        return "Blocked"
    if "awaiting-author" in label_names:
        return "AwaitingAuthor"
    if "awaiting-zulip" in label_names:
        return "AwaitingDecision"
    if "delegated" in label_names:
        return "Delegated"
    if any(lbl in label_names for lbl in ("help-wanted", "please-adopt")):
        return "HelpWanted"
    if any(lbl in label_names for lbl in ("ready-to-merge", "auto-merge-after-ci")):
        return "AwaitingBors"
    return "AwaitingReview"


@dataclass
class QueueboardSnapshotBuilder:
    """Build a queueboard snapshot from DB state with bounded memory."""

    chunk_size: int = 200

    def build(self, repository: Repository, rule_set: QueueRuleSet | None = None) -> dict:
        pr_qs = (
            PullRequest.objects.filter(repository=repository, state=PullRequestState.OPEN)
            .select_related("author")
            .order_by("number")
        )

        label_map = self._labels_for_repo(repository)
        dependency_map = self._dependencies_for_repo(repository)
        ci_checks, ci_statuses = self._ci_inputs_for_repo(repository)

        prs: Dict[int, dict] = {}
        draft_prs: List[int] = []
        nondraft_prs: List[int] = []
        queue_prs: List[int] = []
        queue_new_contrib: List[int] = []
        queue_easy: List[int] = []
        queue_tech_debt: List[int] = []
        needs_decision: List[int] = []

        forbidden_labels = _forbidden_queue_labels(repository.default_branch)

        for pr in pr_qs.iterator(chunk_size=self.chunk_size):
            labels = label_map.get(pr.id, [])
            label_names = {lab["name"].lower() for lab in labels}

            ci_status = _ci_status_for_pr(
                pr.id,
                check_runs=ci_checks.get(pr.id, []),
                status_contexts=ci_statuses.get(pr.id, []),
            )
            pr_status = _classify_pr_status(label_names=label_names, ci_status=ci_status, is_draft=pr.is_draft)
            entry = self._build_pr_entry(
                pr,
                repository=repository,
                labels=labels,
                dependencies=dependency_map.get(pr.id, []),
                ci_status=ci_status,
                pr_status=pr_status,
            )
            prs[pr.number] = entry

            if pr.is_draft:
                draft_prs.append(pr.number)
            else:
                nondraft_prs.append(pr.number)

            on_queue = (
                not pr.is_draft
                and pr.base_ref_name == repository.default_branch
                and ci_status == "pass"
                and forbidden_labels.isdisjoint(label_names)
            )
            if on_queue:
                queue_prs.append(pr.number)
                if "new-contributor" in label_names:
                    queue_new_contrib.append(pr.number)
                if "easy" in label_names:
                    queue_easy.append(pr.number)
                if any(lbl in label_names for lbl in ("tech debt", "longest-pole")):
                    queue_tech_debt.append(pr.number)

            if "awaiting-zulip" in label_names:
                needs_decision.append(pr.number)

        dashboards = {
            "Queue": queue_prs,
            "QueueNewContributor": queue_new_contrib,
            "QueueEasy": queue_easy,
            "QueueTechDebt": queue_tech_debt,
            "NeedsDecision": needs_decision,
        }

        meta = {
            "schema_version": "v1-draft",
            "generated_at": _isoformat(datetime.now(timezone.utc)),
            "repository": f"{repository.owner}/{repository.name}",
            "rule_set_id": rule_set.id if rule_set else "default",
        }

        lists = {"draft_prs": draft_prs, "nondraft_prs": nondraft_prs, "dashboards": dashboards}

        return {"meta": meta, "prs": prs, "lists": lists}

    def build_and_store(
        self,
        repository: Repository,
        *,
        rule_set: QueueRuleSet | None = None,
        cache_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> QueueSnapshot:
        """Build a snapshot and persist it for reuse."""
        snapshot = self.build(repository, rule_set=rule_set)
        key = cache_key or (str(rule_set.id) if rule_set else "default")
        pr_count = len(snapshot["prs"])
        queue_count = len(snapshot["lists"]["dashboards"].get("Queue", []))
        etag = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

        obj, _ = QueueSnapshot.objects.update_or_create(
            repository=repository,
            cache_key=key,
            defaults={
                "generated_at": datetime.now(timezone.utc),
                "payload": snapshot,
                "etag": etag,
                "pr_count": pr_count,
                "queue_count": queue_count,
                "expires_at": expires_at,
            },
        )
        return obj

    def _labels_for_repo(self, repository: Repository) -> Dict[int, List[dict]]:
        qs: QuerySet[PRLabel] = PRLabel.objects.filter(pull_request__repository=repository).select_related("label_def")
        acc: Dict[int, List[dict]] = defaultdict(list)
        for pl in qs.iterator():
            label = pl.label_def
            acc[pl.pull_request_id].append({"name": label.name, "color": label.color, "url": _label_url(repository, label.name)})
        return acc

    def _dependencies_for_repo(self, repository: Repository) -> Dict[int, List[int]]:
        qs: QuerySet[PRDependency] = PRDependency.objects.filter(pull_request__repository=repository)
        acc: Dict[int, List[int]] = defaultdict(list)
        for dep in qs.iterator():
            if dep.depends_on_repository_id == repository.id:
                acc[dep.pull_request_id].append(dep.depends_on_number)
        return acc

    def _ci_inputs_for_repo(self, repository: Repository):
        checks_qs = CheckRun.objects.filter(pull_request__repository=repository).values("pull_request_id", "status", "conclusion")
        statuses_qs = StatusContext.objects.filter(pull_request__repository=repository).values("pull_request_id", "state")

        check_map: Dict[int, List[dict]] = defaultdict(list)
        for cr in checks_qs.iterator():
            check_map[cr["pull_request_id"]].append(cr)

        status_map: Dict[int, List[dict]] = defaultdict(list)
        for sc in statuses_qs.iterator():
            status_map[sc["pull_request_id"]].append(sc)

        return check_map, status_map

    def _build_pr_entry(
        self,
        pr: PullRequest,
        *,
        repository: Repository,
        labels: Iterable[dict],
        dependencies: Iterable[int],
        ci_status: str,
        pr_status: str,
    ) -> dict:
        comments_status = _data_status(bool(pr.comments_incomplete), pr.engagement_synced_at)
        users_commented = [comments_status, list(pr.commenters or [])]

        return {
            "state": pr.state,
            "is_draft": pr.is_draft,
            "base_branch": pr.base_ref_name,
            "branch_name": pr.head_ref_name,
            "last_updated": _isoformat(pr.gh_updated_at),
            "author": pr.author.github_login if pr.author else None,
            "title": pr.title,
            "description": pr.body,
            "labels": list(labels),
            "additions": pr.additions,
            "deletions": pr.deletions,
            "modified_files": pr.files or [],
            "number_modified_files": pr.changed_files_count,
            "approvals": pr.approvals or [],
            "assignees": pr.assignees or [],
            "users_commented": users_commented,
            "number_total_comments": pr.number_total_comments,
            "direct_dependencies": list(dependencies),
            "ci_status": ci_status,
            "pr_status": pr_status,
            "last_status_change": None,
            "first_on_queue": None,
            "total_queue_time": None,
        }
