#!/usr/bin/env python3

# This script accepts json files as command line arguments and displays the data in an HTML dashboard.
# It assumes that for each PR N which should appear in some dashboard,
# there is a file N.json in the `data` directory, which contains all necessary detailed information about that PR.

import sys
from datetime import datetime, timedelta, timezone
from os import path
from typing import List, NamedTuple, Tuple

from dateutil import parser, relativedelta, tz

from queueboard.ci_status import CIStatus
from queueboard.classify_pr_state import PRStatus
from queueboard.compute_dashboard_prs import (AggregatePRInfo, BasicPRInformation, Label, DataStatus, load_from_json_file,
    infer_pr_url, link_to, gather_pr_statistics)
from queueboard.mathlib_dashboards import Dashboard, short_description, long_description, getIdTitle, getTableId
from queueboard.util import format_delta


### Helper methods: writing HTML code for various parts of the generated webpage ###

# Determine HTML code for writing a table header with entries |entries|.
# |base_indent| is the indentation of the <table> tag; we add two additional space per additional level.
def _write_table_header(entries: List[str], base_indent: str) -> str:
    indent = base_indent + "  "
    body = f"\n{indent}".join([f"<th>{entry}</th>" for entry in entries])
    return f"{base_indent}<thead>\n{base_indent}<tr>\n{base_indent}{body}\n{base_indent}</tr>\n{base_indent}</thead>\n"


# Determine HTML code for writing a single table row with entries 'entries' and indentation 'indent'.
# four spaces for the table itself are hard-coded, for now
def _write_table_row(entries: List[str], base_indent: str) -> str:
    indent = base_indent + "  "
    body = f"\n{indent}".join([f"<td>{entry}</td>" for entry in entries])
    return f"{base_indent}<tr>\n{indent}{body}\n{base_indent}</tr>\n"


# |page| is the name of the current webpage (e.g. "review_dashboard.html"),
# |id| is the fragment ID of the current table (e.g. "queue").
def _write_labels(labels: List[Label], page: str, id: str) -> str:
    if len(labels) == 0:
        return ""
    elif len(labels) == 1:
        return label_link(labels[0], page, id)
    else:
        label_part = "\n        ".join(label_link(label, page, id) for label in labels)
        return f"\n        {label_part}\n      "


# Write the code for a h2 heading linking to itself, with id |id|, title |title|,
# and optional tooltip |tooltip| (implemented as an <a title> attribute).
def _make_h2(id: str, title: str, tooltip=None) -> str:
    if tooltip:
        return f'<h2 id="{id}"><a href="#{id}" title="{tooltip}">{title}</a></h2>'
    return f'<h2 id="{id}"><a href="#{id}">{title}</a></h2>'


# An HTML link to a mathlib PR from the PR number
def pr_link(number: int, url: str, title=None) -> str:
    # The PR number is intentionally not prefixed with a #, so it is correctly
    # recognised and sorted as a number (with HTML formatting, a `html-num` type),
    # and not sorted as a string.
    return f"<a href='{url}' title='{title or ''}'>{number}</a>"


# Create a link to the set of all PRs from the current dashboard from this user.
# |page| is the name of the current webpage (e.g. "review_dashboard.html"),
# |id| is the fragment ID of the current table (e.g. "queue").
def user_filter_link(author_name: str, page: str, id: str) -> str:
    # Future: should this *add* to current search terms, instead of replacing them?
    id = f"#{id}" if id else ""
    return f"<a href='{page}?search={author_name}{id}'>{author_name}</a>"

# An HTML link to a GitHub user profile
def user_link(author_name: str, details: str | None = None) -> str:
    url = f"https://github.com/{author_name}"
    title = f" title='{details}'" if details else ""
    return f"<a href='{url}'{title}>{author_name}</a>"


# An HTML link to a mathlib PR from the PR title
def title_link(title: str, url: str) -> str:
    return f"<a href='{url}'>{title}</a>"


# Create a button showing this label's title, and linking to the set of
# all PRs from the current dashboard which have this label.
# |page| is the name of the current webpage (e.g. "review_dashboard.html"),
# |id| is the fragment ID of the current table (e.g. "queue").
def label_link(label: Label, page: str, id: str) -> str:
    # Function to check if the colour of the label is light or dark
    # adapted from https://codepen.io/WebSeed/pen/pvgqEq
    # r, g and b are integers between 0 and 255.
    def isLight(r: int, g: int, b: int) -> bool:
        # Counting the perceptive luminance
        # human eye favors green color...
        a = 1 - (0.299 * r + 0.587 * g + 0.114 * b) / 255
        return a < 0.5

    # Do not link to github's label page (searching for all PRs with that label),
    # but instead to all PRs in the current dashboard with that label.
    # Future: should this *add* to current search terms, instead of replacing them?
    id = f"#{id}" if id else ""
    url = f"{page}?search={label.name}{id}"
    bgcolor = label.color
    fgcolor = "000000" if isLight(int(bgcolor[:2], 16), int(bgcolor[2:4], 16), int(bgcolor[4:], 16)) else "FFFFFF"
    return f"<a href='{url}'><span class='label' style='color: #{fgcolor}; background: #{bgcolor}'>{label.name}</span></a>"


# Auxiliary function, used for sorting the "total time in review".
def format_delta2(delta: timedelta) -> str:
    return f"{delta.days}-{delta.seconds}"

assert parser.isoparse("2024-04-29T18:53:51Z") == datetime(2024, 4, 29, 18, 53, 51, tzinfo=tz.tzutc())


### Core logic: writing out a dashboard of PRs ###

STANDARD_ALIAS_MAPPING = """
  // Return a table column index corresponding to a human-readable alias.
  // |aliasOrIdx| is supposed to be an integer or a string;
  // |show_approvals| is true iff this table contains a visible column of github approvals.
  function getIdx (aliasOrIdx, show_approvals) {
    // Some tables show a column of all PR approvals right after the assignee.
    // In this case, later indices must be shifted by 1.
    let offset = show_approvals ? 1 : 0;
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
      case "assignee":
        return 10;
      // The following column indices depend on whether a dashboard shows
      // the list of users who approved a PR.
      case "approvals":
        if (show_approvals) { return 11; }
        break;
      case "lastUpdate":
        return 11 + offset;
      case "lastStatusChange":
        return 12 + offset;
      case "totalTimeReview":
        return 13 + offset;
      default:
        return aliasOrIdx;
    }
  }
"""

# Dashboard columns with a special sorting relation.
STANDARD_COLUMN_DEFS = "columnDefs: [{ type: 'diff_stat', targets: 5 }, { type: 'assignee', targets: 10 }, { visible: false, targets: [3, 6, 9] } ],"

# Version of STANDARD_ALIAS_MAPPING tailored to the on_the_queue.html page.
# Keep in sync with changes to the above table!
ON_THE_QUEUE_ALIAS_MAPPING = """
  // Return a table column index corresponding to a human-readable alias.
  // |aliasOrIdx| is supposed to be an integer or a string;
  // |show_approvals| is true iff this table contains a visible column of github approvals.
  function getIdx (aliasOrIdx) {
    switch (aliasOrIdx) {
      case "number":
        return 0;
      case "author":
        return 1;
      case "title":
        return 2;
      case "fromFork":
        return 3;
      case "ciStatus":
        return 4;
      case "hasMergeConflict":
        return 5
      case "isBlocked":
        return 6;
      case "isReady":
        return 7;
      case "awaitingReview":
        return 8;
      case "missingTopicLabel":
        return 10;
      case "overallStatus":
        return 11;
      default:
        return aliasOrIdx;
    }
  }
"""

