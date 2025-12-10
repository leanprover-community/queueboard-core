from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import json
from typing import Dict, Iterable, List, Sequence

from django.db.models import QuerySet

from analyzer.models import PRDependency, PRQueueWindow, QueueRuleSet, QueueSnapshot
from core.models import Repository
from syncer.models import PRLabel, PullRequest
from syncer.models.pull_request import PullRequestState
from syncer.models.check_run import CheckRun, CheckRunConclusion, CheckRunStatus
from syncer.models.status_context import StatusContext, StatusContextState
from queueboard.classify_pr_state import determine_PR_status, label_categorisation_rules, LabelKind, PRState, PRStatus
from queueboard.ci_status import CIStatus


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


def _relativedelta_dict(total_seconds: float) -> dict:
    seconds = int(total_seconds)
    days, rem = divmod(seconds, 24 * 3600)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    return {"days": days, "hours": hours, "minutes": minutes, "seconds": secs}


def _ci_status_for_pr(pr_id: int, check_runs: Sequence[dict], status_contexts: Sequence[dict]) -> CIStatus:
    """Coarse CI rollup aligned with legacy determine_ci_status."""
    statuses = []
    for cr in check_runs:
        status = cr["status"]
        conclusion = cr["conclusion"]
        # TODO(parity): legacy queueboard.process.determine_ci_status treats FailInessential
        # as a job-name allowlist (label-new-contributor, apply_one_t_label, etc.). This path
        # ignores job names and marks CANCELLED as inessential, so FailInessential classification diverges.
        if status != CheckRunStatus.COMPLETED:
            statuses.append(CIStatus.Running)
        elif conclusion in (CheckRunConclusion.SUCCESS, CheckRunConclusion.NEUTRAL, CheckRunConclusion.SKIPPED):
            statuses.append(CIStatus.Pass)
        elif conclusion == CheckRunConclusion.CANCELLED:
            statuses.append(CIStatus.FailInessential)
        else:
            statuses.append(CIStatus.Fail)

    for sc in status_contexts:
        state = sc["state"]
        if state == StatusContextState.PENDING:
            statuses.append(CIStatus.Running)
        elif state in (StatusContextState.FAILURE, StatusContextState.ERROR):
            statuses.append(CIStatus.Fail)
        elif state == StatusContextState.SUCCESS:
            statuses.append(CIStatus.Pass)

    if not statuses:
        return CIStatus.Missing
    if CIStatus.Running in statuses:
        return CIStatus.Running
    if CIStatus.Fail in statuses:
        return CIStatus.Fail
    if CIStatus.FailInessential in statuses:
        return CIStatus.FailInessential
    return CIStatus.Pass


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


def _classify_pr_status(
    *, label_names: set[str], ci_status: CIStatus, is_draft: bool, head_repo_owner: str, repo_owner: str
) -> str:
    """Use legacy classify_pr_state logic."""
    kinds = []
    for name in label_names:
        if name in label_categorisation_rules:
            kinds.append(label_categorisation_rules[name])
    from_fork = head_repo_owner.lower() != repo_owner.lower()
    state = PRState(kinds, ci_status, is_draft, from_fork)
    status = determine_PR_status(datetime.now(timezone.utc), state)
    return status.value


def _has_any(labels: set[str], names: tuple[str, ...]) -> bool:
    return any(name in labels for name in names)


