from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from analyzer.models import PRDependency, PRQueueWindow, QueueSnapshot
from analyzer.services.ci_evaluation import ci_status_for_pr
from analyzer.services.queue_rules import QueueRules, default_rule_set_for_repo, load_rules_for_repo
from core.models import Repository
from syncer.models import PRLabel, PullRequest

SNAPSHOT_STALE_SECONDS = 7200  # 2 hours


@dataclass(frozen=True)
class DependencyInfo:
    owner: str
    repo: str
    number: int
    state: str  # "open", "closed", "merged", "unknown"
    is_draft: bool
    title: str | None


@dataclass(frozen=True)
class PRQueueInfo:
    owner: str
    repo: str
    number: int
    title: str
    url: str

    state: str  # "open", "closed", "merged"
    is_draft: bool
    author_login: str | None

    created_at: datetime | None
    updated_at: datetime | None
    closed_at: datetime | None
    merged_at: datetime | None

    labels: list[str]
    assignee_logins: list[str]
    ci_status: str
    ci_requires_success: bool

    on_queue: bool
    off_queue_reasons: list[str]
    queue_since: datetime | None
    total_queue_seconds: int | None

    dependencies: list[DependencyInfo]

    snapshot_generated_at: datetime | None
    snapshot_is_stale: bool

    source: str  # "snapshot" or "db"


def get_pr_queue_info(owner: str, repo_name: str, pr_number: int) -> PRQueueInfo | None:
    """Return queue info for a PR.

    Prefers the default QueueSnapshot (open PRs). Falls back to a direct DB
    query for merged/closed PRs or when the snapshot is missing.
    Returns None if the PR is not found in the database at all.
    """
    try:
        repository = Repository.objects.get(owner__iexact=owner, name__iexact=repo_name)
    except Repository.DoesNotExist:
        return None

    rule_set = default_rule_set_for_repo(repository)
    cache_key = str(rule_set.id) if rule_set else "default"
    snapshot = QueueSnapshot.objects.filter(repository=repository, cache_key=cache_key).order_by("-generated_at").first()

    now = datetime.now(timezone.utc)
    snapshot_generated_at: datetime | None = None
    snapshot_is_stale = False

    if snapshot and snapshot.payload:
        snapshot_generated_at = snapshot.generated_at
        if snapshot_generated_at is not None and snapshot_generated_at.tzinfo is None:
            snapshot_generated_at = snapshot_generated_at.replace(tzinfo=timezone.utc)
        snapshot_is_stale = (
            snapshot_generated_at is None or (now - snapshot_generated_at).total_seconds() > SNAPSHOT_STALE_SECONDS
        )

        prs = snapshot.payload.get("prs", {})
        pr_data = prs.get(str(pr_number))
        if pr_data is not None:
            return _from_snapshot(
                owner=owner,
                repo_name=repo_name,
                pr_number=pr_number,
                pr_data=pr_data,
                snapshot_payload=snapshot.payload,
                snapshot_generated_at=snapshot_generated_at,
                snapshot_is_stale=snapshot_is_stale,
                repository=repository,
            )

    return _from_db(
        owner=owner,
        repo_name=repo_name,
        pr_number=pr_number,
        repository=repository,
        snapshot_generated_at=snapshot_generated_at,
        snapshot_is_stale=snapshot_is_stale,
    )