ON_THE_QUEUE_COLUMN_DEFS = "columnDefs: [{ type: 'diff_stat', targets: 5 }, { visible: false, targets: [3, 6, 9] } ],"


# Template for configuring all datatables on a generated webpage.
# Has two template parameters ALIAS_MAPPING and TABLE_CONFIGURATION.
# NB. We always specify a default sorting order, but may override that in the table configuration.
_TEMPLATE_SCRIPT = """
  let diff_stat = DataTable.type('diff_stat', {
    detect: function (data) { return false; },
    order: {
      pre: function (data) {
        // Input has the form
        // <span style="color:green">42</span>/<span style="color:red">13</span>,
        // we extract the tuple (42, 13) and compute their sum 42+13.
        let parts = data.split('</span>/<span', 2);
        return Number(parts[0].slice(parts[0].search(">") + 1)) + Number(parts[1].slice(parts[1].search(">") + 1, -7));
      }
    },
  });
  let formatted_relativedelta = DataTable.type('formatted_relativedelta', {
    detect: function (data) { return data.startsWith('<div style="display:none">'); },
    order: {
      pre: function (data) {
        let main = (data.split('</div>', 2))[0].slice('<div style="display:none">'.length);
        // If there is no input data, main is the empty string.
        if (!main.includes('-')) {
            return -1;
        }
        const [days, seconds, ...rest] = main.split('-');
        return 100000 * Number(days) + Number(seconds);
      }
    }
  })
  // A PR assignee is sorted as a string; except that the string "nobody"
  // (i.e., a PR is unassigned) is sorted last.
  let assignee = DataTable.type('assignee', {
    order: {
      pre: function (data) { return (data == 'nobody') ? "zzzzzzzzzz" : data; }
    },
  });
{ALIAS_MAPPING}
$(document).ready( function () {
  // Parse the URL for any initial configuration settings.
  // Future: use this for deciding which table to apply the options to.
  let fragment = window.location.hash;
  const params = new URLSearchParams(document.location.search);
  const search_params = params.get("search");
  const pageLength = params.get("length") || 10;
  const sort_params = params.getAll("sort");
  {SORT_CONFIG1}
  for (const config of sort_params) {
    if (!config.includes('-')) {
      console.log(`invalid value ${config} passed as sort parameter`);
      continue;
    }
    const [col, dir, ...rest] = config.split('-');
    if (dir != "asc" && dir != "desc") {
      console.log(`invalid sorting direction ${dir} passed as sorting configuration`);
      continue;
    }
    {SORT_CONFIG2}
   }
  const options = {
    stateDuration: 0,
    pageLength: pageLength,
    "searching": true,
    {COLUMN_DEFS}
    order: sort_config,
  };
  if (params.has("search")) {
    options.search = {
        search: search_params
    };
  }
  $('table').each(function () {
    {TABLE_CONFIGURATION}
  })
});
"""


def tables_configuration_script(alias_mapping: str, column_defs: str, test_tables_with_approval: str, omit_column_config=False) -> str:
    # If table_test is not none, we have two sets of alias configurations,
    # for tables with and without approval.
    if test_tables_with_approval:
        sort_config1 = """
        // The configuration for initial sorting of tables, for tables with and without approvals.
        let sort_config = [];
        let sort_config_approvals = [];
        """.strip().replace("        ", "  ")
        sort_config2 = """
        sort_config.push([getIdx(col, false), dir]);
        sort_config_approvals.push([getIdx(col, true), dir]);
        """.strip().replace("        ", "  ")
        table_config = """
        const tableId = $(this).attr('id')
        let tableOptions = { ...options }
        const show_approval = {table_test};
        tableOptions.order = show_approval ? sort_config_approvals : sort_config;
        $(this).DataTable(tableOptions);
        """.strip().replace("        ", "  ").replace("{table_test}", test_tables_with_approval)
    else:
        sort_config1 = """
        // The configuration for initial sorting of tables.
        let sort_config = [];
        """.strip().replace("        ", "  ")
        sort_config2 = "sort_config.push([getIdx(col), dir]);"
        table_config = """
        const tableId = $(this).attr('id') || "";
        if (tableId.startsWith("t-")) {
          $(this).DataTable(options);
        }
        """.replace("        ", "  ").strip()
    template = (_TEMPLATE_SCRIPT.replace("{SORT_CONFIG1}", sort_config1).replace("{SORT_CONFIG2}", sort_config2)
        .replace("{ALIAS_MAPPING}", alias_mapping).replace("{TABLE_CONFIGURATION}", table_config)
        .replace("{COLUMN_DEFS}", column_defs))
    if omit_column_config:
        # NB. This is brittle; keep in sync with other changes!
        template = template.replace("    columnDefs: ", "    // columnDefs: ")
    return template


# NB: keep this list in sync with any dashboard using ExtraColumnSettings.show_approvals(...).
TABLES_WITH_APPROVALS = [Dashboard.QueueStaleUnassigned, Dashboard.QueueStaleAssigned, Dashboard.Approved]
table_test = " || ".join([f'tableId == "{getTableId(kind)}"' for kind in TABLES_WITH_APPROVALS])

# This javascript code is placed at the bottom of each generated webpage page
# (except for the on_the_queue page, which tweaks this slightly).
STANDARD_SCRIPT = tables_configuration_script(STANDARD_ALIAS_MAPPING, STANDARD_COLUMN_DEFS, table_test)

# Settings for which 'extra columns' to display in a PR dashboard.
class ExtraColumnSettings(NamedTuple):
    # Show which github user(s) this PR is assigned to (if any)
    show_assignee: bool
    # Show the number of users who left an approving review on this PR (with a tooltip for their github handles).
    # 'Maintainer merge/delegate' comments are inconsistently labelled as approving or not.
    # In practice, this is not an issue as 'maintainer merge'd PRs are shown separately anyway.
    show_approvals: bool
    potential_reviewers: bool
    hide_update: bool
    # Future possibilities:
    # - number of (transitive) dependencies (with PR numbers)

    @staticmethod
    def default():
        return ExtraColumnSettings(True, False, False, False)

    @staticmethod
    # NB. Any dashboard which calls this option should be noted in |TABLES_WITH_APPROVALS| above.
    def with_approvals(val: bool):
        self = ExtraColumnSettings.default()
        return ExtraColumnSettings(self.show_assignee, val, self.potential_reviewers, self.hide_update)


# Wrap some HTML |code| inside a display:none div; returns the empty string if |code| is empty.
def hide(code: str) -> str:
    return f'<div style="display:none">{code}</div>' if code else ""


