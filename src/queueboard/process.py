#!/usr/bin/env python3

"""
This script looks at all files in the `data` directory and creates a JSON file,
containing information about all PRs described in that directory. For each PR,
we list
- whether it's in draft stage (as opposed being marked as "ready for review")
- whether mathlib's CI passes on it
- the branch it is based on (usually "master")
"""

import json
import re
import sys
from datetime import datetime, timezone
from os import listdir, path
from typing import List, Tuple

from queueboard.classify_pr_state import PRStatus
from queueboard.state_evolution import first_time_on_queue, last_status_update, total_queue_time
from queueboard.util import eprint, parse_json_file, relativedelta_tryParse, timedelta_tostr


# Determine a PR's CI status: the return value is one of "pass", "fail", "fail-inessential" and "running".
# (Missing CI data is filtered out before, hence cannot happen.)
# (Queued or waiting also count as running, cancelled CI counts as failing.)
# 'CI_check_nodes' is an array of JSON data of all checks for all the commits.
def determine_ci_status(number, CI_check_nodes: dict) -> str:
    # If an inessential job fails, this is usually spurious (e.g. network issues/github rate limits)
    # or indicates a general problem with CI, and not this PR.
    # (Except when this PR modifies CI, of course: currently, we don't check for this.)
    inessential_jobs = [
        "label-new-contributor",
        "label-and-report-new-contributor",
        "New Contributor Check",
        "Add topic label",
        "update-label",
        "apply_one_t_label",
        "Add closed-pr emoji in Zulip",
        "set_pr_emoji",
        "zulip-emoji-merged",
        "Add ready-to-merge label",
        "Add delegated label",
        "Add ready-to-merge or delegated label",
        "Ping maintainers on Zulip",
        # This was an old name for the "Post or update summary comment" job, which has since been given a name.
        # Recall that this check looks at names of CI *jobs*, not *workflow steps*.
        "post-or-update-summary-comment",
        "build",
        "Cross off linked issues",
        "Post summary of benchmarking results",
    ]
    # We consider CI to be passing if no job fails, and every job succeeds or is skipped.
    # If no job fails, but some are still running, we return "running".
    # If some job fails, we check if this is an inessential job or not.
    in_progress = False
    inessential_failure = False
    for r in CI_check_nodes:
        # Ignore bors runs: these don't contain status information (and are not interesting for us).
        if "context" in r:
            continue
        if "label" in r["name"] and r["name"] not in inessential_jobs:
            print(f"info: job name {r['name']} contains the word label, but is not listed as inessential")
        if r["conclusion"] in ["FAILURE", "CANCELLED"]:
            if r["name"] not in inessential_jobs:
                return "fail"
            inessential_failure = True
        elif r["conclusion"] in ["SUCCESS", "SKIPPED", "NEUTRAL"]:
            continue
        elif r["conclusion"] is None and r["status"] in ["IN_PROGRESS", "QUEUED"]:
            in_progress = True
        else:
            print(f'CI run "{r["name"]}" for PR {number} has interesting data: {r}"')
    if inessential_failure:
        return "fail-inessential"
    elif in_progress:
        return "running"
    else:
        return "pass"


