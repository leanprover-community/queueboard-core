import json
from os import path, makedirs
import sys
from random import shuffle
from typing import List, NamedTuple, Dict
from ci_status import CIStatus
from mathlib_dashboards import Dashboard
from compute_dashboard_prs import (AggregatePRInfo, BasicPRInformation,
    PLACEHOLDER_AGGREGATE_INFO, compute_pr_statusses, determine_pr_dashboards, parse_aggregate_file, _extract_prs)

### Reading the input files passed to this script ###

# Information passed to this script, via various JSON files.
class JSONInputData(NamedTuple):
    # All aggregate information stored for every open PR.
    aggregate_info: dict[int, AggregatePRInfo]
    # Information about all open PRs
    all_open_prs: List[BasicPRInformation]


# Validate the command-line arguments and try to read all data passed in via JSON files.
# Any number of JSON files passed in is fine; we interpret them all as containing open PRs.
def read_json_files() -> JSONInputData:
    if len(sys.argv) == 1:
        print("error: need to pass in some JSON files with open PRs")
        sys.exit(1)
    all_open_prs = []
    for i in range(1, len(sys.argv)):
        with open(sys.argv[i]) as prfile:
            open_prs = _extract_prs(json.load(prfile))
            if len(open_prs) >= 990:
                print(f"error: file {sys.argv[i]} contains at least 990 PRs: the REST API will never return more than 1000 PRs. "
                "Please split the list into more files as necessary. Erroring now as this means incomplete data (now or very soon).", file=sys.stderr)
                sys.exit(1)
            elif len(open_prs) >= 900:
                print(f"warning: file {sys.argv[i]} contains at least 900 PRs: the REST API will never return more than 1000 PRs. Please split the list into more files as necessary.", file=sys.stderr)
            all_open_prs.extend(open_prs)
    with open(path.join("processed_data", "open_pr_data.json"), "r") as f:
        aggregate_info = parse_aggregate_file(json.load(f))
    return JSONInputData(aggregate_info, all_open_prs)

# TODO: this code is AI-generated and has not been fully reviewed yet.
# TODO: move this analysis to process.py, and run it at data aggregation time?
# Or will this become too slow, and it is better to only do so for all open PRs?
def generate_dependency_graph(aggregate_info: Dict[int, AggregatePRInfo]) -> Dict:
    """Generate dependency graph data in D3.js compatible format from aggregate PR information."""
    nodes = []
    links = []

    # Build dependency information for all PRs
    pr_dependencies = {}
    pr_dependents = {}

    for pr_number, pr_info in aggregate_info.items():
        direct_deps = pr_info.direct_dependencies
        # Filter to only include dependencies on actual, open PRs.
        direct_deps = [dep for dep in direct_deps if dep in aggregate_info]

        pr_dependencies[pr_number] = direct_deps
        pr_dependents[pr_number] = []

    # Build reverse dependencies
    for pr_number, direct_deps in pr_dependencies.items():
        for dep_pr in direct_deps:
            if dep_pr in pr_dependents:
                pr_dependents[dep_pr].append(pr_number)

    # Create nodes
    for pr_number, pr_info in aggregate_info.items():
        pr_url = f"https://github.com/leanprover-community/mathlib4/pull/{pr_number}"

        # Check for draft status in labels
        is_draft = pr_info.is_draft or any(label.name.lower() in ['wip', 'draft'] for label in pr_info.labels)

        nodes.append({
            "id": pr_number,
            "title": pr_info.title,
            "author": pr_info.author,
            "state": pr_info.state.lower(),
            "is_draft": is_draft,
            "labels": [label.name for label in pr_info.labels],
            "url": pr_url,
            "dependency_count": len(pr_dependencies.get(pr_number, [])),
            "dependent_count": len(pr_dependents.get(pr_number, [])),
            "additions": pr_info.additions,
            "deletions": pr_info.deletions
        })

    # Create links
    for pr_number, direct_deps in pr_dependencies.items():
        for dep_pr in direct_deps:
            if dep_pr in aggregate_info:
                links.append({
                    "source": pr_number,
                    "target": dep_pr,
                    "source_state": aggregate_info[pr_number].state.lower(),
                    "target_state": aggregate_info[dep_pr].state.lower()
                })

    return {
        "nodes": nodes,
        "links": links,
        "metadata": {
            "total_prs": len(aggregate_info),
            "prs_with_dependencies": len([deps for deps in pr_dependencies.values() if deps]),
            "prs_that_are_dependencies": len([deps for deps in pr_dependents.values() if deps]),
            "dependency_links": len(links)
        }
    }

