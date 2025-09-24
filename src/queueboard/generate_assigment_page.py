#!/usr/bin/env python3

"""
Generate a webpage
- displaying statistics about how many reviewers have how many PR assigned to them,
- suggesting potential reviewers for unassigned PRs, based on their self-indicated areas of competence/interest

"""

import json
import glob
from os import path
from typing import List

from queueboard.ci_status import CIStatus
from queueboard.dashboard_data import determine_pr_dashboards, parse_aggregate_file
from queueboard.dashboard import (
    AggregatePRInfo,
    BasicPRInformation,
    Dashboard,
    ExtraColumnSettings,
    _make_h2,
    _write_table_header,
    _write_table_row,
    tables_configuration_script,
    infer_pr_url,
    pr_link,
    user_link,
    write_dashboard,
    write_webpage,
)

from queueboard.suggest_reviewer import (
  read_reviewer_info,
  suggest_reviewers,
  collect_assignment_statistics,
)

# Assumes the aggregate data is correct: no cross-filling in of placeholder data.
def compute_pr_list_from_aggregate_data_only(aggregate_data: dict[int, AggregatePRInfo]) -> dict[Dashboard, List[BasicPRInformation]]:
    all_open_prs: List[BasicPRInformation] = []
    nondraft_PRs: List[BasicPRInformation] = []
    for number, data in aggregate_data.items():
        if data.state == "open":
            info = BasicPRInformation(number, data.author, data.title, infer_pr_url(number), data.labels, data.last_updated)
            all_open_prs.append(info)
            if not data.is_draft:
                nondraft_PRs.append(info)
    CI_status: dict[int, CIStatus] = dict()
    for pr in nondraft_PRs:
        if pr.number in aggregate_data:
            CI_status[pr.number] = aggregate_data[pr.number].CI_status
        else:
            CI_status[pr.number] = CIStatus.Missing
    base_branch: dict[int, str] = dict()
    for pr in nondraft_PRs:
        base_branch[pr.number] = aggregate_data[pr.number].base_branch
    # TODO: re-instate with reverted meaning
    # prs_from_fork = [pr for pr in nondraft_PRs if aggregate_data[pr.number].head_repo != "leanprover-community"]
    return determine_pr_dashboards(all_open_prs, nondraft_PRs, base_branch, CI_status, aggregate_data, True)


# Copy-pasted from STANDARD_ALIAS_MAPPING, but the indices are different.
# Adjust this script, if there is a logic change we want to mirror/a major column is added.
# NB. Keep this in sync with the ExtraColumnSettings below.
ALIAS_MAPPING = """
  // Return a table column index corresponding to a human-readable alias.
  // |aliasOrIdx| is supposed to be an integer or a string.
  function getIdx (aliasOrIdx) {
    switch (aliasOrIdx) {
      case "number":
        return 0;
      case "author":
        return 1;
      case "title":
        return 2;
      // idx = 3 means the PR description, which is a hidden field
      case "labels":
        return 4;
      case "diff":
        return 5;
      // idx = 6 is the list of modified files
      case "numberChangedFiles":
        return 7;
      case "numberComments":
        return 8;
      // idx = 9 means the handles of users who commented or reviewed this PR

      // the assignee field is skipped; approvals are always shown
      case "approvals":
        return 10;
      // we hide the last update
      case "potentialReviewers":
        return 11;
      case "contact": // amounts to sorting by the suggested reviewer, which might be useful
        return 12;
      case "lastStatusChange":
        return 13;
      case "totalTimeReview":
        return 14;
      default:
        return aliasOrIdx;
    }
  }
"""

def ensure_file(filename):
    """
    Ensure the file exists by joining split parts if necessary.

    Args:
        filename (str): Path to the file (e.g., 'processed_data/all_pr_data.json')

    Returns:
        str: Path to the complete file
    """
    if path.exists(filename):
        return filename

    # Look for split parts (e.g., processed_data/all_pr_data.json.aa, processed_data/all_pr_data.json.ab, etc.)
    parts = sorted(glob.glob(f"{filename}.*"))
    # Filter out directories or other non-file matches if any
    parts = [p for p in parts if path.isfile(p)]

    if not parts:
        raise FileNotFoundError(f"Neither {filename} nor its split parts were found")

    # Join the parts into the original file
    with open(filename, 'wb') as outfile:
        for part in parts:
            with open(part, 'rb') as infile:
                outfile.write(infile.read())

    return filename