# Compute information about this PR's real status changes, using the code in state_evolution.py.
# `is_incomplete` is True if the PR's data is known to be incomplete.
# `CI_status` describes a PR's CI status (in the same format as `determine_CI_status`), or is None for missing data.
# Return a tuple of three dictionaries, describing
# - the first time a given PR was on the review queue,
# - the last time a PR's status changed
# - the total time a PR was on the review queue.
# Each dictionary contains its answer status (which can be "missing", "incomplete" or "valid")
# and (if data is present) the computed value.
def _compute_status_change_data(
    pr_data: dict, CI_status: str | None, number: int, is_incomplete: bool
) -> Tuple[dict, dict, dict]:
    # These particular PRs have one label noted as removed several times in a row.
    # This trips up my algorithm. Omit the analysis for now. FIXME: make smarter?
    bad_prs = [
        10655,
        10823,
        10878,
        11703,
        11711,
        11385,
        11874,
        12076,
        12268,
        12311,
        12371,
        12435,
        12488,
        12561,
        13149,
        13248,
        13270,
        13273,
        13697,
        14008,
        14065,
        13089,  # TODO: investigate more closely!
        3200,
        6595,
        9526,
        9273,
        12032,
        24769,
        25712,
        25753,
        25922,
        26004,
    ]
    if number in bad_prs:
        missing = {"status": "missing"}
        return (missing, missing, missing)
    # print(f"trace: computing state changes for PR {number}")

    # PRs with "missing" status are the ones above; basic PRs omit this field.
    validity_status = "incomplete" if is_incomplete else "valid"
    # Match the format produced by github, and expected by all the code.
    # Produces output like "2024-07-15T21:08:42Z".
    time_format = "%Y-%m-%dT%H:%M:%SZ"

    # I *could* cache the computed metadata (through _parse_data),
    # computing it only once and not thrice per PR. However,
    # this does not seem to make a big performance difference.
    first_on_queue = first_time_on_queue(pr_data)
    stringified = None if first_on_queue is None else datetime.strftime(first_on_queue, time_format)
    res_first_on_queue = {"status": validity_status, "date": stringified}
    (time, delta, current_status) = last_status_update(pr_data)
    # XXX: as long as the overall status classification does not take CI status into account
    # (and doing so is difficult in general!), we must take care to not simply use the last
    # computed status, but override that when PR CI is failing.
    if CI_status is not None:
        if CI_status in ["fail", "fail-inessential", "running"]:
            current_status = PRStatus.NotReady
    assert relativedelta_tryParse(repr(delta)) == delta
    res_last_status_change = {
        "status": validity_status,
        "time": datetime.strftime(time, time_format),
        "delta": repr(delta),
        "current_status": PRStatus.to_str(current_status),
    }
    ((value_td, value_rd), explanation) = total_queue_time(pr_data)
    assert relativedelta_tryParse(repr(value_rd)) == value_rd
    res_total_queue_time = {
        "status": validity_status,
        "value_td": timedelta_tostr(value_td),
        "value_rd": repr(value_rd),
        "explanation": explanation,
    }
    return (res_first_on_queue, res_last_status_change, res_total_queue_time)


# Extract the github handle of every user who commented on (or reviewed) a given PR.
# Return a tuple (is_incomplete, users), where
# the former is true iff the list of comments or review comments is probably incomplete,
# the second component is the list of user names.
def _compute_commenter_data(pr_data: dict) -> Tuple[bool, List[str]]:
    inner = pr_data["data"]["repository"]["pullRequest"]
    users = set()
    comments = inner["comments"]["nodes"]
    for comment in comments:
        users.add(comment["author"]["login"])
    reviews = inner["reviews"]["nodes"]
    for review in reviews:
        users.add(review["author"]["login"])
    is_incomplete = len(comments) == 100 or len(reviews) == 100
    return (is_incomplete, sorted(list(users)))


def parse_direct_dependencies(description: str) -> List[int]:
    """Parse dependency information from a PR description.

    Extracts PR numbers from lines like:
    - [x] depends on: #24880 [optional extra text]
    - [ ] depends on: #24881 [optional extra text]

    Returns a list of PR numbers as integers.
    """
    if not description:
        return []
    # Pattern matches both checked [x] and unchecked [ ] dependencies
    # Captures the PR number after the #. Allows both content before and after the pattern.
    pattern = r"- \[[ x]\] depends on: #(\d+)"
    matches = re.findall(pattern, description, re.IGNORECASE)
    # Remove duplicate numbers; sort for determinism.
    return sorted(list(set([int(pr_num) for pr_num in matches])))