# Compute the table entries about a sequence of PRs.
# |page| is the name of the current webpage (e.g. "review_dashboard.html"),
# |id| is the fragment ID of the current table (e.g. "queue").
# 'aggregate_information' maps each PR number to the corresponding aggregate information
# (and may contain information on PRs not to be printed).
# TODO: remove 'prs' in favour of the aggregate information --- once I can ensure that the data
# in the latter is always kept updated.
def _compute_pr_entries(
    page_name: str, id: str,
    prs: List[BasicPRInformation], aggregate_information: dict[int, AggregatePRInfo],
    extra_settings: ExtraColumnSettings, potential_reviewers: dict[int, Tuple[str, List[str]]] | None=None,
) -> str:
    result = ""
    for pr in prs:
        pr_number = str(pr.number)
        name = aggregate_information[pr_number].author
        if pr.url != infer_pr_url(pr.number):
            print(f"warning: PR {pr.number} has url differing from the inferred one:\n  actual:   {pr.url}\n  inferred: {infer_pr_url(pr.number)}", file=sys.stderr)
        labels = _write_labels(pr.labels, page_name, id)
        # Mild HACK: if a PR has label "t-algebra", we append the hidden string "label:t-algebra$" to make this searchable.
        label_hack = hide('label:t-algebra$') if "t-algebra" in [lab.name for lab in pr.labels] else ""
        branch_name = aggregate_information[pr_number].branch_name if pr_number in aggregate_information else "missing"
        description = aggregate_information[pr_number].description
        # Mild HACK: append each PR's author as "author:name" to the end of the author column (hidden),
        # to allow for searches "author:name".
        author_hack = hide(f"author:{name}")
        entries = [pr_link(pr.number, pr.url, branch_name), user_filter_link(name, page_name, id) + author_hack,
            title_link(pr.title, pr.url), description, labels + label_hack]
        # Detailed information about the current PR.
        pr_info = None
        if pr_number in aggregate_information:
            pr_info = aggregate_information[pr_number]
        if pr_info is None:
            print(f"main dashboard: found no aggregate information for PR {pr.number}", file=sys.stderr)
            entries.extend(["-1/-1", "no data available", "-1", "-1", '<a title="no data available">n/a</a>'])
            if extra_settings.show_assignee:
                entries.append("???")
            if extra_settings.show_approvals:
                entries.append("???")
            if extra_settings.potential_reviewers and potential_reviewers is not None:
                entries.append("???")
        else:
            na = '<a title="no data available">n/a</a>'
            total_comments = na if pr_info.number_total_comments is None else str(pr_info.number_total_comments)
            (status, users) = pr_info.users_commented or (None, None)
            entries.extend([
                # NB: keep the styling of the diff column in sync with the custom sorting
                # function by diff size below.
                '<span style="color:green">{}</span>/<span style="color:red">{}</span>'.format(pr_info.additions, pr_info.deletions),
                ",".join(pr_info.modified_files),
                str(pr_info.number_modified_files),
                total_comments, users,
            ])
            if len(pr_info.modified_files) < pr_info.number_modified_files:
                print(f"warning: PR {pr.number} has {pr_info.number_modified_files} modified files, "
                    f"but the list of filenames only contains {len(pr_info.modified_files)}, "
                    "data is incomplete", file=sys.stderr)
            if status == DataStatus.Incomplete:
                print(f"warning: PR {pr.number} supposedly has exactly 100 comments; data is likely incomplete", file=sys.stderr)
            if extra_settings.show_assignee:
                match sorted(pr_info.assignees):
                    case []:
                        assignees = "nobody"
                    case [user]:
                        assignees = user
                    case [user1, user2]:
                        assignees = f"{user1} and {user2}"
                    case several_users:
                        assignees = ", ".join(several_users)
                # Mild HACK: add a hidden string 'assignee:name' for each assignee, to allow
                # a typed search for PR assignees.
                assignee_hack = hide(" ".join((f"assignee:{name}" for name in pr_info.assignees)))
                entries.append(assignees + assignee_hack)

            if extra_settings.show_approvals:
                # Deduplicate the users with approving reviews.
                # FIXME: should one indicate the number of such approvals per user instead?
                approvals_dedup = set(pr_info.approvals)
                app = ", ".join(approvals_dedup)
                approval_link = f'<a title="{app}">{len(approvals_dedup)}</a>' if approvals_dedup else "none"
                entries.append(approval_link)
            if extra_settings.potential_reviewers and potential_reviewers is not None:
                (reviewer_str, names) = potential_reviewers[pr_number]
                entries.append(reviewer_str)
                if names:
                    # Just allow contacting the first reviewer, for now.
                    # Future: change this to one randomly selected reviewer instead?
                    # FUTURE: add a button with a drop-down, for the various options.
                    fn = f"contactMessage('{names[0]}', {pr.number})"
                    entries.append(f'<button onclick="{fn}">Ask {names[0]} for review</button>')
                else:
                    entries.append("")
        if not extra_settings.hide_update:
            update = pr.updatedAt
            tooltip = update.strftime("%Y-%m-%d %H:%M")
            now = datetime.now(timezone.utc)
            rd = relativedelta.relativedelta(now, update)
            prefix = hide(format_delta2(now - update))
            entries.append(f'{prefix} <a title="{tooltip}">{format_delta(rd)} ago</a>')

        # Always start this column with a <div> with display:none, this is important for auto-detecting the column type!
        real_update = f'{hide(" ")}<a title="the last actual update for this PR could not be determined">unknown</a>'
        total_time = f'{hide(" ")}<a title="this PR\'s total time in review could not be determined">unknown</a>'
        if pr_info:
            last_update = aggregate_information[pr_number].last_status_change
            if last_update is not None and last_update.status != DataStatus.Missing:
                date = str(last_update.time).replace("+00:00", "")
                prefix = hide(format_delta2(datetime.now(timezone.utc) - last_update.time))
                real_update = f'{prefix}<a title="{date}">{format_delta(last_update.delta)} ago</a>'
                if last_update.status == DataStatus.Incomplete:
                    real_update += '<a title="caution: this data is likely incomplete">*</a>'
            tqt = aggregate_information[pr_number].total_queue_time
            if tqt is not None and tqt.status != DataStatus.Missing:
                prefix = hide(format_delta2(tqt.value_td))
                total_time = f'{prefix}<a title="{tqt.explanation}">{format_delta(tqt.value_rd)}</a>'
                if tqt.status == DataStatus.Incomplete:
                    total_time += '<a title="caution: this data is likely incomplete">*</a>'
        entries.append(real_update)
        entries.append(total_time)
        result += _write_table_row(entries, "    ")
    return result


# Write the code for a dashboard of a given list of PRs.
# "page_name" is the name of the page this dashboard lives in (e.g. triage.html),
# 'aggregate_information' maps each PR number to the corresponding aggregate information
# (and may contain information on PRs not to be printed).
# TODO: remove 'prs' in favour of the aggregate information --- once I can ensure that the data
# in the latter is always kept updated.
# If 'header' is false, a table header is omitted and just the dashboard is printed.
#
# |potential_reviewers| maps each PR number to a tuple (HTML code, reviewer names).
# The full string is shown on the webpage; the list of reviewer names is used for offering
# a contact button.
def write_dashboard(
    page_name: str,
    prs: dict[Dashboard, List[BasicPRInformation]], kind: Dashboard, aggregate_info: dict[int, AggregatePRInfo],
    extra_settings: ExtraColumnSettings | None=None, header=True,
    potential_reviewers: dict[int, Tuple[str, List[str]]] | None=None, custom_subpage: str | None=None,
) -> str:
    def _inner(
        prs: List[BasicPRInformation], kind: Dashboard, aggregate_info: dict[int, AggregatePRInfo],
        extra_settings: ExtraColumnSettings, add_header: bool
    ):
        # Title of each list, and the corresponding HTML anchor.
        # Explain what each dashboard contains upon hovering the heading.
        if add_header:
            (id, title) = getIdTitle(kind)
            title = _make_h2(id, title, long_description(kind))
            # If there are no PRs, skip the table header and print a bold notice such as
            # "There are currently **no** stale `delegated` PRs. Congratulations!".
            if not prs:
                return f"{title}\nThere are currently <b>no</b> {short_description(kind)}. Congratulations!\n"
        else:
            title = ""
        headings = [
            "Number", "Author", "Title", "Description", "Labels",
            '<a title="number of added/deleted lines">+/-</a>',
            'Modified files (first 100)',
            '<a title="number of files modified">&#128221;</a>',
            '<a title="number of standard or review comments on this PR">&#128172;</a>',
            'All users who commented or reviewed',
        ]
        if extra_settings.show_assignee:
            headings.append('<a title="github user(s) this PR is assigned to (if any)">Assignee(s)</a>')
        if extra_settings.show_approvals:
            headings.append('<a title="github user(s) who have left an approving review of this PR (if any)">Approval(s)</a>')
        if extra_settings.potential_reviewers and potential_reviewers is not None:
            headings.append("Potential reviewers")
            headings.append("Contact")
        if not extra_settings.hide_update:
            headings.append("<a title=\"this pull request's last update, according to github\">Updated</a>")

        # TODO: are there better headings for this and the other header?
        headings.append('<a title="The last time this PR\'s status changed from e.g. review to merge conflict, awaiting-author">Last status change</a>')
        headings.append("total time in review")
        head = _write_table_header(headings, "    ")
        body = _compute_pr_entries(page_name, custom_subpage or getIdTitle(kind)[0], prs, aggregate_info, extra_settings, potential_reviewers)
        id = getTableId(kind) if custom_subpage is None else f"t-{custom_subpage}"
        return f"{title}\n  <table id={id}>\n{head}{body}  </table>"

    if extra_settings is None:
        extra_settings = ExtraColumnSettings.default()
    return _inner(prs[kind], kind, aggregate_info, extra_settings, header)

