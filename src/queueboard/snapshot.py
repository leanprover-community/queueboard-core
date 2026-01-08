from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Iterable, List, Tuple
from dateutil import parser, relativedelta

from queueboard.ci_status import CIStatus
from queueboard.classify_pr_state import PRStatus
from queueboard.compute_dashboard_prs import (
    AggregatePRInfo,
    BasicPRInformation,
    DataStatus,
    Label,
    LastStatusChange,
    TotalQueueTime,
    infer_pr_url,
)
from queueboard.mathlib_dashboards import Dashboard


def _isoformat(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    # Ensure timezone-aware output; legacy data is already UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _serialize_relativedelta(rd) -> Dict[str, int]:
    """Convert a relativedelta to a plain dict with non-zero components."""
    if rd is None:
        return {}
    fields = ("years", "months", "days", "hours", "minutes", "seconds", "microseconds")
    return {field: getattr(rd, field) for field in fields if getattr(rd, field, 0) not in (None, 0)}


def _serialize_timedelta(td: timedelta | None) -> float | None:
    if td is None:
        return None
    return td.total_seconds()


def _serialize_label(label: Label) -> Dict[str, str]:
    return {"name": label.name, "color": label.color, "url": label.url}


def _serialize_users_commented(users: Tuple[DataStatus, List[str]] | None):
    if users is None:
        return None
    status, commenters = users
    return [status.value, commenters]


def _serialize_last_status_change(last: LastStatusChange | None) -> Dict | None:
    if last is None:
        return None
    return {
        "status": last.status.value,
        "time": _isoformat(last.time),
        "delta": _serialize_relativedelta(last.delta),
        "current_status": last.current_status.value,
    }


def _serialize_total_queue_time(tqt: TotalQueueTime | None) -> Dict | None:
    if tqt is None:
        return None
    return {
        "status": tqt.status.value,
        "value_td": _serialize_timedelta(tqt.value_td),
        "value_rd": _serialize_relativedelta(tqt.value_rd),
        "explanation": tqt.explanation,
    }


def _serialize_pr(
    number: int,
    info: AggregatePRInfo,
    pr_statuses: Dict[int, PRStatus],
) -> Dict:
    return {
        "state": info.state,
        "is_draft": info.is_draft,
        "base_branch": info.base_branch,
        "branch_name": info.branch_name,
        "last_updated": _isoformat(info.last_updated),
        "author": info.author,
        "title": info.title,
        "description": info.description,
        "labels": [_serialize_label(label) for label in info.labels],
        "additions": info.additions,
        "deletions": info.deletions,
        "modified_files": info.modified_files,
        "number_modified_files": info.number_modified_files,
        "approvals": info.approvals,
        "assignees": info.assignees,
        "users_commented": _serialize_users_commented(info.users_commented),
        "number_total_comments": info.number_total_comments,
        "direct_dependencies": info.direct_dependencies,
        "ci_status": info.CI_status.value if isinstance(info.CI_status, CIStatus) else str(info.CI_status),
        "pr_status": pr_statuses.get(number).value if number in pr_statuses else None,
        "last_status_change": _serialize_last_status_change(info.last_status_change),
        "first_on_queue": None
        if info.first_on_queue is None
        else [info.first_on_queue[0].value, _isoformat(info.first_on_queue[1])],
        "total_queue_time": _serialize_total_queue_time(info.total_queue_time),
    }


def _pr_numbers(prs: Iterable[BasicPRInformation]) -> List[int]:
    return [pr.number for pr in prs]


def build_snapshot(
    aggregate_info: Dict[int, AggregatePRInfo],
    pr_statuses: Dict[int, PRStatus],
    draft_prs: List[BasicPRInformation],
    nondraft_prs: List[BasicPRInformation],
    prs_to_list: Dict[Dashboard, List[BasicPRInformation]],
    repository: str = "leanprover-community/mathlib4",
    rule_set_id: str | None = None,
) -> Dict:
    """Construct the minimal dashboard snapshot described in docs/queueboard_api_contract.md."""
    meta = {
        "schema_version": "v1-draft",
        "generated_at": _isoformat(datetime.now(timezone.utc)),
        "repository": repository,
        "rule_set_id": rule_set_id or "default",
    }

    prs = {number: _serialize_pr(number, info, pr_statuses) for number, info in aggregate_info.items()}

    dashboards = {kind.name: _pr_numbers(prs_list) for kind, prs_list in prs_to_list.items()}

    lists = {
        "draft_prs": _pr_numbers(draft_prs),
        "nondraft_prs": _pr_numbers(nondraft_prs),
        "dashboards": dashboards,
    }

    return {"meta": meta, "prs": prs, "lists": lists}


def _parse_relativedelta(data: Dict[str, int] | None):
    if not data:
        return relativedelta.relativedelta()
    return relativedelta.relativedelta(**data)


def _parse_timedelta(seconds: float | int | None):
    if seconds is None:
        return None
    return timedelta(seconds=seconds)


def _parse_last_status_change(payload: Dict | None, *, fallback_status: str | None = None) -> LastStatusChange | None:
    if not payload:
        return None
    status = DataStatus.fromStr(payload["status"])
    time = parser.isoparse(payload["time"]) if payload.get("time") else None
    delta = _parse_relativedelta(payload.get("delta"))
    raw_current = payload.get("current_status")
    current = PRStatus.tryFrom_str(raw_current)
    if current is None and raw_current in {"OnQueue", "OffQueue"} and fallback_status:
        current = PRStatus.tryFrom_str(fallback_status)
    if time is None or current is None:
        return None
    return LastStatusChange(status, time, delta, current)


def _parse_total_queue_time(payload: Dict | None) -> TotalQueueTime | None:
    if not payload:
        return None
    status = DataStatus.fromStr(payload["status"])
    td = _parse_timedelta(payload.get("value_td"))
    rd = _parse_relativedelta(payload.get("value_rd"))
    explanation = payload.get("explanation", "")
    if td is None:
        return None
    return TotalQueueTime(status, td, rd, explanation)


def _aggregate_from_snapshot(pr_number: int, payload: Dict) -> AggregatePRInfo:
    # When reading, build Label objects directly.
    labels = [Label(label["name"], label["color"], label["url"]) for label in payload.get("labels", [])]
    users_commented = payload.get("users_commented")
    uc_tuple: Tuple[DataStatus, List[str]] | None = None
    if users_commented:
        uc_tuple = (DataStatus.fromStr(users_commented[0]), users_commented[1])

    foq = payload.get("first_on_queue")
    first_on_queue = None
    if foq:
        status = None
        time = None
        if isinstance(foq, (list, tuple)) and len(foq) >= 2:
            status = DataStatus.fromStr(foq[0])
            time = parser.isoparse(foq[1]) if foq[1] else None
        elif isinstance(foq, dict):
            status = DataStatus.fromStr(foq.get("status"))
            date_str = foq.get("date") or foq.get("time")
            time = parser.isoparse(date_str) if date_str else None
        if status is not None:
            first_on_queue = (status, time)

    return AggregatePRInfo(
        payload["is_draft"],
        CIStatus.from_string(payload.get("ci_status")),
        payload["base_branch"],
        payload["branch_name"],
        payload.get("head_repo", "leanprover-community"),
        payload["state"],
        parser.isoparse(payload["last_updated"]),
        payload["author"],
        payload["title"],
        payload["description"],
        payload.get("direct_dependencies", []),
        labels,
        payload["additions"],
        payload["deletions"],
        payload.get("modified_files", []),
        payload["number_modified_files"],
        payload.get("approvals", []),
        payload.get("assignees", []),
        uc_tuple or (DataStatus.Missing, []),
        payload.get("number_total_comments"),
        _parse_last_status_change(payload.get("last_status_change"), fallback_status=payload.get("pr_status")),
        first_on_queue,
        _parse_total_queue_time(payload.get("total_queue_time")),
    )


def _to_basic(pr_number: int, info: AggregatePRInfo) -> BasicPRInformation:
    return BasicPRInformation(pr_number, info.author, info.title, infer_pr_url(pr_number), info.labels, info.last_updated)


def load_snapshot(api_dir: str):
    """Load snapshot.json and return the legacy structures the dashboards expect."""
    with open(f"{api_dir}/snapshot.json", "r") as f:
        snapshot = json.load(f)

    prs_payload = snapshot["prs"]
    aggregate_info: Dict[str, AggregatePRInfo] = {}
    all_pr_status: Dict[str, PRStatus] = {}
    CI_status: Dict[str, CIStatus] = {}
    base_branch: Dict[str, str] = {}

    for num_str, payload in prs_payload.items():
        number = int(num_str)
        info = _aggregate_from_snapshot(number, payload)
        aggregate_info[num_str] = info
        CI_status[num_str] = info.CI_status
        base_branch[num_str] = info.base_branch
        if payload.get("pr_status") is not None:
            status = PRStatus.tryFrom_str(payload["pr_status"])
            if status:
                all_pr_status[num_str] = status

    draft_prs = [
        _to_basic(int(n), aggregate_info[str(n)]) for n in snapshot["lists"].get("draft_prs", []) if str(n) in aggregate_info
    ]
    nondraft_prs = [
        _to_basic(int(n), aggregate_info[str(n)]) for n in snapshot["lists"].get("nondraft_prs", []) if str(n) in aggregate_info
    ]

    dashboards_payload = snapshot["lists"].get("dashboards", {})
    prs_to_list: Dict[Dashboard, List[BasicPRInformation]] = {}
    for dash_name, pr_numbers in dashboards_payload.items():
        try:
            dash = Dashboard[dash_name]
        except KeyError:
            continue
        prs_to_list[dash] = [_to_basic(int(n), aggregate_info[str(n)]) for n in pr_numbers if str(n) in aggregate_info]

    return aggregate_info, draft_prs, nondraft_prs, CI_status, all_pr_status, base_branch, prs_to_list