@dataclass
class QueueboardSnapshotBuilder:
    """Build a queueboard snapshot from DB state with bounded memory."""

    chunk_size: int = 200

    def build(self, repository: Repository, rule_set: QueueRuleSet | None = None) -> dict:
        generated_at = datetime.now(timezone.utc)
        effective_rule_set = rule_set or self._default_rule_set(repository)
        pr_qs = (
            PullRequest.objects.filter(repository=repository, state=PullRequestState.OPEN)
            .select_related("author")
            .order_by("number")
        )

        label_map = self._labels_for_repo(repository)
        dependency_map = self._dependencies_for_repo(repository)
        ci_checks, ci_statuses = self._ci_inputs_for_repo(repository)
        queue_windows = self._queue_windows_for_repo(repository, rule_set=effective_rule_set)

        prs: Dict[int, dict] = {}
        draft_prs: List[int] = []
        nondraft_prs: List[int] = []
        queue_prs: List[int] = []
        queue_new_contrib: List[int] = []
        queue_easy: List[int] = []
        queue_tech_debt: List[int] = []
        queue_stale_unassigned: List[int] = []
        queue_stale_assigned: List[int] = []
        needs_decision: List[int] = []
        needs_merge: List[int] = []
        inessential_ci_fails: List[int] = []
        tech_debt: List[int] = []
        needs_help: List[int] = []
        other_base: List[int] = []
        all_ready_to_merge: List[int] = []
        stale_ready_to_merge: List[int] = []
        stale_delegated: List[int] = []
        stale_maintainer_merge: List[int] = []
        all_maintainer_merge: List[int] = []
        stale_new_contributor: List[int] = []
        approved: List[int] = []
        bad_title: List[int] = []
        unlabelled: List[int] = []
        contradictory: List[int] = []
        all_prs: List[int] = []

        forbidden_labels = _forbidden_queue_labels(repository.default_branch)
        now = datetime.now(timezone.utc)
        stale_queue_threshold = now - timedelta(days=3)
        stale_queue_assigned_threshold = now - timedelta(days=14)
        stale_ready_threshold = now - timedelta(days=1)
        stale_new_contrib_threshold = now - timedelta(days=7)
        # NOTE(parity): legacy stale queue logic uses last_status_change timestamps; Analyzer falls
        # back to gh_updated_at because timeline replay is not wired in yet.

        for pr in pr_qs.iterator(chunk_size=self.chunk_size):
            labels = label_map.get(pr.id, [])
            label_names = {lab["name"] for lab in labels}
            label_names_lc = {name.lower() for name in label_names}
            all_prs.append(pr.number)
            windows = queue_windows.get(pr.id, [])
            queue_status = self._queue_data_status(pr, windows, effective_rule_set)
            queue_fields = self._queue_fields_for_pr(
                windows=windows,
                data_status=queue_status,
                generated_at=generated_at,
            )

            ci_status = _ci_status_for_pr(
                pr.id,
                check_runs=ci_checks.get(pr.id, []),
                status_contexts=ci_statuses.get(pr.id, []),
            )
            ci_value = ci_status.value if isinstance(ci_status, CIStatus) else str(ci_status)
            pr_status = _classify_pr_status(
                label_names=label_names,
                ci_status=ci_status,
                is_draft=pr.is_draft,
                head_repo_owner=pr.head_repo_owner_login,
                repo_owner=repository.owner,
            )
            entry = self._build_pr_entry(
                pr,
                repository=repository,
                labels=labels,
                dependencies=dependency_map.get(pr.id, []),
                ci_status=ci_status,
                pr_status=pr_status,
                queue_fields=queue_fields,
                queue_status=queue_status,
            )
            prs[pr.number] = entry

            if pr.is_draft:
                draft_prs.append(pr.number)
            else:
                nondraft_prs.append(pr.number)
                # NOTE(parity): legacy Approved dashboard includes WIP-labelled PRs; we drop WIP here.
                if "wip" not in label_names_lc:
                    if pr.approvals:
                        approved.append(pr.number)
                    topic_label = any(
                        lbl["name"].lower() in {"ci", "imo"} or lbl["name"].lower().startswith("t-") for lbl in labels
                    )
                    if (pr.title or "").startswith("feat") and not topic_label:
                        unlabelled.append(pr.number)
                    lowered_title = (pr.title or "").lower()
                    if lowered_title and not lowered_title.startswith(
                        ("feat", "chore", "perf", "refactor", "style", "fix", "doc")
                    ):
                        bad_title.append(pr.number)
                    if _has_contradictory_labels(label_names_lc):
                        contradictory.append(pr.number)

            # NOTE(parity): legacy determine_pr_dashboards drops merge-conflict PRs from Queue
            # and can optionally source queue.json; this path always uses aggregate data and keeps
            # merge-conflict PRs in Queue membership.
            on_queue = (
                not pr.is_draft
                and pr.base_ref_name == repository.default_branch
                and ci_value == CIStatus.Pass.value
                and forbidden_labels.isdisjoint(label_names_lc)
            )
            if on_queue:
                queue_prs.append(pr.number)
                if "new-contributor" in label_names_lc:
                    queue_new_contrib.append(pr.number)
                if "easy" in label_names_lc:
                    queue_easy.append(pr.number)
                if any(lbl in label_names_lc for lbl in ("tech debt", "longest-pole")):
                    queue_tech_debt.append(pr.number)
                if (pr.assignees or []) and pr.gh_updated_at < stale_queue_assigned_threshold:
                    queue_stale_assigned.append(pr.number)
                if not pr.assignees and pr.gh_updated_at < stale_queue_threshold:
                    queue_stale_unassigned.append(pr.number)

            if "awaiting-zulip" in label_names_lc:
                needs_decision.append(pr.number)
            if "merge-conflict" in label_names_lc and on_queue:
                needs_merge.append(pr.number)
            # NOTE(parity): legacy InessentialCIFails also filters out blocked/help-wanted/etc.
            # labels; here we include all default-branch FailInessential nondraft PRs.
            if ci_value == CIStatus.FailInessential.value and pr.base_ref_name == repository.default_branch and not pr.is_draft:
                inessential_ci_fails.append(pr.number)
            if any(lbl in label_names_lc for lbl in ("tech debt", "longest-pole")) and not pr.is_draft:
                tech_debt.append(pr.number)
            if any(lbl in label_names_lc for lbl in ("help-wanted", "please-adopt")) and not pr.is_draft:
                needs_help.append(pr.number)
            if pr.base_ref_name != repository.default_branch and not pr.is_draft:
                other_base.append(pr.number)
            if any(lbl in label_names_lc for lbl in ("ready-to-merge", "auto-merge-after-ci")) and not pr.is_draft:
                all_ready_to_merge.append(pr.number)
                if pr.gh_updated_at < stale_ready_threshold:
                    stale_ready_to_merge.append(pr.number)
            if "delegated" in label_names_lc and pr.gh_updated_at < stale_ready_threshold and not pr.is_draft:
                stale_delegated.append(pr.number)
            if "maintainer-merge" in label_names_lc and not any(
                lbl in label_names_lc for lbl in ("ready-to-merge", "auto-merge-after-ci")
            ):
                # NOTE(parity): legacy AllMaintainerMerge only includes PRs older than a day;
                # we currently keep all maintainer-merge PRs here and use staleness only for the stale subset.
                all_maintainer_merge.append(pr.number)
                if pr.gh_updated_at < stale_ready_threshold:
                    stale_maintainer_merge.append(pr.number)
            if "new-contributor" in label_names_lc and pr.gh_updated_at < stale_new_contrib_threshold:
                stale_new_contributor.append(pr.number)

        dashboards = {
            "Queue": queue_prs,
            "QueueNewContributor": queue_new_contrib,
            "QueueEasy": queue_easy,
            "QueueTechDebt": queue_tech_debt,
            "QueueStaleUnassigned": queue_stale_unassigned,
            "QueueStaleAssigned": queue_stale_assigned,
            "NeedsDecision": needs_decision,
            "NeedsMerge": needs_merge,
            "InessentialCIFails": inessential_ci_fails,
            "TechDebt": tech_debt,
            "NeedsHelp": needs_help,
            "OtherBase": other_base,
            "AllReadyToMerge": all_ready_to_merge,
            "StaleReadyToMerge": stale_ready_to_merge,
            "StaleDelegated": stale_delegated,
            "StaleMaintainerMerge": stale_maintainer_merge,
            "AllMaintainerMerge": all_maintainer_merge,
            "StaleNewContributor": stale_new_contributor,
            "Approved": approved,
            "BadTitle": bad_title,
            "Unlabelled": unlabelled,
            "ContradictoryLabels": contradictory,
            "All": all_prs,
        }

        meta = {
            "schema_version": "v1-draft",
            "generated_at": _isoformat(generated_at),
            "repository": f"{repository.owner}/{repository.name}",
            "rule_set_id": effective_rule_set.id if effective_rule_set else "default",
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

    def _queue_windows_for_repo(self, repository: Repository, rule_set: QueueRuleSet | None) -> Dict[int, List[tuple]]:
        qs = PRQueueWindow.objects.filter(pull_request__repository=repository)
        if rule_set:
            qs = qs.filter(rule_set=rule_set)
        acc: Dict[int, List[tuple]] = defaultdict(list)
        for win in qs.order_by("pull_request_id", "from_ts").iterator():
            acc[win.pull_request_id].append((win.from_ts, win.to_ts))
        return acc

    def _queue_data_status(self, pr: PullRequest, windows: list[tuple], rule_set: QueueRuleSet | None) -> DataStatus:
        if not getattr(pr, "timeline_backfill_done", False):
            return "missing"
        if rule_set and rule_set.require_ci_success and not windows:
            return "missing"
        return "valid"

    def _queue_fields_for_pr(self, *, windows: list[tuple], data_status: DataStatus, generated_at: datetime) -> dict:
        if not windows and data_status != "valid":
            return {
                "first_on_queue": {"status": data_status, "date": None},
                "total_queue_time": {"status": data_status, "value_td": None, "value_rd": None, "explanation": ""},
                "last_queue_status_change": None,
            }

        first_date = _isoformat(windows[0][0]) if windows else None
        total_seconds = 0
        explanation_parts: list[str] = []
        for start, end in windows:
            end_clamped = end if end <= generated_at else generated_at
            if start >= end_clamped:
                continue
            total_seconds += (end_clamped - start).total_seconds()
            explanation_parts.append(f"{_isoformat(start)} → {_isoformat(end_clamped)}")

        value_rd = _relativedelta_dict(total_seconds)
        total_queue_time = {
            "status": data_status,
            "value_td": int(total_seconds),
            "value_rd": value_rd,
            "explanation": "; ".join(explanation_parts),
        }

        last_change = None
        if windows:
            last_start, last_end = windows[-1]
            on_queue = last_start <= generated_at < last_end
            change_time = last_start if on_queue else last_end
            delta_seconds = (generated_at - change_time).total_seconds()
            last_change = {
                "status": data_status,
                "time": _isoformat(change_time),
                "delta": _relativedelta_dict(delta_seconds),
                "current_status": "OnQueue" if on_queue else "OffQueue",
            }

        return {
            "first_on_queue": {"status": data_status, "date": first_date},
            "total_queue_time": total_queue_time,
            "last_queue_status_change": last_change,
        }

    def _default_rule_set(self, repository: Repository) -> QueueRuleSet | None:
        return QueueRuleSet.objects.filter(repository=repository, is_active=True).order_by("-version", "-id").first()

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
        ci_status: CIStatus,
        pr_status: str,
        queue_fields: dict,
        queue_status: DataStatus,
    ) -> dict:
        comments_status = _data_status(bool(pr.comments_incomplete), pr.engagement_synced_at)
        assignees_status = _data_status(bool(pr.assignees_incomplete), pr.engagement_synced_at)
        files_status = _data_status(bool(pr.files_incomplete), pr.engagement_synced_at)
        approvals_status = _data_status(bool(pr.reviews_incomplete), pr.engagement_synced_at)
        users_commented = [comments_status, list(pr.commenters or [])]

        return {
            "state": pr.state,
            "is_draft": pr.is_draft,
            "base_branch": pr.base_ref_name,
            "branch_name": pr.head_ref_name,
            "head_repo": pr.head_repo_owner_login,
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
            "ci_status": ci_status.value if isinstance(ci_status, CIStatus) else str(ci_status),
            "pr_status": pr_status,
            # NOTE(parity): legacy snapshot populates the timeline-derived fields from state_evolution;
            # Analyzer has not ported that yet, so these remain empty.
            "last_status_change": None,
            "last_queue_status_change": queue_fields.get("last_queue_status_change"),
            "first_on_queue": queue_fields.get("first_on_queue"),
            "total_queue_time": queue_fields.get("total_queue_time"),
            # NOTE: legacy snapshot payloads omit data_status; we expose it here for API consumers.
            "data_status": {
                "files": files_status,
                "assignees": assignees_status,
                "approvals": approvals_status,
                "comments": comments_status,
                "queue": queue_status,
            },
        }


def _has_contradictory_labels(label_names: set[str]) -> bool:
    """Match legacy has_contradictory_labels heuristics assuming no WIP in the set."""
    canonical = set()
    for name in label_names:
        if name in {"ready-to-merge", "auto-merge-after-ci"}:
            canonical.add("bors")
        elif name in {"blocked-by-other-pr", "blocked-by-core-pr", "blocked-by-batt-pr", "blocked-by-qq-pr"}:
            canonical.add("blocked")
        else:
            canonical.add(name)

    if "awaiting-review-dont-use" in canonical:
        return True
    if "bors" in canonical and {"awaiting-author", "awaiting-zulip"} & canonical:
        return True
    # Legacy also treated WIP+awaiting-review as contradictory; WIP is excluded upstream.
    return False