# Specific code for writing the actual webpage files.

HTML_HEADER = """
<!DOCTYPE html>
<html>
<head>
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.datatables.net; style-src 'self' 'unsafe-inline' https://cdn.datatables.net; form-action 'none'; base-uri 'none'">
<title>Mathlib review and triage dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jquery/3.5.1/jquery.min.js"
    integrity="sha512-bLT0Qm9VnAYZDflyKcBaQ2gg0hSYNQrJ8RilYldYQ1FxQYoCLtUjuuRuZo+fjqhx/qtq/1itJ0C2ejDxltZVFg=="
	crossorigin="anonymous"></script>
<link rel="stylesheet" href="https://cdn.datatables.net/2.1.8/css/dataTables.dataTables.css"
    integrity="sha384-eCorNQ6xLKDT9aok8iCYVVP8S813O3kaugZFLBt1YhfR80d1ZgkNcf2ghiTRzRno" crossorigin="anonymous">
<script src="https://cdn.datatables.net/2.1.8/js/dataTables.js"
    integrity="sha384-cDXquhvkdBprgcpTQsrhfhxXRN4wfwmWauQ3wR5ZTyYtGrET2jd68wvJ1LlDqlQG" crossorigin="anonymous"></script>
<link rel='stylesheet' href='style.css'>
<base target="_blank">
</head>
<body>
""".strip()


# Write a webpage with body out a file called 'outfile*.
# 'custom_script' (if present) is expected to be newline-delimited and appropriately indented.
def write_webpage(body: str, outfile: str, use_tables: bool = True, custom_script: str | None = None) -> None:
    with open(outfile, "w") as fi:
        script = f"<script>{custom_script or STANDARD_SCRIPT}</script>\n" if use_tables else ""
        footer = f"{script}</body>\n</html>"
        print(f"{HTML_HEADER}\n{body}\n{footer}", file=fi)


### Main logic: generating the various webpages ###
# This part is mathlib-specific again; the pieces above were not.


EXPLANATION_ON_THE_QUEUE_PAGE = """
<p>To appear on the review queue, your open pull request must...</p>
<ul>
<li>be based on the <em>master</em> branch of mathlib,</li>
<li>pass mathlib's CI,</li>
<li>not be blocked by another PR (as marked by the labels <em>blocked-by-other-PR</em> and similar)</li>
<li>have no merge conflict (as marked by the <em>merge-conflict</em>),</li>
<li>not be in draft status, nor labelled with one of <em>WIP</em>, <em>help-wanted</em> or <em>please-adopt</em>: these mean the PR is not fully ready yet;</li>
<li>not be labelled <em>awaiting-CI</em>, <em>awaiting-author</em> or <em>awaiting-zulip</em>,</li>
<li>not be labelled <em>delegated</em>, <em>auto-merge-after-CI</em> or <em>ready-to-merge</em>: these labels mean your PR is already approved.</li>
</ul>
<p>For PRs which add new code (as opposed to e.g. performance optimisation, refactoring or documentation), it is very helpful to add a corresponding area label. Otherwise, it may take longer to find a suitable reviewer.</p>
<p>
The table below contains all open PRs against the <em>master</em> branch which are not in draft mode. For each PR, it shows whether the checks above are satisfied.
You can filter that list by entering terms into the search box, such as the PR number or your github username.</p>""".lstrip()


# Print a hyperlink to |s|, which displays |s| inside a <code> tag.
def code_link(s: str) -> str:
    return f'<a href="{s}"><code>{s}</code></a>'


TIPS_AND_TRICKS = f"""  <h2 id="tips-and-tricks"><a href="#tips-and-tricks">Tips and tricks</a></h2>
  <details><summary>These webpages have a couple of hidden features. Click here to learn more.</summary>
  <div><ul>
  <li><strong>semantic sorting</strong>: clicking on a table column sorts by the column. This sorting is semantically correct: PR diffs are sorted by the total number of added or deleted lines, sorting by total time in review converts between months, days, hours and seconds</li>
  <li><strong>hovers</strong>: many items contain further information when you hover over it.
  Hover over a section header to see which PRs are contained in it, hover over a column definition to see what it measures, hover over a PR number to see its git branch name, hover over a time column to see the detailed time, etc.</li>
  <li><strong>author search</strong>: search for <code>author:name</code> to find all PRs by a particular author</li>
  <li><strong>assignee search</strong>: search for <code>assignee:name</code> to find all PRs assigned to this user</li>
  <li><strong>exact label matching</strong>: a few label names are prefixes of other labels, e.g. <code>t-algebra</code> is a prefix of <code>t-algebraic geometry</code>, so searching for <code>t-algebra</code> will also find <code>t-algebraic geometry</code> PRs.
  There is special support for this combining: searching for <code>t-algebra$</code> will match only PRs with label <code>t-algebra</code>.</li>
  <li><strong>find PRs by modified file</strong>: search for a file name and find all PRs in a list which modify this file. Searching for several files finds all PRs which modify all these files. (Caveat: we only track the first 100 files each PR changes, so this may yield incomplete results for cross-cutting PRs. Such PRs are rare, however.)</li>
  <li><strong>find your PRs</strong>: searching for your user name returns all PRs you reviewed or commented on</li>
  <li><strong>search PR description</strong>: searching also searches a PR description, and the list of all users who ever commented on this PR. To find all PRs from the sphere-eversion project, searching for "sphere eversion" (or "sphere eversion") should do the trick.</li>
  <li><strong>configuration via URL</strong>: you can configure the initial sorting, search terms and number of entries per page by changing the URL you're visiting. Three short examples:
    <ul>
      <li>{code_link("review_dashboard.html?sort=totalTimeReview-desc#queue")} sorts the #queue table by total time in review (in descending order),</li>
      <li>{code_link("triage.html=?search=manifold&sort=author-asc&length=10#all")} shows all PRs which contain the string "manifold" in their entry or PR description, sorted by author (in ascending order), with 10 items per page, and</li>
      <li>{code_link("on_the_queue.html?search=jcommelin&length=100")} shows the status of all PRs by <code>jcommelin</code></li>
      <li>{code_link("dependency_dashboard.html?search=carleson")} shows visualises dependencies between all PRs from the Carleson project (i.e., with the Carleson label)
    </ul>
  <details><summary>Reference-level explanation of search syntax</summary>
  The <code>search</code> parameter filters all tables on a page by default.
  The <code>sort</code> parameter changes the initial sorting of all dashboards; if the parameter is given several times, this configures a multi-column sort (sorting by the first parameter first). A valid value is of the form <code>idxOrAlias-direction</code>, where <code>direction</code> is either <code>asc</code> or <code>desc</code> (for ascending or descending order), and <code>idxOrAlias</code> describes the column to sort.
  All columns have human-readable names: these are <code>number</code>, <code>author</code>, <code>title</code>, <code>labels</code>, <code>diff</code>, <code>numberChangedFiles</code>, <code>numberComments</code>, <code>assignee</code>, <code>approvals</code>, <code>lastUpdate</code>, <code>lastStatusChange</code> and <code>totalTimeReview</code>, respectively &emdash; mapping to the obvious column.
  Alternatively (deprecated), you can pass in the (0-based) index of the column you want to sort. (You have to account for hidden columns, and there are no stability guarantees. This option is only kept for backwards compatibility.)
  </details>
  </li>
  <li><strong>filtering</strong>: click on a label or PR author to filter by this author</li>
  <li><strong>exact match when searching</strong>: searching for <code>foo bar</code> will match all PRs whose entry contains the string foo and the string bar (but at potentially different places). Searching for <code>"foo bar"</code> only yields literal occurrences of the string <code>foo bar</code></li>
  <li><strong>multi-column sorting</strong>: use shift-click to sort by a second (or third, etc.) column</li>
  </ul></div>

  Would you like to add a hidden feature? <a href="https://github.com/leanprover-community/queueboard/issues?q=is%3Aissue%20state%3Aopen%20label%3Ahas-mentoring-instructions">These proposed features</a> have mentoring instructions; PRs are very welcome!
  </details>"""


