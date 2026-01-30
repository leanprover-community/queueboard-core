from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import json
from typing import Dict, Iterable, List, Sequence

from dateutil import relativedelta
from django.db.models import Q, QuerySet

from analyzer.models import PRDependency, PRQueueWindow, QueueRuleSet, QueueSnapshot, PRRevision
from analyzer.services.queue_rules import QueueRules, rules_for_rule_set
from core.models import Repository
from syncer.models import PRLabel, PullRequest
from syncer.models.pull_request import PullRequestState
from syncer.models.check_run import CheckRun, CheckRunConclusion, CheckRunStatus
from syncer.models.status_context import StatusContext, StatusContextState
from queueboard.classify_pr_state import determine_PR_status, label_categorisation_rules, LabelKind, PRState, PRStatus
from queueboard.ci_status import CIStatus
from queueboard.util import format_delta


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


def _latest_ci_head_sha(check_runs: Sequence[dict], status_contexts: Sequence[dict]) -> str | None:
    latest_ts: datetime | None = None
    latest_sha: str | None = None
    for cr in check_runs:
        ts = cr.get("gh_completed_at") or cr.get("gh_started_at")
        head_sha = cr.get("head_sha")
        if not ts or not head_sha:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
            latest_sha = head_sha
    for sc in status_contexts:
        ts = sc.get("gh_created_at")
        head_sha = sc.get("head_sha")
        if not ts or not head_sha:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
            latest_sha = head_sha
    return latest_sha


def _head_sha_for_pr(
    *,
    pr_id: int,
    pr_head_sha: str | None,
    revision_heads: dict[int, str],
    check_runs: Sequence[dict],
    status_contexts: Sequence[dict],
) -> str | None:
    head_sha = pr_head_sha
    if head_sha:
        return head_sha
    head_sha = revision_heads.get(pr_id)
    if head_sha:
        return head_sha
    return _latest_ci_head_sha(check_runs, status_contexts)


def _normalize_context_fragment(name: str) -> str:
    return name.strip().lower()


def _context_name_matches(name: str, required_fragment: str) -> bool:
    required = _normalize_context_fragment(required_fragment)
    if not required:
        return False
    return required in name.lower()


def _matching_check_runs(
    check_runs: Sequence[dict],
    *,
    required_fragment: str,
    head_sha: str | None,
) -> list[dict]:
    matches: list[dict] = []
    for cr in check_runs:
        if not _context_name_matches(cr.get("name") or "", required_fragment):
            continue
        if head_sha and cr.get("head_sha") != head_sha:
            continue
        matches.append(cr)
    return matches


def _matching_status_contexts(
    status_contexts: Sequence[dict],
    *,
    required_fragment: str,
    head_sha: str | None,
) -> list[dict]:
    matches: list[dict] = []
    for sc in status_contexts:
        if not _context_name_matches(sc.get("name") or "", required_fragment):
            continue
        if head_sha and sc.get("head_sha") != head_sha:
            continue
        matches.append(sc)
    return matches


def _check_run_status(cr: dict) -> CIStatus:
    status_raw = cr.get("status")
    conclusion_raw = cr.get("conclusion")
    status = str(status_raw or "").upper()
    conclusion = str(conclusion_raw or "").upper() if conclusion_raw is not None else None
    if status in {"IN_PROGRESS", "QUEUED", "PENDING"}:
        return CIStatus.Running
    if conclusion in {CheckRunConclusion.SUCCESS, CheckRunConclusion.NEUTRAL, CheckRunConclusion.SKIPPED}:
        return CIStatus.Pass
    if conclusion in {CheckRunConclusion.FAILURE, CheckRunConclusion.CANCELLED, CheckRunConclusion.TIMED_OUT}:
        return CIStatus.Fail
    if conclusion is None and status == CheckRunStatus.COMPLETED:
        return CIStatus.Fail
    return CIStatus.Running


def _status_context_status(sc: dict) -> CIStatus:
    state = sc.get("state")
    if state == StatusContextState.SUCCESS:
        return CIStatus.Pass
    if state == StatusContextState.PENDING:
        return CIStatus.Running
    if state in (StatusContextState.FAILURE, StatusContextState.ERROR):
        return CIStatus.Fail
    return CIStatus.Running