def _from_snapshot(
    *,
    owner: str,
    repo_name: str,
    pr_number: int,
    pr_data: dict[str, Any],
    snapshot_payload: dict[str, Any],
    snapshot_generated_at: datetime | None,
    snapshot_is_stale: bool,
    repository: Repository,
) -> PRQueueInfo:
    dashboards: dict[str, list[int]] = (snapshot_payload.get("lists") or {}).get("dashboards", {})
    on_queue = pr_number in dashboards.get("Queue", [])

    rules = load_rules_for_repo(repository, at=snapshot_generated_at)
    off_queue_reasons: list[str] = [] if on_queue else _off_queue_reasons_from_snapshot(pr_data, dashboards, pr_number, rules)

    labels = [lbl["name"] for lbl in pr_data.get("labels", [])]
    assignees: list[str] = list(pr_data.get("assignees") or [])
    ci_status: str = pr_data.get("ci_status") or "missing"

    queue_since: datetime | None = None
    last_change = pr_data.get("last_queue_status_change")
    if on_queue and last_change and last_change.get("current_status") == "OnQueue":
        queue_since = _parse_iso(last_change.get("time"))

    total_queue_seconds: int | None = None
    total_qt = pr_data.get("total_queue_time")
    if total_qt and total_qt.get("value_td") is not None:
        total_queue_seconds = int(total_qt["value_td"])

    dep_numbers: list[int] = list(pr_data.get("direct_dependencies") or [])
    dep_snapshot_prs: dict[str, Any] = snapshot_payload.get("prs", {})
    dep_db_states = _dep_states_from_db(repository, dep_numbers)
    dependencies = []
    for dep_num in dep_numbers:
        dep_data = dep_snapshot_prs.get(str(dep_num))
        if dep_data is not None:
            dep_info = DependencyInfo(
                owner=owner,
                repo=repo_name,
                number=dep_num,
                state="open",
                is_draft=bool(dep_data.get("is_draft")),
                title=dep_data.get("title"),
            )
        else:
            db = dep_db_states.get(dep_num)
            dep_info = DependencyInfo(
                owner=owner,
                repo=repo_name,
                number=dep_num,
                state=db[0] if db else "unknown",
                is_draft=db[1] if db else False,
                title=db[2] if db else None,
            )
        dependencies.append(dep_info)

    return PRQueueInfo(
        owner=owner,
        repo=repo_name,
        number=pr_number,
        title=pr_data.get("title") or f"PR #{pr_number}",
        url=f"https://github.com/{owner}/{repo_name}/pull/{pr_number}",
        state=pr_data.get("state") or "open",
        is_draft=bool(pr_data.get("is_draft")),
        author_login=pr_data.get("author"),
        created_at=_parse_iso(pr_data.get("created_at")),
        updated_at=_parse_iso(pr_data.get("last_updated")),
        closed_at=None,
        merged_at=None,
        labels=labels,
        assignee_logins=assignees,
        ci_status=ci_status,
        ci_requires_success=_ci_missing_is_failure(rules),
        on_queue=on_queue,
        off_queue_reasons=off_queue_reasons,
        queue_since=queue_since,
        total_queue_seconds=total_queue_seconds,
        dependencies=dependencies,
        snapshot_generated_at=snapshot_generated_at,
        snapshot_is_stale=snapshot_is_stale,
        source="snapshot",
    )


def _from_db(
    *,
    owner: str,
    repo_name: str,
    pr_number: int,
    repository: Repository,
    snapshot_generated_at: datetime | None,
    snapshot_is_stale: bool,
) -> PRQueueInfo | None:
    try:
        pr = PullRequest.objects.select_related("author").get(repository=repository, number=pr_number)
    except PullRequest.DoesNotExist:
        return None

    now = datetime.now(timezone.utc)

    labels: list[str] = list(
        PRLabel.objects.filter(pull_request=pr).select_related("label_def").values_list("label_def__name", flat=True)
    )
    assignees: list[str] = list(pr.assignees or [])

    rules = load_rules_for_repo(repository)
    ci_status: str = ci_status_for_pr(pr, rules, repository)

    # Queue timing: find the latest cycle across active rule sets for this repo.
    queue_windows = (
        PRQueueWindow.objects.filter(
            pull_request=pr,
            rule_set__repository=repository,
            rule_set__is_active=True,
        )
        .select_related("rule_set")
        .order_by("-from_ts")
    )
    on_queue = False
    queue_since: datetime | None = None
    total_queue_seconds: int | None = None
    latest_window = queue_windows.first()
    if latest_window:
        if latest_window.to_ts is None:
            on_queue = True
            queue_since = latest_window.from_ts
            if queue_since is not None and queue_since.tzinfo is None:
                queue_since = queue_since.replace(tzinfo=timezone.utc)
        latest_cycle = queue_windows.order_by("-cycle_index").first()
        if latest_cycle:
            cumulative = int(latest_cycle.cumulative_seconds_closed)
            if latest_cycle.to_ts is None and latest_cycle.from_ts is not None:
                start = latest_cycle.from_ts
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                cumulative += int((now - start).total_seconds())
            total_queue_seconds = cumulative

    dep_rows = list(
        PRDependency.objects.filter(pull_request=pr)
        .select_related(
            "depends_on_pull_request",
            "depends_on_repository",
        )
        .order_by("depends_on_number")
    )
    dependencies = []
    for dep in dep_rows:
        dep_repo = dep.depends_on_repository
        dep_pr = dep.depends_on_pull_request
        if dep_pr is not None:
            dep_state = "merged" if dep_pr.merged_at else dep_pr.state
            dep_info = DependencyInfo(
                owner=dep_repo.owner,
                repo=dep_repo.name,
                number=dep.depends_on_number,
                state=dep_state,
                is_draft=dep_pr.is_draft,
                title=dep_pr.title,
            )
        else:
            dep_info = DependencyInfo(
                owner=dep_repo.owner,
                repo=dep_repo.name,
                number=dep.depends_on_number,
                state="unknown",
                is_draft=False,
                title=None,
            )
        dependencies.append(dep_info)

    state = "merged" if pr.merged_at else pr.state

    off_queue_reasons: list[str] = []
    if not on_queue and state == "open":
        label_set = {lbl.lower() for lbl in labels}
        off_queue_reasons = _off_queue_reasons_from_labels(label_set, rules, ci_status, pr.is_draft)

    return PRQueueInfo(
        owner=owner,
        repo=repo_name,
        number=pr_number,
        title=pr.title or f"PR #{pr_number}",
        url=f"https://github.com/{owner}/{repo_name}/pull/{pr_number}",
        state=state,
        is_draft=pr.is_draft,
        author_login=pr.author.github_login if pr.author else None,
        created_at=_ensure_utc(pr.gh_created_at),
        updated_at=_ensure_utc(pr.gh_updated_at),
        closed_at=_ensure_utc(pr.closed_at),
        merged_at=_ensure_utc(pr.merged_at),
        labels=labels,
        assignee_logins=assignees,
        ci_status=ci_status,
        ci_requires_success=_ci_missing_is_failure(rules),
        on_queue=on_queue,
        off_queue_reasons=off_queue_reasons,
        queue_since=queue_since,
        total_queue_seconds=total_queue_seconds,
        dependencies=dependencies,
        snapshot_generated_at=snapshot_generated_at,
        snapshot_is_stale=snapshot_is_stale,
        source="db",
    )