# Print a webpage "why is my PR not on the queue" to the file "on_the_queue.html".
# 'prs' is the list of PRs on which to print information;
# 'CI_status' states, for each PR, whether PR passes, fails or is still running (or we are missing information).
# 'base_branch' returns the pase branch of each PR.
# Future: add a check for PRs which are *not* opened from a fork of mathlib.
def write_on_the_queue_page(
    all_pr_status: dict[int, PRStatus],
    aggregate_info: dict[int, AggregatePRInfo],
    prs: List[BasicPRInformation],
    # prs_from_fork: List[BasicPRInformation],
    CI_status: dict[int, CIStatus],
    base_branch: dict[int, str],
) -> None:
    def icon(state: bool | None) -> str:
        """Return a green checkmark emoji if `state` is true, and a red cross emoji otherwise."""
        return "&#9989;" if state else "&#10060;"

    body = ""
    for pr in prs:
        pr_number = str(pr.number)
        if base_branch[pr_number] != "master":
            continue
        # from_fork = pr in prs_from_fork
        status_symbol = {
            CIStatus.Pass: f'<a title="CI for this pull request passes">{icon(True)}</a>',
            CIStatus.Fail: f'<a title="CI for this pull request fails">{icon(False)}</a>',
            # TODO: change symbol, cross with a ?, with underline and explanation!
            CIStatus.FailInessential: f'<a title="CI for this pull request fails, but the failing jobs are typically spurious or related to mathlib\'s infrastructure. Unless this PR modifies that infrastructure itself, the failure is not the fault of this PR">{icon(False)}?</a>',
            CIStatus.Running: '<a title="CI for this pull request is still running">&#128996;</a>',
            CIStatus.Missing: '<a title="missing information about this PR\'s CI status">???</a>',
        }
        is_blocked = any(lab.name in ["blocked-by-other-PR", "blocked-by-core-PR", "blocked-by-batt-PR", "blocked-by-qq-PR"] for lab in pr.labels)
        has_merge_conflict = "merge-conflict" in [lab.name for lab in pr.labels]
        is_ready = not (any(lab.name in ["WIP", "help-wanted", "please-adopt"] for lab in pr.labels))
        review = not (any(lab.name in ["awaiting-CI", "awaiting-author", "awaiting-zulip"] for lab in pr.labels))
        overall = (CI_status[pr_number] == CIStatus.Pass) and (not is_blocked) and (not has_merge_conflict) and is_ready and review
        name = pr.author_name
        if name is None:
            # TODO: take the author from the aggregate information instead
            # requires refactoring this function accordingly
            name = "dependabot(?)"
        has_topic_label = any((lab.name.startswith("t-") or lab.name in ["CI", "IMO"]) for lab in pr.labels)
        missing_topic_label = pr.title.startswith("feat") and (not has_topic_label)
        topic_label_symbol = '<a title="this feature PR has no topic label">⚠️</a>' if missing_topic_label else ""
        current_status = PRStatus.Closed if aggregate_info[pr_number].state == "closed" else all_pr_status[pr_number]
        (curr1, curr2) = {
            PRStatus.AwaitingBors: ("is", "awaiting bors"),
            PRStatus.AwaitingAuthor: ("is", "awaiting author"),
            PRStatus.AwaitingReview: ("is", "awaiting review"),
            PRStatus.AwaitingDecision: ("is", "awaiting a zulip discussion"),
            PRStatus.MergeConflict: ("has a", "merge conflict"),
            PRStatus.Delegated: ("is", "delegated"),
            PRStatus.HelpWanted: ("is", "looking for help"),
            PRStatus.Blocked: ("is", "blocked on another PR"),
            PRStatus.NotReady: ("is", "labelled WIP or marked draft"),
            PRStatus.Contradictory: ("has", "contradictory labels"),
            PRStatus.Closed: ("is", "closed (so shouldn't appear in this list)"),
            # TODO: in August, re-instate reverted
            # PRStatus.FromFork: ("is", "opened from a fork"),
        }[current_status]
        if current_status == PRStatus.NotReady:
            # We emit more fine-grained information for "not ready" PRs.
            if aggregate_info[pr_number].is_draft:
                curr2 = "marked draft"
            if "WIP" in [lab.name for lab in aggregate_info[pr_number].labels]:
                curr2 = "labelled WIP"
            elif "awaiting-CI" in [lab.name for lab in aggregate_info[pr_number].labels]:
                (curr1, curr2) = ("", "does not pass CI")
            else:
                match aggregate_info[pr_number].CI_status:
                    case CIStatus.Fail:
                        (curr1, curr2) = ("has", "failing CI")
                    case CIStatus.Running:
                        curr2 = "running CI"
                    case CIStatus.Missing:
                        curr2 = "missing CI information"
                    case CIStatus.FailInessential:
                        # Future: one could be more specific, but this would have to be in a hover.
                        (curr1, curr2) = ("has", "infrastructure-related CI failing")
                    case CIStatus.Pass:
                        pass  # should not happen!
                    case _:
                        raise ValueError(f"missing case: {aggregate_info[pr_number].CI_status}")
        pr_data = aggregate_info[pr_number]
        if pr_data.last_status_change is None or pr_data.total_queue_time is None:
            status = curr2
        else:
            if pr_data.last_status_change.current_status not in [PRStatus.NotReady, PRStatus.Closed]:
                if pr_data.last_status_change.current_status != current_status and pr_data.last_status_change.status == DataStatus.Valid:
                    print(
                        f"WARNING: mismatch for {pr.number}: current status (from REST API data) is {current_status}, "
                        f"but the 'last status' from the aggregate data is {pr_data.last_status_change.current_status}", file=sys.stderr
                    )
            details = f" (details: {pr_data.total_queue_time.explanation})" if pr_data.total_queue_time.explanation else ""
            hover = f"PR {pr.number} was in review for {format_delta(pr_data.total_queue_time.value_rd)} overall{details}. It was last updated {format_delta(pr_data.last_status_change.delta)} ago and {curr1} {curr2}."
            status = f'<a title="{hover}">{curr2}</a>'
            if pr_data.last_status_change.status == "incomplete" or pr_data.total_queue_time.status == "incomplete":
                status += '<a title="caution: this data is likely incomplete">*</a>'
        entries = [
            pr_link(pr.number, pr.url), user_filter_link(name, "on_the_queue.html", ""), title_link(pr.title, pr.url),
            _write_labels(pr.labels, "on_the_queue.html", ""),
            # icon(not from_fork), # TODO(August/September): re-instate with inverted meaning
            status_symbol[CI_status[pr_number]],
            icon(not is_blocked), icon(not has_merge_conflict), icon(is_ready), icon(review), icon(overall),
            topic_label_symbol, status
        ]
        result = _write_table_row(entries, "    ")
        body += result
    headings = [
        "Number", "Author", "Title", "Labels",
        # TODO(August/September): re-instate with inverted meaning "not from a fork?",
        "CI status?",
        '<a title="not labelled with blocked-by-other-PR, blocked-by-batt-PR, blocked-by-core-PR, blocked-by-qq-PR">not blocked?</a>',
        "no merge conflict?",
        '<a title="not in draft state or labelled as in progress">ready?</a>',
        '<a title="not labelled awaiting-author, awaiting-zulip, awaiting-CI">awaiting review?</a>',
        "On the review queue?",
        "Missing topic label?",
        "PR's overall status",
    ]
    head = _write_table_header(headings, "    ")
    table = f"  <table id='t-prs'>\n{head}{body}  </table>"
    # FUTURE: can this time be displayed in the local time zone of the user viewing this page?
    updated = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    start = f"  <h1>Why is my PR not on the queue?</h1>\n  <small>This page was last updated on: {updated}</small>"
    script = tables_configuration_script(ON_THE_QUEUE_ALIAS_MAPPING, ON_THE_QUEUE_COLUMN_DEFS, "", True)
    write_webpage(f"{start}\n{EXPLANATION_ON_THE_QUEUE_PAGE}\n{table}", "on_the_queue.html", custom_script=script)