def test_deps_parsing():
    def check(input: str, expected: List[int]) -> None:
        actual = parse_direct_dependencies(input)
        aux = input.replace("\n", "\n  ")
        assert expected == actual, f"expected direct dependencies {expected} from description\n  {aux}\ngot {actual}"

    check("", [])
    check("Some PR description without dependencies", [])
    check("- [ ] depends on: #12", [12])
    check("Some PR description\n- [x] depends on: #21", [21])
    check("Some PR description\n- [x] depends on: #21\nSome furtherPR description\n- [x] depends on: #18", [18, 21])
    check("Some PR description\n- [ ] depends on: #24880 [optional extra text]", [24880])
    check("Some PR description\n- [x] depends on: #35000 [optional extra text]", [35000])
    # Specifying the same PR number twice is only recorded once
    check("Some PR description\n- [ ] depends on: #37\n\n- [x] depends on: #37", [37])
    # Dependencies without a checkbox are not recognised as such.
    # XXX: audit all deps for such descriptions
    check("- depends on: #12", [])
    check("- [ ] depends on #12", [])
    # Extra indentation in the dependency line does not matter. Extra things at the line beginning neither.
    check("Some PR description\n   - [x] depends on: #21", [21])
    check("Some PR description and now a dep. line- [x] depends on: #21", [21])

    # Specifying PRs in other repos is not recognised.
    check("Some PR description\n- [x] depends on: leanprover/lean4#21", [])
    # Putting something bogus is not recognised either.
    check("Some PR description\n- [ ] depends on: #xyz with lots of additional text", [])
    # The standard PR template does not yield dependent PRs.
    std = "- [ ] depends on: #abc [optional extra text]\r\n- [ ] depends on: #xyz [optional extra text]\r\n"
    check(std, [])
    # NB. We currently don't check if a PR number is inside the HTML comment.
    check("<!--\n- [ ] depends on: #123 ->", [123])