def _context_status_from_matches(check_runs: list[dict], status_contexts: list[dict]) -> CIStatus:
    latest: dict[str, tuple[datetime, CIStatus]] = {}

    for cr in check_runs:
        name = cr.get("name")
        ts = cr.get("gh_completed_at") or cr.get("gh_started_at")
        if not name or not ts:
            continue
        key = name.lower()
        status = _check_run_status(cr)
        current = latest.get(key)
        if current is None or ts > current[0]:
            latest[key] = (ts, status)

    for sc in status_contexts:
        name = sc.get("name")
        ts = sc.get("gh_created_at")
        if not name or not ts:
            continue
        key = name.lower()
        status = _status_context_status(sc)
        current = latest.get(key)
        if current is None or ts > current[0]:
            latest[key] = (ts, status)

    if not latest:
        return CIStatus.Missing

    any_fail = any(status == CIStatus.Fail for _, status in latest.values())
    if any_fail:
        return CIStatus.Fail
    any_running = any(status == CIStatus.Running for _, status in latest.values())
    if any_running:
        return CIStatus.Running
    return CIStatus.Pass


def _required_contexts_status(
    *,
    required_contexts: Sequence[str],
    check_runs: Sequence[dict],
    status_contexts: Sequence[dict],
    head_sha: str | None,
) -> CIStatus:
    if not head_sha:
        return CIStatus.Missing

    any_fail = False
    any_running = False
    any_missing = False

    for ctx_name in required_contexts:
        cr_matches = _matching_check_runs(check_runs, required_fragment=ctx_name, head_sha=head_sha)
        sc_matches = _matching_status_contexts(status_contexts, required_fragment=ctx_name, head_sha=head_sha)
        status = _context_status_from_matches(cr_matches, sc_matches)
        if status == CIStatus.Pass:
            continue
        if status == CIStatus.Fail:
            any_fail = True
        elif status == CIStatus.Missing:
            any_missing = True
        elif status == CIStatus.Running:
            any_running = True

    if any_fail:
        return CIStatus.Fail
    if any_missing:
        return CIStatus.Missing
    if any_running:
        return CIStatus.Running
    return CIStatus.Pass


def _ci_status_for_pr(
    *,
    pr_id: int,
    pr_head_sha: str | None,
    rule_set: QueueRuleSet | None,
    check_runs: Sequence[dict],
    status_contexts: Sequence[dict],
    head_state: str | None,
    revision_heads: dict[int, str],
) -> tuple[CIStatus, bool]:
    """CI rollup aligned with queue rule set semantics."""

    if not rule_set or not rule_set.require_ci_success:
        return (CIStatus.Pass, True)

    required = [
        _normalize_context_fragment(ctx) for ctx in (rule_set.required_ci_contexts or []) if isinstance(ctx, str) and ctx.strip()
    ]
    if not required:
        return (CIStatus.Pass, True)

    head_sha = _head_sha_for_pr(
        pr_id=pr_id,
        pr_head_sha=pr_head_sha,
        revision_heads=revision_heads,
        check_runs=check_runs,
        status_contexts=status_contexts,
    )
    required_status = _required_contexts_status(
        required_contexts=required,
        check_runs=check_runs,
        status_contexts=status_contexts,
        head_sha=head_sha,
    )
    if required_status == CIStatus.Pass:
        if (head_state or "").upper() in ("FAILURE", "ERROR"):
            return (CIStatus.FailInessential, True)
        return (CIStatus.Pass, True)
    return (required_status, False)


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


def _is_queue_candidate(
    *,
    rules: QueueRules,
    is_open: bool,
    is_draft: bool,
    labels: set[str],
    ci_ok: bool | None,
    allow_forbidden_label: str | None = None,
) -> bool:
    if rules.require_open and not is_open:
        return False
    if rules.require_not_draft and is_draft:
        return False
    if rules.required_labels and not rules.required_labels.issubset(labels):
        return False
    if rules.forbidden_labels:
        forbidden = set(rules.forbidden_labels)
        if allow_forbidden_label:
            forbidden.discard(allow_forbidden_label.lower())
        if labels & forbidden:
            return False
    if rules.require_ci_success and ci_ok is not True:
        return False
    return True


