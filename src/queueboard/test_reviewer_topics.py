#!/usr/bin/env python3

"""
Integration test to validate that the repository-root reviewer-topics.json works with
the dashboard data pipeline using the synthetic fixtures in test/newtest/.

Steps:
- Work strictly in a temp directory (copy of test/newtest/) to avoid modifying repo files
- Copy repo-root reviewer-topics.json into the temp dir (so suggest_reviewer reads it)
- Generate a minimal processed_data/assignment_data.json matching that reviewer map
  and the PRs in processed_data/open_pr_data.json in the temp dir
- Run queueboard.dashboard_data to produce API JSON files in the temp dir
- Run queueboard.dashboard to produce HTML; verify key outputs exist

Run via: `python -m queueboard.test_reviewer_topics`
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import shutil
from datetime import datetime, timezone


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _source_dir() -> Path:
    return _repo_root() / "test" / "newtest"


def _load_root_reviewer_topics() -> list[dict]:
    root = _repo_root() / "reviewer-topics.json"
    with root.open("r") as f:
        return json.load(f)


def _copy_root_topics_into(work_dir: Path) -> None:
    topics = _load_root_reviewer_topics()
    dest = work_dir / "reviewer-topics.json"
    dest.write_text(json.dumps(topics, indent=2))


def _generate_assignment_data_from_topics(work: Path) -> None:
    processed = work / "processed_data"
    processed.mkdir(exist_ok=True)

    # Load PRs from synthetic aggregate data
    with (processed / "open_pr_data.json").open("r") as f:
        open_pr_data = json.load(f)
    prs = open_pr_data["pr_statusses"]

    # Build area -> reviewers map from root reviewer-topics.json
    reviewers = _load_root_reviewer_topics()
    area_to_reviewers: dict[str, list[str]] = {}
    all_reviewers: list[str] = []
    for r in reviewers:
        gh = r.get("github_handle")
        if not gh:
            continue
        all_reviewers.append(gh)
        for area in r.get("top_level", []) or []:
            area_to_reviewers.setdefault(area, []).append(gh)

    # Simple assignment heuristic: assign each PR to one reviewer whose area matches
    # a topic label (labels starting with 't-' or 'tech debt'). If no match, skip PR.
    assignments: dict[str, list[dict]] = {}
    assigned_prs: set[int] = set()
    for pr in prs:
        number = pr["number"]
        labels = pr.get("label_names", [])
        topic_labels = [lab for lab in labels if lab.startswith("t-") or lab in ["tech debt"]]
        chosen_reviewer: str | None = None
        for lab in topic_labels:
            reviewers_for_area = area_to_reviewers.get(lab)
            if reviewers_for_area:
                chosen_reviewer = reviewers_for_area[0]
                break
        # Fallback: if no matching area, assign to the first reviewer (if any) so we still exercise paths
        if not chosen_reviewer and all_reviewers:
            chosen_reviewer = all_reviewers[0]
        if chosen_reviewer:
            assignments.setdefault(chosen_reviewer, []).append({"number": number, "state": "open"})
            assigned_prs.add(number)

    # Build header counts consistent with the assignments we just created
    number_all_prs = len(prs)
    number_open_prs = number_all_prs
    number_all_assigned = sum(len(v) for v in assignments.values())
    number_open_assigned = len({item["number"] for v in assignments.values() for item in v if item["state"] == "open"})

    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "number_all_prs": number_all_prs,
        "number_open_prs": number_open_prs,
        "number_all_assigned": number_all_assigned,
        "number_open_assigned": number_open_assigned,
        "all_assignments": assignments,
    }
    with (processed / "assignment_data.json").open("w") as f:
        json.dump(payload, f, indent=2)


def _run_dashboard_pipeline(work: Path) -> None:
    # Run queueboard.dashboard_data then queueboard.dashboard inside work dir
    from queueboard import dashboard_data, dashboard

    cwd0 = Path.cwd()
    try:
        os.chdir(work)
        # Generate API files
        # dashboard_data.read_json_files reads sys.argv for input file list.
        import sys as _sys

        _argv0 = list(_sys.argv)
        try:
            _sys.argv = ["queueboard.dashboard_data", "all-open-PRs-test.json"]
            dashboard_data.main()
        finally:
            _sys.argv = _argv0

        # Generate HTML files from API
        _argv1 = list(_sys.argv)
        try:
            _sys.argv = ["queueboard.dashboard"]
            dashboard.main()
        finally:
            _sys.argv = _argv1
    finally:
        os.chdir(cwd0)


def _assert_outputs(work: Path) -> None:
    # Key HTML pages
    expected_pages = [
        work / "gh-pages" / "index.html",
        work / "gh-pages" / "review_dashboard.html",
        work / "gh-pages" / "maintainers_quick.html",
        work / "gh-pages" / "help_out.html",
    ]
    missing = [str(p) for p in expected_pages if not p.exists()]
    if missing:
        raise SystemExit(f"Missing expected dashboard outputs: {missing}")

    # Copied JSONs
    expected_jsons = [
        work / "gh-pages" / "area_stats.json",
        work / "gh-pages" / "dependency_graph.json",
        work / "gh-pages" / "automatic_assignments.json",
    ]
    missing_jsons = [str(p) for p in expected_jsons if not p.exists()]
    if missing_jsons:
        raise SystemExit(f"Missing expected copied JSON outputs: {missing_jsons}")


def main() -> None:
    # Work in a temp copy of test/newtest to avoid modifying files in the repository
    src = _source_dir()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        # Copy the entire fixture directory tree
        shutil.copytree(src, work, dirs_exist_ok=True)
        # Overwrite the copied reviewer-topics.json with the repository-root version
        _copy_root_topics_into(work)
        # Generate assignment_data.json inside the temp work dir
        _generate_assignment_data_from_topics(work)
        # Run the pipeline entirely within the temp dir
        _run_dashboard_pipeline(work)
        _assert_outputs(work)
        print("test_reviewer_topics: OK — dashboard generated successfully using root reviewer-topics.json (no repo files modified)")


if __name__ == "__main__":
    main()