def write_overview_page(updated: str) -> None:
    title = "  <h1>Mathlib review and triage dashboard</h1>"
    welcome = """<p>Welcome to the mathlib review and triage webpage! There are many ways to help, what are you looking for in particular?</p>

<div class="btn-group">
  <a href="review_dashboard.html"><button>Review queue</button></a>
  <a href="maintainers_quick.html"><button>For maintainers (quick)</button></a>
  <a href="help_out.html"><button>Help out</button></a>
  <a href="triage.html"><button>Triage dashboard</button></a>
  <a href="dependency_dashboard.html"><button>Dependency graph</button></a>
</div><p></p>
<div class="btn-group"><a href="on_the_queue.html"><button>Why is my PR not on the queue? Can I see all my PRs?</button></a></div><p></p>
<div class="btn-group"><a href="triage.html#all"><button>What's going on? Just show me all open PRs, please!</button></a></div><p></p>

<details>
  <summary>What do these buttons mean? Which page should I visit?</summary>
  <ul>
    <li>If you just want to see all open PRs, there is a <a href="triage.html#all">dashboard</a> just for you.</li>
    <li>Would you like to review some pull request? The <strong><a href="review_dashboard.html">review dashboard</a></strong> contains all PRs waiting for review. There are special sections for PRs by new contributors, labelled <em>easy</em> or addressing technical debt.</li>
    <li>Would you like to find out <strong>why</strong> your PR is (not) <strong>on the review queue</strong>? Are you interested in an overview of all your PRs with their status? <a href="on_the_queue.html">This webpage</a> contains all information necessary.</li>
    <li>There is a webpage for <strong>maintainers with little time</strong>: this contains e.g. all PRs which are just awaiting maintainer approval.
    If you actually have some more time at your hands, the <a href="review_dashboard.html">page for reviewers</a> or the <a href="triage.html">triage dashboard</a> should be useful.</li>
    <li>Would you just like to <strong>help out</strong>? <a href="help_out.html">This page</a> collects PRs where help was requested, or where some quick action can be useful.</li>
    <li>Are you coming here for <strong>PR triage</strong>: looking for PRs stuck in some state, and would like to move them along? The <a href="triage.html">triage dashboard</a> has the ultimate collection of all public information.</li>
    <li>Want to visualize <strong>dependency relationships</strong> between PRs? The <a href="dependency_dashboard.html">dependency graph</a> shows which PRs are blocked by others and helps identify unblocked work.</li>
    <!-- 'hidden' page for maintainers to assign reviewers must be generated locally, by running a script -->
  </ul>
</details>"""
    welcome = "\n  ".join(welcome.splitlines())
    feedback = '<p>Feedback (including bug reports and ideas for improvements) on this dashboard is very welcome, for instance <a href="https://github.com/leanprover-community/queueboard">directly on the github repository</a>.</p>'
    body = f"{title}\n  {welcome}\n  {feedback}\n  <p><small>This dashboard was last updated on: {updated}</small></p>\n\n{TIPS_AND_TRICKS}\n"
    write_webpage(body, "index.html", use_tables=False)


def write_review_queue_page(
    updated: str,
    prs_to_list: dict[Dashboard, List[BasicPRInformation]],
    aggregate_info: dict[int, AggregatePRInfo],
) -> None:
    title = "  <h1>The mathlib review queue</h1>"
    welcome = "<p>Welcome to the mathlib review page. Everybody's help with reviewing is appreciated. Reviewing contributions is important, and everybody is welcome to review pull requests! If you're not sure how, the <a href=\"https://leanprover-community.github.io/contribute/pr-review.html\">pull request review guide</a> is there to help you.<br>\n  This page contains tables of</p>"
    items = [
        (Dashboard.Queue, "all PRs ready for review", ""),
        (
            Dashboard.QueueNewContributor,
            'among these, all PRs written by "new contributors"',
            " (i.e., everybody who has had at most five PRs merged to mathlib)",
        ),
        (Dashboard.QueueEasy, 'just the PRs labelled "easy"', ""),
        (Dashboard.QueueTechDebt, "just the PRs addressing technical debt", ""),
    ]
    list_items = [
        f'<li><a href="#{getIdTitle(kind)[0]}">{description}</a>{unlinked}</li>\n' for (kind, description, unlinked) in items
    ]
    body = f"{title}\n  {welcome}\n  <ul>{'    '.join(list_items)}  </ul>\n  <small>This dashboard was last updated on: {updated}</small>\n\n"
    dashboards = [write_dashboard("review_dashboard.html", prs_to_list, kind, aggregate_info) for (kind, _, _) in items]
    body += "\n".join(dashboards) + "\n"
    write_webpage(body, "review_dashboard.html")