# ---------------------------------------------------------------------------
# CI display helpers
# ---------------------------------------------------------------------------

_NO_REQUIRED_FAILURES = "no_required_failures"


def _ci_missing_is_failure(rules: QueueRules) -> bool:
    """Return True only when a missing CI status should be treated as a failure.

    In ALL_REQUIRED_SUCCESS mode every required context must explicitly pass, so
    missing data is a failure.  In NO_REQUIRED_FAILURES mode the gate only trips
    on explicit required-context failures, so missing data is a pass.
    """
    return rules.require_ci_success and rules.ci_gating_mode != _NO_REQUIRED_FAILURES


# ---------------------------------------------------------------------------
# Off-queue reason derivation
# ---------------------------------------------------------------------------

# Forbidden labels paired with human-readable reasons, checked in order.
_FORBIDDEN_LABEL_REASONS: list[tuple[str, str]] = [
    ("merge-conflict", "merge-conflict label"),
    ("awaiting-author", "awaiting author"),
    ("awaiting-zulip", "awaiting Zulip discussion"),
    ("awaiting-ci", "awaiting CI result"),
    ("wip", "labeled WIP"),
    ("delegated", "labeled delegated"),
    ("ready-to-merge", "labeled ready-to-merge"),
    ("auto-merge-after-ci", "labeled auto-merge-after-ci"),
    ("maintainer-merge", "labeled maintainer-merge"),
    ("help-wanted", "labeled help-wanted"),
    ("please-adopt", "labeled please-adopt"),
]


def _off_queue_reasons_from_snapshot(
    pr_data: dict[str, Any],
    dashboards: dict[str, list[int]],
    pr_number: int,
    rules: QueueRules,
) -> list[str]:
    if pr_data.get("is_draft"):
        return ["draft PR"]

    if pr_number in dashboards.get("OtherBase", []):
        base = pr_data.get("base_branch") or "non-default"
        return [f"targets non-default branch ({base!r})"]

    label_names_lc = {lbl["name"].lower() for lbl in pr_data.get("labels", [])}
    ci_status: str = pr_data.get("ci_status") or "missing"
    return _off_queue_reasons_from_labels(label_names_lc, rules, ci_status, False)


def _off_queue_reasons_from_labels(
    label_names_lc: set[str],
    rules: QueueRules,
    ci_status: str,
    is_draft: bool,
) -> list[str]:
    if is_draft:
        return ["draft PR"]

    reasons: list[str] = []

    blocked_by = [lbl for lbl in label_names_lc if lbl.startswith("blocked-by-")]
    if blocked_by:
        reasons.append("blocked-by label present")

    for label, reason in _FORBIDDEN_LABEL_REASONS:
        if label in label_names_lc:
            reasons.append(reason)

    if rules.required_labels:
        missing = rules.required_labels - label_names_lc
        if missing:
            reasons.append(f"missing required label(s): {', '.join(sorted(missing))}")

    if rules.require_ci_success and ci_status in ("fail", "running", "missing"):
        reasons.append(f"CI not passing ({ci_status})")

    if not reasons:
        reasons.append("not queue-labeled")

    return reasons


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dep_states_from_db(repository: Repository, dep_numbers: list[int]) -> dict[int, tuple[str, bool, str | None]]:
    """Return {pr_number: (state, is_draft, title)} for a batch of dep numbers."""
    if not dep_numbers:
        return {}
    rows = PullRequest.objects.filter(repository=repository, number__in=dep_numbers).values(
        "number", "state", "is_draft", "title", "merged_at"
    )
    return {
        row["number"]: (
            "merged" if row["merged_at"] else row["state"],
            bool(row["is_draft"]),
            row["title"],
        )
        for row in rows
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