@dataclass
class QueueboardSnapshotBuilder:
    """Build a queueboard snapshot from DB state with bounded memory."""

    chunk_size: int = 200

    def build(self, repository: Repository, rule_set: QueueRuleSet | None = None) -> dict:
        generated_at = datetime.now(timezone.utc)
        effective_rule_set = rule_set or self._default_rule_set(repository)
        required_contexts = self._required_contexts(effective_rule_set)
        need_ci_data = bool(required_contexts)
        pr_qs = (
            PullRequest.objects.filter(repository=repository, state=PullRequestState.OPEN)
            .select_related("author")
            .order_by("gh_updated_at", "number")
        )

        label_map = self._labels_for_repo(repository)
        dependency_map = self._dependencies_for_repo(repository)
        if need_ci_data:
            head_sha_map = self._head_shas_for_repo(repository)
            missing_head_pr_ids = {pr_id for pr_id, sha in head_sha_map.items() if not sha}
            revision_heads = {}
            if missing_head_pr_ids:
                revision_heads = self._revision_heads_for_repo(repository, pr_ids=missing_head_pr_ids)
            head_shas = {sha for sha in head_sha_map.values() if sha}
            head_shas.update(revision_heads.values())
            missing_head_pr_ids = {pr_id for pr_id in missing_head_pr_ids if pr_id not in revision_heads}
            ci_checks, ci_statuses = self._ci_inputs_for_repo(
                repository,
                head_shas=head_shas,
                required_contexts=required_contexts,
                missing_head_pr_ids=missing_head_pr_ids,
            )
        else:
            ci_checks = {}
            ci_statuses = {}
            revision_heads = {}
        queue_windows = self._queue_window_latest_for_repo(repository, rule_set=effective_rule_set)
        queue_tails = self._queue_window_tails_for_repo(repository, rule_set=effective_rule_set)

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

        rules = rules_for_rule_set(effective_rule_set) if effective_rule_set else QueueRules()
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
            window_summary = queue_windows.get(pr.id)
            tail_windows = queue_tails.get(pr.id, [])
            has_windows = bool(window_summary)
            queue_status = self._queue_data_status(pr, has_windows, effective_rule_set)
            queue_fields = self._queue_fields_for_pr(
                window_summary=window_summary,
                tail_windows=tail_windows,
                data_status=queue_status,
                generated_at=generated_at,
            )

            ci_status, ci_ok = _ci_status_for_pr(
                pr_id=pr.id,
                pr_head_sha=(pr.head_sha or "").strip() or None,
                rule_set=effective_rule_set,
                check_runs=ci_checks.get(pr.id, []),
                status_contexts=ci_statuses.get(pr.id, []),
                head_state=getattr(pr, "head_ci_state", None),
                revision_heads=revision_heads,
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

            # NOTE(parity): legacy determine_pr_dashboards builds queue candidates before
            # splitting merge-conflict PRs into NeedsMerge. Mirror that behavior while
            # still honoring ruleset requirements.
            queue_candidate = pr.base_ref_name == repository.default_branch and _is_queue_candidate(
                rules=rules,
                is_open=True,
                is_draft=pr.is_draft,
                labels=label_names_lc,
                ci_ok=ci_ok,
                allow_forbidden_label="merge-conflict",
            )
            if queue_candidate:
                if "merge-conflict" in label_names_lc:
                    needs_merge.append(pr.number)
                else:
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
        qs: QuerySet[PRLabel] = PRLabel.objects.filter(
            pull_request__repository=repository,
            pull_request__state=PullRequestState.OPEN,
        ).select_related("label_def")
        acc: Dict[int, List[dict]] = defaultdict(list)
        for pl in qs.iterator():
            label = pl.label_def
            acc[pl.pull_request_id].append({"name": label.name, "color": label.color, "url": _label_url(repository, label.name)})
        return acc

    def _queue_window_latest_for_repo(self, repository: Repository, rule_set: QueueRuleSet | None) -> Dict[int, dict]:
        if rule_set is None:
            return {}
        qs = PRQueueWindow.objects.filter(pull_request__repository=repository, pull_request__state=PullRequestState.OPEN)
        qs = qs.filter(rule_set=rule_set)
        acc: Dict[int, dict] = {}
        last_pr_id = None
        for win in qs.order_by("pull_request_id", "-from_ts", "-id").iterator():
            if win.pull_request_id == last_pr_id:
                continue
            last_pr_id = win.pull_request_id
            acc[win.pull_request_id] = {
                "from_ts": win.from_ts,
                "to_ts": win.to_ts,
                "duration_seconds_closed": win.duration_seconds_closed,
                "cumulative_seconds_closed": win.cumulative_seconds_closed,
                "window_count": win.window_count,
                "first_on_queue_ts": win.first_on_queue_ts,
            }
        return acc

    def _queue_window_tails_for_repo(self, repository: Repository, rule_set: QueueRuleSet | None) -> Dict[int, list[dict]]:
        if rule_set is None:
            return {}
        qs = PRQueueWindow.objects.filter(pull_request__repository=repository, pull_request__state=PullRequestState.OPEN)
        qs = qs.filter(rule_set=rule_set)
        acc: Dict[int, list[dict]] = defaultdict(list)
        counts: Dict[int, int] = defaultdict(int)
        for win in qs.order_by("pull_request_id", "-from_ts", "-id").iterator():
            pr_id = win.pull_request_id
            if counts[pr_id] >= 5:
                continue
            acc[pr_id].append({"from": _isoformat(win.from_ts), "to": _isoformat(win.to_ts)})
            counts[pr_id] += 1
        for pr_id, windows in acc.items():
            acc[pr_id] = list(reversed(windows))
        return acc

    def _queue_data_status(self, pr: PullRequest, has_windows: bool, rule_set: QueueRuleSet | None) -> DataStatus:
        if not getattr(pr, "timeline_backfill_done", False):
            return "missing"
        if rule_set and rule_set.require_ci_success and (rule_set.required_ci_contexts or []) and not has_windows:
            return "missing"
        return "valid"

    def _queue_fields_for_pr(
        self,
        *,
        window_summary: dict | None,
        tail_windows: list[dict],
        data_status: DataStatus,
        generated_at: datetime,
    ) -> dict:
        if not window_summary and data_status != "valid":
            return {
                "first_on_queue": {"status": data_status, "date": None},
                "total_queue_time": {"status": data_status, "value_td": None, "value_rd": None, "explanation": ""},
                "last_queue_status_change": None,
            }

        first_on_queue_ts = window_summary.get("first_on_queue_ts") if window_summary else None
        first_date = _isoformat(first_on_queue_ts) if first_on_queue_ts else None
        total_seconds = 0.0
        explanation_parts: list[str] = []
        if window_summary:
            total_seconds = float(window_summary.get("cumulative_seconds_closed") or 0)
            window_start = window_summary.get("from_ts")
            window_end = window_summary.get("to_ts")
            if window_start and window_end is None and generated_at >= window_start:
                total_seconds += (generated_at - window_start).total_seconds()
            for win in tail_windows:
                start = win.get("from")
                end = win.get("to")
                explanation = self._format_queue_window(start, end, generated_at=generated_at)
                if explanation:
                    explanation_parts.append(explanation)

        value_rd = _relativedelta_dict(total_seconds)
        total_queue_time = {
            "status": data_status,
            "value_td": int(total_seconds),
            "value_rd": value_rd,
            "explanation": self._format_queue_explanation(explanation_parts, window_summary),
        }

        last_change = None
        if window_summary:
            last_start = window_summary.get("from_ts")
            last_end = window_summary.get("to_ts")
            if last_start:
                if last_end is None:
                    on_queue = last_start <= generated_at
                    change_time = last_start
                else:
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
        qs: QuerySet[PRDependency] = PRDependency.objects.filter(
            pull_request__repository=repository,
            pull_request__state=PullRequestState.OPEN,
        )
        acc: Dict[int, List[int]] = defaultdict(list)
        for dep in qs.iterator():
            if dep.depends_on_repository_id == repository.id:
                acc[dep.pull_request_id].append(dep.depends_on_number)
        return acc

    def _ci_inputs_for_repo(
        self,
        repository: Repository,
        *,
        head_shas: set[str],
        required_contexts: Sequence[str],
        missing_head_pr_ids: set[int],
    ):
        base_checks_qs = CheckRun.objects.filter(
            pull_request__repository=repository,
            pull_request__state=PullRequestState.OPEN,
        )
        base_statuses_qs = StatusContext.objects.filter(
            pull_request__repository=repository,
            pull_request__state=PullRequestState.OPEN,
        )

        check_map: Dict[int, List[dict]] = defaultdict(list)
        status_map: Dict[int, List[dict]] = defaultdict(list)

        name_filter = Q()
        for ctx in required_contexts:
            name_filter |= Q(name__istartswith=ctx)

        checks_head_qs = base_checks_qs.none()
        if head_shas:
            checks_head_qs = base_checks_qs.filter(head_sha__in=head_shas)
            if required_contexts:
                checks_head_qs = checks_head_qs.filter(name_filter)
        checks_missing_qs = base_checks_qs.filter(pull_request_id__in=missing_head_pr_ids) if missing_head_pr_ids else None

        statuses_head_qs = base_statuses_qs.none()
        if head_shas:
            statuses_head_qs = base_statuses_qs.filter(head_sha__in=head_shas)
            if required_contexts:
                statuses_head_qs = statuses_head_qs.filter(name_filter)
        statuses_missing_qs = base_statuses_qs.filter(pull_request_id__in=missing_head_pr_ids) if missing_head_pr_ids else None

        for cr in checks_head_qs.values(
            "pull_request_id",
            "name",
            "status",
            "conclusion",
            "head_sha",
            "gh_started_at",
            "gh_completed_at",
        ).iterator():
            check_map[cr["pull_request_id"]].append(cr)
        if checks_missing_qs is not None:
            for cr in checks_missing_qs.values(
                "pull_request_id",
                "name",
                "status",
                "conclusion",
                "head_sha",
                "gh_started_at",
                "gh_completed_at",
            ).iterator():
                check_map[cr["pull_request_id"]].append(cr)

        for sc in statuses_head_qs.values(
            "pull_request_id",
            "name",
            "state",
            "head_sha",
            "gh_created_at",
        ).iterator():
            status_map[sc["pull_request_id"]].append(sc)
        if statuses_missing_qs is not None:
            for sc in statuses_missing_qs.values(
                "pull_request_id",
                "name",
                "state",
                "head_sha",
                "gh_created_at",
            ).iterator():
                status_map[sc["pull_request_id"]].append(sc)

        return check_map, status_map

    def _revision_heads_for_repo(self, repository: Repository, *, pr_ids: set[int] | None = None) -> Dict[int, str]:
        acc: Dict[int, str] = {}
        qs = PRRevision.objects.filter(
            pull_request__repository=repository,
            pull_request__state=PullRequestState.OPEN,
        ).order_by(
            "pull_request_id",
            "-from_ts",
            "-seq",
            "-id",
        )
        if pr_ids:
            qs = qs.filter(pull_request_id__in=pr_ids)
        for rev in qs.iterator():
            if rev.pull_request_id in acc:
                continue
            if rev.head_sha:
                acc[rev.pull_request_id] = rev.head_sha
        return acc

    def _required_contexts(self, rule_set: QueueRuleSet | None) -> list[str]:
        if not rule_set or not rule_set.require_ci_success:
            return []
        required = rule_set.required_ci_contexts or []
        return [ctx.strip() for ctx in required if isinstance(ctx, str) and ctx.strip()]

    def _head_shas_for_repo(self, repository: Repository) -> Dict[int, str]:
        qs = PullRequest.objects.filter(
            repository=repository,
            state=PullRequestState.OPEN,
        ).values_list("id", "head_sha")
        return {pr_id: (head_sha or "").strip() for pr_id, head_sha in qs.iterator()}

    def _format_queue_explanation(self, parts: list[str], window_summary: dict | None) -> str:
        if not parts:
            return ""
        count = 0
        if window_summary:
            count = int(window_summary.get("window_count") or 0)
        tail_count = len(parts)
        if count > tail_count and tail_count > 0:
            return f"{'\n'.join(parts)} (last {tail_count} of {count})"
        return "\n".join(parts)

    def _format_queue_window(
        self,
        start: str | None,
        end: str | None,
        *,
        generated_at: datetime,
    ) -> str | None:
        if not start:
            return None
        try:
            start_dt = datetime.fromisoformat(start)
        except ValueError:
            return f"{start} → {end}" if end else str(start)
        if end:
            try:
                end_dt = datetime.fromisoformat(end)
            except ValueError:
                return f"{start} → {end}"
            delta = relativedelta.relativedelta(end_dt, start_dt)
            return f"from {start_dt:%Y-%m-%d %H:%M} to {end_dt:%Y-%m-%d %H:%M} ({format_delta(delta)})"
        delta = relativedelta.relativedelta(generated_at, start_dt)
        return f"since {start_dt:%Y-%m-%d %H:%M} ({format_delta(delta)})"

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
            # We expose queue-derived timing only; legacy last_status_change was timeline-replay-based.
            "last_status_change": queue_fields.get("last_queue_status_change"),
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