def write_maintainers_quick_page(
    updated: str,
    prs_to_list: dict[Dashboard, List[BasicPRInformation]],
    aggregate_info: dict[int, AggregatePRInfo],
) -> None:
    title = "  <h1>Maintainers page: short tasks</h1>"
    welcome = "<p>Are you a maintainer with just a short amount of time? The following kinds of pull requests could be relevant for you.</p>"
    items = [
        (Dashboard.StaleReadyToMerge, "stale PRs ready-to-merge", ""),
        (Dashboard.StaleMaintainerMerge, 'stale PRs labelled "maintainer merge"', ""),
        (Dashboard.AllMaintainerMerge, "all PRs labelled 'maintainer merge'", ""),
        # TODO: in August, re-instate reverted
        # (Dashboard.FromFork, "all PRs made from a fork", ""),
        (Dashboard.NeedsDecision, "all PRs waiting on finding consensus on zulip", ""),
        (Dashboard.QueueTechDebt, "just the PRs addressing technical debt", ""),
    ]
    list_items = [
        f'<li><a href="#{getIdTitle(kind)[0]}">{description}</a>{unlinked}</li>\n' for (kind, description, unlinked) in items
    ]
    post = 'If you realise you actually have a bit more time, you can also look at the <a href="review_dashboard.html">page for reviewers</a>, or look at the <a href="triage.html">triage page!</a>'
    body = f"{title}\n  {welcome}\n  <ul>{'    '.join(list_items)}  </ul>\n  {post}<br>\n  <small>This dashboard was last updated on: {updated}</small>\n\n"
    dashboards = [write_dashboard("maintainers_quick.html", prs_to_list, kind, aggregate_info) for (kind, _, _) in items]
    body += "\n".join(dashboards) + "\n"
    write_webpage(body, "maintainers_quick.html")


def write_help_out_page(
    updated: str,
    prs_to_list: dict[Dashboard, List[BasicPRInformation]],
    aggregate_info: dict[int, AggregatePRInfo],
) -> None:
    title = "  <h1>Helping out: short tasks</h1>"
    welcome = "<p>Would you like to help out at a PR, differently from reviewing? Here are some ideas:</p>"
    items = [
        (
            Dashboard.InessentialCIFails,
            "take a look at ",
            "PRs with just some mathlib-infrastructure-related CI failure",
            ": unless it's an infrastructure PR, these are spurious/not the PRs fault. If the failure is spurious, commenting to this effect can be helpful.",
        ),
        (Dashboard.NeedsHelp, "take a look at ", "PRs labelled help-wanted or please-adopt", ""),
        (
            Dashboard.NeedsMerge,
            "If the author hasn't noticed, you can ask in a PR which ",
            "just has a merge conflict, but would be reviewable otherwise",
            ". (Remember, that most contributors to mathlib are volunteers, contribute in their free time and often have other commitments — and that real-life events can happen!)",
        ),
        # TODO: in August, re-instate reverted
        # (
        #     Dashboard.FromFork,
        #     "post a comment on a ",
        #     "PR made from a fork",
        #     ", nicely asking them to re-submit it from a mathlib branch instead",
        # ),
        (
            Dashboard.StaleNewContributor,
            "check if any ",
            "'stale' PR by a new contributor",
            " benefits from support, such as help with failing CI or providing feedback on the code",
        ),
        (
            Dashboard.StaleDelegated,
            "check if any ",
            "'stale' delegated PR",
            " benefits from support, such as by fixing merge conflicts (but be sure to ask first; the PR author might simply be very busy)",
        ),
    ]
    list_items = [
        f'<li>{pre}<a href="#{getIdTitle(kind)[0]}">{description}</a>{post}</li>\n' for (kind, pre, description, post) in items
    ]
    body = f"{title}\n  {welcome}\n  <ul>{'    '.join(list_items)}  </ul>\n  <small>This dashboard was last updated on: {updated}</small>\n\n"
    dashboards = [write_dashboard("help_out.html", prs_to_list, kind, aggregate_info) for (kind, _, _, _) in items]
    body += "\n".join(dashboards) + "\n"
    write_webpage(body, "help_out.html")


def pr_statistics(
    all_pr_statusses: dict[int, PRStatus],
    aggregate_info: dict[int, AggregatePRInfo],
    prs: dict[Dashboard, List[BasicPRInformation]],
    all_ready_prs: List[BasicPRInformation],
    all_draft_prs: List[BasicPRInformation],
    is_triage_board: bool,
) -> str:
    (number_all, details_list, piechart) = gather_pr_statistics(all_pr_statusses, aggregate_info, prs, all_ready_prs, all_draft_prs, is_triage_board)
    return f'\n{_make_h2("statistics", "Overall statistics")}\nFound <b>{number_all}</b> open PRs overall. Among these PRs\n{details_list}{piechart}\n'


