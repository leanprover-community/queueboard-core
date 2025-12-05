#!/usr/bin/env python3

"""
Smoke test to validate the new snapshot.json output against legacy API files using the
synthetic fixtures in test/newtest/.

Run via: `python -m queueboard.test_snapshot`
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from queueboard import dashboard_data
from queueboard.ci_status import CIStatus
from queueboard.classify_pr_state import PRStatus
from queueboard.compute_dashboard_prs import BasicPRInformation, load_from_json_file
from queueboard.mathlib_dashboards import Dashboard


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _source_dir() -> Path:
    return _repo_root() / "test" / "newtest"


def _run_dashboard_data(work: Path) -> None:
    cwd0 = Path.cwd()
    try:
        os.chdir(work)
        import sys as _sys

        argv0 = list(_sys.argv)
        try:
            _sys.argv = ["queueboard.dashboard_data", "all-open-PRs-test.json"]
            dashboard_data.main()
        finally:
            _sys.argv = argv0
    finally:
        os.chdir(cwd0)


def _load_snapshot(work: Path) -> dict:
    with (work / "api" / "snapshot.json").open("r") as f:
        return json.load(f)


def _load_ci_status(work: Path) -> dict[str, str]:
    data = load_from_json_file(str(work / "api" / "CI_status.json"))
    return {str(k): (v.value if isinstance(v, CIStatus) else v) for k, v in data.items()}


def _load_base_branch(work: Path) -> dict[str, str]:
    data = load_from_json_file(str(work / "api" / "base_branch.json"))
    return {str(k): v for k, v in data.items()}


def _load_pr_status(work: Path) -> dict[str, str]:
    data = load_from_json_file(str(work / "api" / "all_pr_status.json"))
    return {str(k): (v.value if isinstance(v, PRStatus) else v) for k, v in data.items()}


def _load_prs_to_list(work: Path) -> dict[str, list[int]]:
    data = load_from_json_file(str(work / "api" / "prs_to_list.json"))
    dashboards: dict[str, list[int]] = {}
    for dash, prs in data.items():
        key = dash.name if isinstance(dash, Dashboard) else str(dash)
        dashboards[key] = [pr.number for pr in prs]
    return dashboards


def _load_pr_numbers_from_basic(prs: list[BasicPRInformation]) -> list[int]:
    return [int(pr.number) for pr in prs]


def _assert_snapshot(snapshot: dict, work: Path) -> None:
    assert snapshot["meta"]["schema_version"] == "v1-draft", "unexpected schema_version"

    ci_status = _load_ci_status(work)
    base_branch = _load_base_branch(work)
    pr_status = _load_pr_status(work)
    dashboards = _load_prs_to_list(work)
    draft_prs = load_from_json_file(str(work / "api" / "draft_PRs.json"))
    nondraft_prs = load_from_json_file(str(work / "api" / "nondraft_PRs.json"))

    snapshot_prs = snapshot["prs"]
    # Compare per-PR fields that overlap with legacy maps
    for pr_number_str, payload in snapshot_prs.items():
        if pr_number_str in ci_status:
            assert payload["ci_status"] == ci_status[pr_number_str], f"CI mismatch for PR {pr_number_str}"
        if pr_number_str in pr_status:
            assert payload["pr_status"] == pr_status[pr_number_str], f"Status mismatch for PR {pr_number_str}"
        assert payload["base_branch"] == base_branch.get(pr_number_str, payload["base_branch"]), (
            f"Base branch mismatch for PR {pr_number_str}"
        )

    # Ensure key CI statuses are present in the snapshot
    ci_values = set(payload["ci_status"] for payload in snapshot_prs.values())
    assert {"pass", "fail", "fail-inessential", "running", "missing"} <= ci_values, "Missing CI status variants"

    # Draft / non-draft partitions
    expected_drafts = set(_load_pr_numbers_from_basic(draft_prs))
    expected_nondrafts = set(_load_pr_numbers_from_basic(nondraft_prs))
    assert set(int(n) for n in snapshot["lists"]["draft_prs"]) == expected_drafts, "Draft PR list mismatch"
    assert set(int(n) for n in snapshot["lists"]["nondraft_prs"]) == expected_nondrafts, "Nondraft PR list mismatch"

    # Dashboard partitions
    for dash_name, expected_prs in dashboards.items():
        snap_prs = snapshot["lists"]["dashboards"].get(dash_name)
        assert snap_prs is not None, f"Missing dashboard {dash_name} in snapshot"
        assert sorted(snap_prs) == sorted(expected_prs), f"Dashboard {dash_name} mismatch"
    # Spot-check a few key dashboards we rely on
    for required_dash in ["Queue", "QueueEasy", "QueueNewContributor", "TechDebt", "NeedsDecision"]:
        assert required_dash in snapshot["lists"]["dashboards"], f"Expected dashboard {required_dash} in snapshot"


def main() -> None:
    src = _source_dir()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(src, work, dirs_exist_ok=True)
        _run_dashboard_data(work)
        snapshot = _load_snapshot(work)
        _assert_snapshot(snapshot, work)
        print("test_snapshot: OK — snapshot.json matches legacy API outputs for test/newtest fixtures")


if __name__ == "__main__":
    main()