def get_aggregate_data(pr_data: dict, only_basic_info: bool) -> dict:
    inner = pr_data["data"]["repository"]["pullRequest"]
    number = inner["number"]
    branch_name = inner["headRefName"]
    head_repo = inner["headRepositoryOwner"]
    base_branch = inner["baseRefName"]
    is_draft = inner["isDraft"]
    state = inner["state"].lower()
    last_updated = inner["updatedAt"]
    # We assume the author URL is determined by the github handle: in practice, it is.
    author = inner["author"]["login"]
    title = inner["title"]
    description = inner["body"]
    additions = inner["additions"]
    deletions = inner["deletions"]
    # Number of files modified by this PR.
    number_modified_files = inner["changedFiles"]
    modified_files = [n["path"] for n in inner["files"]["nodes"]]
    # Names of all labels applied to this PR: missing the background colour!
    labels = [lab["name"] for lab in inner["labels"]["nodes"]]
    assignees = [ass["login"] for ass in inner["assignees"]["nodes"]]
    # Get information about the latest CI run; `None` if that information seems missing.
    # For closed PRs, missing information is fine, however: do not warn there.
    if inner["statusCheckRollup"] is None:
        if state == "open":
            print(f'warning: PR {number} has missing information ("null") for CI status checks')
        CI_status = None
    else:
        CI_status = determine_ci_status(number, inner["statusCheckRollup"]["contexts"]["nodes"])
    # github usernames of everyone who left an "approving" review on this PR.
    approvals = []
    for r in inner["reviews"]["nodes"]:
        if r["state"] == "APPROVED":
            approvals.append(r["author"]["login"])
    number_comments = len(inner["comments"]["nodes"])
    (is_incomplete, commenters) = _compute_commenter_data(pr_data)
    # NB. When adding future fields, pay attention to whether the 'basic' info files
    # also contain this information --- otherwise, it is fine to omit it!
    aggregate_data = {
        "number": number,
        "is_draft": is_draft,
        "CI_status": CI_status,
        "head_repo": head_repo,
        "base_branch": base_branch,
        "branch_name": branch_name,
        "state": state,
        "last_updated": last_updated,
        "author": author,
        "title": title,
        "description": description,
        # TODO: add tests for this methods and check that it returns sane results
        # The numbers of the PRs which this PR directly depends on.
        # This is automatically extracted from the PR description,
        # hence only as good as the description.
        "direct_dependencies": parse_direct_dependencies(description),
        # TODO: compute transitive dependencies and include in this data
        "label_names": labels,
        "additions": additions,
        "deletions": deletions,
        "num_files": number_modified_files,
        "files": modified_files,
        "number_comments": number_comments,
        "commenters": {
            "status": "incomplete" if is_incomplete else "valid",
            "users": commenters,
        },
        "assignees": assignees,
        "review_approvals": approvals,
    }
    if not only_basic_info:
        number_review_comments = 0
        review_threads = inner["reviewThreads"]["nodes"]
        for t in review_threads:
            number_review_comments += len(t["comments"]["nodes"])
        aggregate_data["number_review_comments"] = number_review_comments
        num_events = len(inner["timelineItems"]["nodes"])
        # events_not_commit = [n for n in inner["timelineItems"]["nodes"]
        #    if "__typename" not in n or n["__typename"] != "PullRequestCommit"]

        # All these PRs have (sometimes far) more than commits or 250 events, so re-downloading their
        # data now would not help: do not print information about them.
        do_not_redownload = [
            6057,
            6277,
            6468,
            7849,
            8585,
            9013,
            # 13611 has ~240 commits, so the events data is also incomplete
            10235,
            10383,
            11465,
            11466,
            13194,
            13429,
            13611,
            13905,
            16152,
            16316,
            16351,
            17518,
            17519,
            17715,
            18672,
            20768,
            15564,
            15746,
            15748,
            15749,
            15978,
            15981,
            16077,
            16080,
            16112,
            16148,
            16313,
            16362,
            16375,
            16534,
            16535,
            16657,
            17132,
            17240,
            19706,
            # adaptation PRs or benchmarking
            8076,
            15181,
            15358,
            15503,
            15788,
            15827,
            16163,
            16244,
            16425,
            16669,
            16716,
            17058,
            17374,
            17532,
            18007,
            18421,
            18830,
            19494,
            19984,
            20392,
            20402,
        ]
        # Until cursor handling has been implemented, these warnings do not add helpful information.
        # if num_events == 250 and len(events_not_commit) == 0:
        #     print(f"process.py: {state} PR {number} has exactly 250 events, all of which are commits: probably this data is incomplete!", file=sys.stderr)
        # elif num_events == 250:
        #     print(f"process.py: {state} PR {number} has exactly 250 events: probably this data is incomplete, please double-check!", file=sys.stderr)
        num_commits = len(inner["commits"]["nodes"])
        if num_commits == 100 and state == "open":
            if number not in do_not_redownload:
                print(
                    f"process.py: {state} PR {number} has exactly 100 commits; please double-check if this data is complete",
                    file=sys.stderr,
                )

        # Compute information about this PR's real status changes, using the code in state_evolution.py.
        (res_first_on_queue, res_last_status_change, res_total_queue_time) = _compute_status_change_data(
            pr_data, CI_status, number, num_events == 250
        )
        aggregate_data["first_on_queue"] = res_first_on_queue
        aggregate_data["last_status_change"] = res_last_status_change
        aggregate_data["total_queue_time"] = res_total_queue_time
    return aggregate_data


# For each open PR with the "infinity-cosmos" label, record its last update
# (according to github), its current state and its last real status change.
def compute_infinity_cosmos_data(now: str, all_open_pr_items: dict) -> dict:
    prs = []
    for pr in all_open_pr_items:
        if "infinity-cosmos" in pr["label_names"]:
            real = pr.get("last_status_change")
            if real is None or real["status"] != "valid":
                prs.append(
                    {
                        "number": pr["number"],
                        "last_updated": pr["last_updated"],
                        "last_status_change": None,
                        "current_status": None,
                    }
                )
            else:
                prs.append(
                    {
                        "number": pr["number"],
                        "last_updated": pr["last_updated"],
                        "last_status_change": real["time"],
                        "current_status": real["current_status"],
                    }
                )
    return {"timestamp": now, "prs": prs}