def write_triage_page(
    updated: str,
    prs_to_list: dict[Dashboard, List[BasicPRInformation]],
    all_pr_status: dict[int, PRStatus],
    aggregate_info: dict[int, AggregatePRInfo],
    # These two are just used for generating statistics.
    nondraft_PRs: list[BasicPRInformation],
    draft_PRs: list[BasicPRInformation],
) -> None:
    title = "  <h1>Mathlib triage dashboard</h1>"
    welcome = "<p>Welcome to the PR triage page! This page is perfect if you intend to look for pull request which seem to have stalled.<br>Feedback on designing this page or further information to include is very welcome.</p>"
    welcome += f"\n  <small>This dashboard was last updated on: {updated}</small>"

    spurious_ci = (
        getIdTitle(Dashboard.InessentialCIFails)[0], "Likely-spurious CI failures",
         "PRs with just failing CI, and all failures that are very likely to be infrastructure issues, and not the fault of the PR!"
    )
    subsections = [
        ("statistics", "PR statistics"),
        ("not-yet-landed", "Not yet landed PRs"),
        ("review-status", "Review status"),
        (getIdTitle(Dashboard.QueueStaleUnassigned)[0], "Stale unassigned PRs"),
        (getIdTitle(Dashboard.QueueStaleAssigned)[0], "Stale assigned PRs"),
        ("spuriousci", ""),
        (getIdTitle(Dashboard.QueueTechDebt)[0], "Tech debt PRs"),
        getIdTitle(Dashboard.NeedsDecision),
        getIdTitle(Dashboard.NeedsMerge),
        getIdTitle(Dashboard.StaleNewContributor),
        (getIdTitle(Dashboard.OtherBase)[0], "PRs not into master"),
        # TODO: in August, re-instate reverted
        # (getIdTitle(Dashboard.FromFork)[0], "PRs from a fork"),
        getIdTitle(Dashboard.Approved),
        getIdTitle(Dashboard.All),
        ("other", "Other lists of PRs"),
    ]
    subsections_mapped = [
        (a, b, None) if a != "spuriousci" else spurious_ci
        for (a, b) in subsections
    ]
    # Created solely because escaping in format strings was too hard.
    def aux(tooltip: str | None) -> str:
        return " " if tooltip is None else f' title="{tooltip}"'
    items = [
        f"<a href=\"#{anchor}\"{aux(tooltip)}target=\"_self\">{title}</a>"
        for (anchor, title, tooltip) in subsections_mapped
    ]
    toc = f"<br><p>\n<b>Quick links:</b> {' | '.join(items)}"

    stats = pr_statistics(all_pr_status, aggregate_info, prs_to_list, nondraft_PRs, draft_PRs, False)

    output_file = "triage.html"
    some_stale = f": <strong>{len(prs_to_list[Dashboard.StaleReadyToMerge])}</strong> of them are stale, and merit another look</li>\n"
    some_stale += write_dashboard(output_file, prs_to_list, Dashboard.StaleReadyToMerge, aggregate_info, header=False)
    no_stale = " &mdash; among these <strong>no</strong> stale ones, congratulations!</li>"
    ready_to_merge = f"<strong>{len(prs_to_list[Dashboard.AllReadyToMerge])}</strong> PRs are ready to merge{some_stale if prs_to_list[Dashboard.StaleReadyToMerge] else no_stale}"

    stale_mm = f'of which <strong>{len(prs_to_list[Dashboard.StaleMaintainerMerge])}</strong> have not been updated in a day (<a href="maintainers_quick.html#stale-maintainer-merge">these</a>)'
    no_stale_mm = "<strong>none of which</strong> has been pending for more than a day. Congratulations!"
    mm = f'<strong>{len(prs_to_list[Dashboard.AllMaintainerMerge])}</strong> PRs are waiting on maintainer approval (<a href="maintainers_quick.html#all-maintainer-merge">these</a>), {stale_mm if prs_to_list[Dashboard.StaleMaintainerMerge] else no_stale_mm}'

    no_stale_delegated = "<li><strong>no</strong> PRs have been delegated and not updated in a day, congratulations!</li>"
    stale_delegated = f"<li><details><summary><strong>{len(prs_to_list[Dashboard.StaleDelegated])}</strong> PRs have been delegated and not updated in a day</summary>\n  \n  {write_dashboard(output_file, prs_to_list, Dashboard.StaleDelegated, aggregate_info, header=False)}</details></li>"

    notlanded = f"""{_make_h2('not-yet-landed', 'Approved, not yet landed PRs')}

    At the moment,
    <ul>
      <li>{ready_to_merge}
      <li>{mm}</li>
      {stale_delegated if prs_to_list[Dashboard.StaleDelegated] else no_stale_delegated}
    </ul>"""

    # All PRs which appear on the queue for the first time in the past two weeks
    # as computed from aggregate events data.
    recent_on_queue = []
    two_weeks_ago = datetime.now(timezone.utc) - timedelta(days=14)
    for pr in prs_to_list[Dashboard.Queue]:
        pr_number = str(pr.number)
        first_on_queue = aggregate_info[pr_number].first_on_queue
        if first_on_queue is not None:
            (foq_status, foq_time) = first_on_queue
            if foq_time is None:
                if foq_status != DataStatus.Incomplete:
                    print(f"warning: PR {pr.number} is listed as never on queue, while it's on the queue", file=sys.stderr)
            elif two_weeks_ago <= foq_time:
                recent_on_queue.append(pr.number)

    # PRs on the queue which are unassigned and have had no meaningful update (i.e. status change) in the past week.
    unassigned = len(prs_to_list[Dashboard.QueueStaleUnassigned])
    # Awaiting review, assigned and no meaningful update in two weeks.
    # (This includes PRs which were reviewed, when the reviewer forgot to change the PR label.)
    stale_assigned = len(prs_to_list[Dashboard.QueueStaleAssigned])
    review_heading = f"""\n{_make_h2('review-status', 'Review status')}
  <p>There are currently <strong>{len(prs_to_list[Dashboard.Queue])}</strong> {link_to(Dashboard.Queue, "PRs awaiting review", "review_dashboard.html")}. Among these,</p>
  <ul>
    <li><strong>{len(prs_to_list[Dashboard.QueueEasy])}</strong> are labelled easy ({link_to(Dashboard.QueueEasy, subpage="review_dashboard.html")}),</li>
    <li><strong>{len(prs_to_list[Dashboard.QueueTechDebt])}</strong> are addressing technical debt ({link_to(Dashboard.QueueTechDebt, "namely these", "review_dashboard.html")}), and</li>
    <li><strong>{len(recent_on_queue)}</strong> appeared on the review queue within the last two weeks.</li>
  </ul>
  <p>On the other hand, {link_to(Dashboard.QueueStaleUnassigned, f"<strong>{unassigned}</strong> PRs")} are unassigned and have not seen a status change in a week, and {link_to(Dashboard.QueueStaleAssigned, f"<strong>{stale_assigned}</strong> PRs")} are assigned, without recent review activity.</p>"""
    review_heading = "\n  ".join(review_heading.splitlines())

    # Write a dashboard of unassigned PRs: we can safely skip the "assignee" column.
    config = ExtraColumnSettings.with_approvals(True)
    stale_unassigned = write_dashboard(output_file, prs_to_list, Dashboard.QueueStaleUnassigned, aggregate_info, config)

    # XXX: when updating the definition of "stale assigned" PRs, make sure to update all the dashboard descriptions
    setting = ExtraColumnSettings.with_approvals(True)
    further = write_dashboard(output_file, prs_to_list, Dashboard.QueueStaleAssigned, aggregate_info, setting)

    others = [
        Dashboard.InessentialCIFails,
        Dashboard.TechDebt,
        Dashboard.NeedsDecision,
        Dashboard.NeedsMerge,
        Dashboard.StaleNewContributor,
        Dashboard.OtherBase,
        # TODO: in August, re-instate reverted
        # Dashboard.FromFork,
        Dashboard.Approved,
        Dashboard.All,
    ]
    for kind in others:
        setting = ExtraColumnSettings.with_approvals(kind == Dashboard.Approved)
        further += write_dashboard(output_file, prs_to_list, kind, aggregate_info, setting)

    # xxx: audit links; which ones should open on the same page, which ones in a new tab?

    items2 = []
    for kind in Dashboard._member_map_.values():
        # These dashboards were already put on other pages or earlier on this page:
        # no need to display them again.
        kinds_to_hide = [
            Dashboard.Queue,
            Dashboard.QueueEasy,
            Dashboard.QueueNewContributor,
            Dashboard.QueueTechDebt,
            Dashboard.QueueStaleUnassigned,
            Dashboard.QueueStaleAssigned,
            Dashboard.AllMaintainerMerge,
            Dashboard.StaleMaintainerMerge,
            Dashboard.StaleDelegated,
            Dashboard.AllReadyToMerge,
            Dashboard.StaleReadyToMerge,
            Dashboard.NeedsHelp,
        ]
        if kind not in kinds_to_hide and kind not in others:
            items2.append((kind, "", long_description(kind), ""))
    list_items = [
        f'<li>{pre}<a href="#{getIdTitle(kind)[0]}">{description}</a>{post}</li>\n' for (kind, pre, description, post) in items2
    ]
    remainder = f"""\n{_make_h2('other', 'Other lists of PRs')}
    Some other lists of PRs which could be useful:
    <ul>{'    '.join(list_items)}  </ul>
    """

    body = f"{title}\n  {welcome}\n  {toc}\n  {stats}\n  {notlanded}\n  {review_heading}\n  {stale_unassigned}\n  {further}\n  {remainder}\n"
    dashboards = [write_dashboard(output_file, prs_to_list, kind, aggregate_info) for (kind, _, _, _) in items2]
    body += "\n".join(dashboards) + "\n"
    write_webpage(body, output_file)


def main() -> None:
    aggregate_info = load_from_json_file(path.join("api", "aggregate_info.json"))
    draft_PRs = load_from_json_file(path.join("api", "draft_PRs.json"))
    nondraft_PRs = load_from_json_file(path.join("api", "nondraft_PRs.json"))
    CI_status = load_from_json_file(path.join("api", "CI_status.json"))
    all_pr_status = load_from_json_file(path.join("api", "all_pr_status.json"))
    base_branch = load_from_json_file(path.join("api", "base_branch.json"))
    prs_to_list = load_from_json_file(path.join("api", "prs_to_list.json"))

    write_on_the_queue_page(all_pr_status, aggregate_info, nondraft_PRs, CI_status, base_branch)

    # FUTURE: can this time be displayed in the local time zone of the user viewing this page?
    updated = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    write_overview_page(updated)
    # Future idea: add a histogram with the most common areas,
    # or dedicated tables for common areas (and perhaps one for t-algebra, because it's hard to filter)
    write_review_queue_page(updated, prs_to_list, aggregate_info)
    write_maintainers_quick_page(updated, prs_to_list, aggregate_info)
    write_help_out_page(updated, prs_to_list, aggregate_info)
    write_triage_page(updated, prs_to_list, all_pr_status, aggregate_info, nondraft_PRs, draft_PRs)


if __name__ == "__main__":
    main()