def main() -> None:
    # intermediate "API" files will go in the api directory
    makedirs('api')

    input_data = read_json_files()
    # Populate basic information from the input data: splitting into draft and non-draft PRs
    # (mostly, we only use the latter); extract separate dictionaries for CI status and base branch.

    # NB. We handle missing metadata by adding "default" values for its aggregate data
    # (ready for review, open, against master, failing CI and just updated now).
    aggregate_info = input_data.aggregate_info.copy()

    for pr in input_data.all_open_prs:
        if pr.number not in input_data.aggregate_info:
            print(f"warning: found no aggregate information for PR {pr.number}; filling in defaults", file=sys.stderr)
            aggregate_info[pr.number] = PLACEHOLDER_AGGREGATE_INFO
    with open(path.join("api", "aggregate_info.json"), "w") as f:
        json.dump(aggregate_info, f)

    draft_PRs = [pr for pr in input_data.all_open_prs if aggregate_info[pr.number].is_draft]
    with open(path.join("api", "draft_PRs.json"), "w") as f:
        json.dump(draft_PRs, f)
    nondraft_PRs = [pr for pr in input_data.all_open_prs if not aggregate_info[pr.number].is_draft]
    with open(path.join("api", "nondraft_PRs.json"), "w") as f:
        json.dump(nondraft_PRs, f)

    # The only exception is for the "on the queue" page,
    # which points out missing information explicitly, hence is passed the non-filled in data.
    CI_status: dict[int, CIStatus] = dict()
    for pr in nondraft_PRs:
        if pr.number in input_data.aggregate_info:
            CI_status[pr.number] = input_data.aggregate_info[pr.number].CI_status
        else:
            CI_status[pr.number] = CIStatus.Missing
    with open(path.join("api", "CI_status.json"), "w") as f:
        json.dump(CI_status, f)

    base_branch: dict[int, str] = dict()
    for pr in nondraft_PRs:
        base_branch[pr.number] = aggregate_info[pr.number].base_branch
    with open(path.join("api", "base_branch.json"), "w") as f:
        json.dump(base_branch, f)

    # TODO(August/September): re-instate this check and invert it
    # prs_from_fork = [pr for pr in nondraft_PRs if aggregate_info[pr.number].head_repo != "leanprover-community"]
    all_pr_status = compute_pr_statusses(aggregate_info, input_data.all_open_prs)
    with open(path.join("api", "all_pr_status.json"), "w") as f:
        json.dump(all_pr_status, f)

    # TODO: try to enable |use_aggregate_queue| 'queue_prs' again, once all the root causes
    # for PRs getting 'dropped' by 'gather_stats.sh' are found and fixed.
    prs_to_list = determine_pr_dashboards(input_data.all_open_prs, nondraft_PRs, base_branch, CI_status, aggregate_info, False)
    with open(path.join("api", "prs_to_list.json"), "w") as f:
        json.dump(prs_to_list, f)

    # As a final feature, we propose a reviewer for 50 (randomly drawn) stale unassigned pull requests,
    # and write this information to "automatic_assignments.json".
    # NB. These 50 PRs include any PRs without any suggested reviewer (say, because an area has not enough
    # reviewers or everybody is too busy) --- so in practice, fewer reviewers may be actually assigned.
    # XXX: importing this at the beginning leads to a circular import; importing it here seems to work.
    from suggest_reviewer import read_reviewer_info, collect_assignment_statistics, suggest_reviewers_many
    reviewer_info = read_reviewer_info()
    assignment_stats = collect_assignment_statistics(aggregate_info)
    all_stale_unassigned : List[int] = [pr.number for pr in prs_to_list[Dashboard.QueueStaleUnassigned]]
    shuffle(all_stale_unassigned)
    try:
        with open("outdated_prs.txt", "r") as fi:
            lines = fi.readlines()
    except FileNotFoundError:
        lines = []
    outdated_prs = [int(s) for s in lines if s]
    to_analyze = [pr for pr in all_stale_unassigned if pr not in outdated_prs]
    proposed_reviews = suggest_reviewers_many(assignment_stats.assignments, reviewer_info, sorted(to_analyze[0:50]), aggregate_info)
    with open("automatic_assignments.json", "w") as fi:
        print(json.dumps(proposed_reviews, indent=4), file=fi)

    # Generate dependency graph for the dependency dashboard
    dependency_graph_data = generate_dependency_graph(aggregate_info)
    with open("dependency_graph.json", "w") as f:
        json.dump(dependency_graph_data, f, indent=2)

    print(f"Generated dependency graph with {dependency_graph_data['metadata']['dependency_links']} links between {dependency_graph_data['metadata']['total_prs']} PRs")



if __name__ == "__main__":
    main()