def main() -> None:
    fast = False
    if len(sys.argv) == 2:
        if sys.argv[1] == "--fast":
            fast = True
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    label_colours: dict[str, str] = dict()
    all_pr_data: List[dict] = []
    # A few files are known to have broken detailed information.
    # They can be found in the file "stubborn_prs.txt".
    known_erronerous: List[str] = []
    with open("stubborn_prs.txt", "r") as error_prs:
        for line in error_prs:
            if not line.startswith("--"):
                known_erronerous.append(line.rstrip())
    # Read all pr info files in the data directory.
    pr_dirs: List[str] = sorted(listdir("data"))
    for pr_dir in pr_dirs:
        only_basic_info = "basic" in pr_dir
        pr_number = pr_dir.removesuffix("-basic")
        filename = (
            path.join("data", pr_dir, "basic_pr_info.json") if only_basic_info else path.join("data", pr_dir, "pr_info.json")
        )
        match parse_json_file(filename, pr_number):
            case str(err):
                if pr_number not in known_erronerous:
                    print(f"attention: found an unexpected error!\n  {err}", file=sys.stderr)
            case dict(data):
                if (pr_number in known_erronerous) and not only_basic_info:
                    print(
                        f"warning: PR {pr_number} has fine data, but is listed as erronerous: please remove it from that list",
                        file=sys.stderr,
                    )
                label_data = data["data"]["repository"]["pullRequest"]["labels"]["nodes"]
                for lab in label_data:
                    if "color" in lab:
                        (name, colour) = (lab["name"], lab["color"])
                        if name in label_colours and colour != label_colours[name]:
                            eprint(f"warning: label {name} is assigned colours {colour} and {label_colours[name]}")
                        else:
                            label_colours[name] = colour
                if (not fast) or data["data"]["repository"]["pullRequest"]["state"] == "OPEN":
                    all_pr_data.append(get_aggregate_data(data, only_basic_info))
    if not fast:
        all_prs = {
            "timestamp": updated,
            "label_colours": dict(sorted(label_colours.items())),
            "pr_statusses": all_pr_data,
        }
    just_open_prs = {
        "timestamp": updated,
        "label_colours": dict(sorted(label_colours.items())),
        "pr_statusses": [item for item in all_pr_data if item["state"] == "open"],
    }

    # Mapping of github handles 'X', to a list of pairs `(number, state)`,
    # where `number` is a PR number assigned to `X`,
    # and `state` is the state of that PR (open/closed).
    # This contains *all* assigned PRs.
    all_assignments = {}
    assigned_prs = filter(lambda pr: pr["assignees"], all_pr_data)
    for pr in assigned_prs:
        val = {"number": pr["number"], "state": pr["state"]}
        for name in pr["assignees"]:
            if name not in all_assignments:
                all_assignments[name] = [val]
            else:
                mapping = all_assignments[name]
                mapping.append(val)
                all_assignments[name] = mapping

    # Gather statistics about open and assigned PRs.
    # A PR assigned to multiple reviewers is only counted once.
    num_all_assigned = 0
    num_open_assigned = 0
    for pr in all_pr_data:
        if pr["assignees"]:
            num_all_assigned += 1
            if pr["state"] == "open":
                num_open_assigned += 1
    assignment_data = {
        "timestamp": updated,
        "number_all_prs": len(all_pr_data),
        "number_open_prs": len(just_open_prs["pr_statusses"]),
        "number_all_assigned": num_all_assigned,
        "number_open_assigned": num_open_assigned,
        "all_assignments": all_assignments,
    }

    infty_cosmos_data = compute_infinity_cosmos_data(updated, just_open_prs["pr_statusses"])

    if not fast:
        with open(path.join("processed_data", "all_pr_data.json"), "w") as f:
            print(json.dumps(all_prs, indent=4), file=f)
    with open(path.join("processed_data", "open_pr_data.json"), "w") as f:
        print(json.dumps(just_open_prs, indent=4), file=f)
    with open(path.join("processed_data", "assignment_data.json"), "w") as f:
        print(json.dumps(assignment_data, indent=4), file=f)
    with open(path.join("processed_data", "infinity_cosmos_data.json"), "w") as f:
        print(json.dumps(infty_cosmos_data, indent=4), file=f)


main()