def main() -> None:
    with open(ensure_file(path.join("processed_data", "all_pr_data.json")), "r") as fi:
        parsed = parse_aggregate_file(json.load(fi))
    stats = collect_assignment_statistics(parsed)

    title = "  <h1>PR assigment overview</h1>"
    welcome = "<p>This is a hidden page, meant for maintainers: it displays information on which PRs are assigned and suggests appropriate reviewers for unassigned PRs. In the future, it could provide the means to contact them. To prevent spam, for now this page is a bit hidden: it has to be generated locally from a script.</p>"
    updated = stats.timestamp.strftime("%B %d, %Y at %H:%M UTC")
    update = f"<p><small>The data underlying this webpage was last updated on: {updated}</small></p>"

    header = _make_h2("assignment-stats", "PR assignment statistics")
    intro = "The following table contains statistics about all open PRs.<br>"
    stat = (
        f"Overall, <b>{len(stats.assigned_open)}</b> of these <b>{stats.num_open}</b> open PRs (<b>{len(stats.assigned_open)/stats.num_open:.1%}</b>) have at least one assignee. "
        f"Among these, <strong>{stats.number_multiple_assignees}</strong> have more than one assignee. "
        f"We provide the number of all PRs ever assigned as a rough reference—but be very careful with interpreting it: "
         "some reviewers are active for longer than others, and the assignee field is used to <b>greatly varying</b> degree!"
    )
    all_recent = '<a title="number of all assigned PRs">Number of PRs ever assigned</a>'
    # NB. Add an empty column to please the formatting script.
    open_assigned = 'Open assigned PR(s)'
    explanation="Each assigned PR is weighed according to its status: PRs on the queue or with just a merge conflict count fully, PRs awaiting author count 1/(#days in this state + 1), blocked PRs don't count"
    thead = _write_table_header(["User", open_assigned, "Number", f'<a title="{explanation}">Weighted number</a>', all_recent, ""], "    ")
    tbody = ""
    for name, (prs, n_weighted, n_all) in stats.assignments.items():
        formatted_prs = [pr_link(int(pr), infer_pr_url(pr), parsed[pr].title) for pr in prs]
        # FUTURE: add a detailed computation of this count, explanation how this sums up?
        # (If so, make _compute_weight return a number and an explanatory string and use the latter here.)
        tbody += _write_table_row([user_link(name), ", ".join(formatted_prs), str(len(prs)), f"{n_weighted:.1f}", str(n_all), ""], "    ")
    table = f"  <table>\n{thead}{tbody}  </table>"
    stats_section = f"{header}\n{intro}\n{stat}\n{table}"

    header = _make_h2("reviewers", "Mathlib reviewers with areas of interest")
    intro = "The following lists all mathlib reviewers with their (self-declared) topics of interest. (Beware: still need a solution for keep this file in sync with the 'master' data.)"

    parsed_reviewers = read_reviewer_info()
    curr = "Currently assigned PRs"
    # NB. Add an empty column to please the formatting script.
    thead = _write_table_header(["Github username", "Zulip handle", "Topic areas", "Comments", curr, ""], "    ")
    tbody = ""
    for rev in parsed_reviewers:
        if rev.github in stats.assignments:
            (pr_numbers, n_weighted, n_all) = stats.assignments[rev.github]
            numbers = ", ".join([str(n) for n in pr_numbers]) if pr_numbers else "none"
            desc = f'<a title="{n_weighted:.1f} weighed PRs; {n_all} PR(s) ever assigned">{numbers}</a>'
        else:
            desc = "none ever"
        tbody += _write_table_row([user_link(rev.github), rev.zulip, ", ".join(rev.top_level), rev.comment, desc, ""], "    ")
    table = f"  <table>\n{thead}{tbody}  </table>"
    reviewers = f"{header}\n{intro}\n{table}"

    header = _make_h2("propose-reviewers", "Finding reviewers for stale unassigned PRs")
    pr_lists = compute_pr_list_from_aggregate_data_only(parsed)
    suggestions_pre = {
        pr.number: suggest_reviewers(stats.assignments, parsed_reviewers, pr.number, parsed[pr.number], parsed)
        for pr in pr_lists[Dashboard.Queue]
    }
    suggestions = {
      n: (val.code, val.all_potential_reviewers)
      for (n, val) in suggestions_pre.items()
    }
    # Future: have another column with a button to send a zulip DM to a
    # potential (e.g. selecting from the suggested ones).
    settings = ExtraColumnSettings(show_assignee=False, show_approvals=True, potential_reviewers=True, hide_update=True)
    table = write_dashboard("assign-reviewer.html", pr_lists, Dashboard.QueueStaleUnassigned, parsed, settings, False, suggestions, "propose-reviewers")
    propose_stale = f"{header}\n{table}\n"
    # NB. This line becomes actual javascript code, so uses JS' string interpolation syntax.
    # msg = "Dear ${name}, I'm triaging unassigned PRs. #${number} matches your interests; would you like to review it? Thanks!"
    # extra = "  function contactMessage(name, number) {\n    alert(`msg`);\n  }".replace("msg", msg)

    header = _make_h2("propose-reviewers-all", "Finding reviewers for all unassigned PRs")
    table = write_dashboard("assign-reviewer.html", pr_lists, Dashboard.Queue, parsed, settings, False, suggestions, "propose-reviewers-all")
    propose_all = f"{header}\n{table}\n"
    write_webpage(
      f"{title}\n{welcome}\n{update}\n{stats_section}\n{reviewers}\n{propose_all}\n{propose_stale}",
      "assign-reviewer.html", custom_script=tables_configuration_script(ALIAS_MAPPING, "")
    )
    print('Finished generating a PR assignment overview page. Open "assign-reviewer.html" in your browser to view it.')


main()
