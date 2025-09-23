#!/usr/bin/env python3

"""
This file contains the code for computing the PRs on each dashboard in |mathlib_dashboards.py|.

"""

from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import sys
from dateutil import parser, relativedelta
from typing import Dict, List, NamedTuple, OrderedDict, Tuple, Any

from ci_status import CIStatus
from classify_pr_state import (PRState, PRStatus,
                               determine_PR_status, label_categorisation_rules)
from mathlib_dashboards import Dashboard, getIdTitle
from util import my_assert_eq, timedelta_tryParse, relativedelta_tryParse


# The following structures are completely project-agnostic.

# The information we need about each PR label: its name, background colour and URL
class Label(NamedTuple):
    name: str
    """This label's background colour, as a six-digit hexadecimal code"""
    color: str
    url: str


# Basic information about a PR: does not contain the diff size, which is contained in pr_info.json instead.
class BasicPRInformation(NamedTuple):
    number: int  # PR number, non-negative
    # Just the author's github handle; the corresponding URL is determined automatically.
    # For dependabot PRs, this can be `None` due to quirks in the data returned by Github's REST API.
    author_name: str | None
    title: str
    url: str
    labels: List[Label]
    # Github's answer to "last updated at"
    updatedAt: datetime


# Extract all PRs mentioned in a data file.
def _extract_prs(data: dict) -> List[BasicPRInformation]:
    prs = []
    for page in data["output"]:
        for entry in page["data"]["search"]["nodes"]:
            name = None
            if "login" not in entry["author"]:
                print(
                    f'warning: missing author information for PR {entry["number"]}, its authors dictionary is {entry["author"]} --- was this submitted by dependabot?',
                    file=sys.stderr,
                )
            else:
                name = entry["author"]["login"]
            labels = [Label(label["name"], label["color"], label["url"]) for label in entry["labels"]["nodes"]]

            prs.append(BasicPRInformation(entry["number"], name, entry["title"], entry["url"], labels, parser.isoparse(entry["updatedAt"])))
    return prs


class DataStatus(StrEnum):
    Valid = "valid"
    Incomplete = "incomplete"
    # This can happen if a PR is stubborn (i.e. no events data is collected)
    # or a PR's data is contradictory (hence ignored).
    Missing = "missing"

    # Throws a ValueError on invalid input.
    @staticmethod
    def fromStr(val: str):
        return {
            "valid": DataStatus.Valid,
            "incomplete": DataStatus.Incomplete,
            "missing": DataStatus.Missing,
        }[val]

class LastStatusChange(NamedTuple):
    status: DataStatus
    time: datetime
    delta: relativedelta.relativedelta
    current_status: PRStatus

class TotalQueueTime(NamedTuple):
    status: DataStatus
    value_td: timedelta
    value_rd: relativedelta.relativedelta
    explanation: str

# All information about a single PR contained in `open_pr_info.json`.
# Keep this in sync with the actual file, extending this once new data is added!
class AggregatePRInfo(NamedTuple):
    is_draft: bool
    CI_status: CIStatus
    # The branch this PR is opened against: should be 'master' (for most PRs)
    base_branch: str
    # The name of this PR's branch.
    branch_name: str
    # The repository this PR was opened from: should not be 'leanprover-community',
    # i.e. a PR opened from a fork of mathlib
    head_repo: str
    # 'open' for open PRs, 'closed' for closed PRs
    state: str
    # Github's time when the PR was "last updated"
    last_updated: datetime
    # The PR author's github handle
    author: str
    title: str
    # The full body of the PR description. For mathlib PRs,
    # everything before the first "---" line becomes the final commit message.
    # Information about dependent PRs is noted after the fold.
    description: str
    # The numbers of all PRs which this one directly depends on
    # (as parsed from the PR description).
    direct_dependencies: List[int]
    # All labels assigned to this PR.
    labels: List[Label]
    additions: int
    deletions: int
    # The first 100 modified files.
    modified_files: List[str]
    number_modified_files: int
    # Github handles of all users (if any) approving this
    approvals: List[str]
    # The github handles of all users (if any) assigned to this PR
    assignees: List[str]
    # All users who left a standard or review comment on this PR: could include
    # bots posting, and the PR author (aside from the description).
    users_commented: Tuple[DataStatus, List[str]]
    # This field is *not* present if there is only "basic" information about this PR.
    # (Basic PRs contain have the number of standard comments, but of review comments.)
    number_total_comments: int | None
    # The following fields are not present when there is only basic information about this PR.
    # They are also missing if a PR's events data is invalid (because github returned bogus results).
    # Otherwise, they include their validity status ("missing", "incomplete", "valid").
    last_status_change: LastStatusChange | None
    first_on_queue: Tuple[DataStatus, datetime | None] | None
    total_queue_time: TotalQueueTime | None


# Missing aggregate information will be replaced by this default item.
PLACEHOLDER_AGGREGATE_INFO = AggregatePRInfo(
    False, CIStatus.Missing, "master", "leanprover-community", "default-branch-name",
    "open", datetime.now(timezone.utc),
    "unknown", "unknown title", "unknown description", [], [], -1, -1, [], -1, [], [], (DataStatus.Missing, []), None, None, None, None,
)

class CustomJSONEncoder:
    """Handles serialization of custom objects to JSON-serializable format"""

    @staticmethod
    def serialize_obj(obj):
        """Convert custom objects to JSON-serializable format with type information"""

        # Handle NamedTuples (including your custom ones)
        if hasattr(obj, "_asdict") and hasattr(obj, "_fields"):
            return {
                "__type__": obj.__class__.__name__,
                "__module__": obj.__class__.__module__,
                "__data__": OrderedDict(
                    (field, CustomJSONEncoder.serialize_obj(getattr(obj, field)))
                    for field in obj._fields
                )
            }

        # Handle StrEnum and other enums
        elif isinstance(obj, StrEnum):
            return {
                "__type__": obj.__class__.__name__,
                "__module__": obj.__class__.__module__,
                "__value__": obj.value
            }

        # Handle datetime objects
        elif isinstance(obj, datetime):
            return {
                "__type__": "datetime",
                "__value__": obj.isoformat()
            }

        # Handle timedelta objects
        elif isinstance(obj, timedelta):
            return {
                "__type__": "timedelta",
                "__total_seconds__": obj.total_seconds()
            }

        # Handle relativedelta objects
        elif isinstance(obj, relativedelta.relativedelta):
            # Only serialize non-None/non-zero attributes
            data = {}
            for attr in ['years', 'months', 'days', 'hours', 'minutes', 'seconds', 'microseconds', 'leapdays',
                        'year', 'month', 'day', 'hour', 'minute', 'second', 'microsecond', 'weekday']:
                value = getattr(obj, attr, None)
                if value is not None and value != 0:
                    data[attr] = value

            return {
                "__type__": "relativedelta",
                "__data__": data
            }

        # Handle strings (don't iterate over them)
        elif isinstance(obj, str):
            return obj

        # Handle dictionaries
        elif hasattr(obj, "keys"):
            return OrderedDict(
                (key, CustomJSONEncoder.serialize_obj(value))
                for key, value in obj.items()
            )

        # Handle tuples (preserve as tuples with type info)
        elif isinstance(obj, tuple):
            return {
                "__type__": "tuple",
                "__data__": [CustomJSONEncoder.serialize_obj(item) for item in obj]
            }

        # Handle other iterables (lists, sets, etc.)
        elif hasattr(obj, "__iter__"):
            return type(obj)(CustomJSONEncoder.serialize_obj(item) for item in obj)

        # Handle None and basic types (int, float, bool)
        else:
            return obj


class CustomJSONDecoder:
    """Handles deserialization from JSON format back to custom objects"""

    def __init__(self, class_registry: Dict[str, type] = None):
        """
        Initialize with a registry of classes that can be reconstructed

        Args:
            class_registry: Dictionary mapping class names to class objects
        """
        self.class_registry = class_registry or {}

        # Add built-in types
        self.class_registry.update({
            'datetime': datetime,
            'timedelta': timedelta,
            'relativedelta': relativedelta.relativedelta,
            'tuple': tuple
        })

    def deserialize_obj(self, obj):
        """Convert JSON data back to custom objects"""

        # Handle dictionaries that might represent custom objects
        if isinstance(obj, dict):
            # Check if this is a serialized custom object
            if "__type__" in obj:
                type_name = obj["__type__"]

                # Handle datetime
                if type_name == "datetime":
                    return datetime.fromisoformat(obj["__value__"])

                # Handle timedelta
                elif type_name == "timedelta":
                    return timedelta(seconds=obj["__total_seconds__"])

                # Handle relativedelta
                elif type_name == "relativedelta":
                    return relativedelta.relativedelta(**obj["__data__"])

                # Handle tuples
                elif type_name == "tuple":
                    return tuple(self.deserialize_obj(item) for item in obj["__data__"])

                # Handle StrEnum and other custom enums
                elif type_name in self.class_registry:
                    target_class = self.class_registry[type_name]

                    # Check if this is an enum (has __value__ instead of __data__)
                    if hasattr(target_class, '_value_') or issubclass(target_class, StrEnum):
                        return target_class(obj["__value__"])

                    # Handle NamedTuples and other classes (has __data__)
                    elif "__data__" in obj:
                        # Recursively deserialize the data
                        deserialized_data = {
                            key: self.deserialize_obj(value)
                            for key, value in obj["__data__"].items()
                        }
                        return target_class(**deserialized_data)

                    else:
                        raise ValueError(f"Cannot deserialize object of type '{type_name}': missing __data__ or __value__")

                else:
                    # Unknown type, return as-is or raise an error
                    print(f"Warning: Unknown type '{type_name}', returning raw data")
                    return obj

            # Regular dictionary
            else:
                return {
                    key: self.deserialize_obj(value)
                    for key, value in obj.items()
                }

        # Handle lists
        elif isinstance(obj, list):
            return [self.deserialize_obj(item) for item in obj]

        # Handle other basic types
        else:
            return obj


def dump_to_json_file(data: Any, filepath: str, indent: int = 2):
    """Save data to JSON file with custom serialization"""
    serialized_data = CustomJSONEncoder.serialize_obj(data)
    with open(filepath, 'w') as f:
        json.dump(serialized_data, f, indent=indent)

# Example registry containing custom classes
DEFAULT_CLASS_REGISTRY = {
        'AggregatePRInfo': AggregatePRInfo,
        'CIStatus': CIStatus,
        'Label': Label,
        'DataStatus': DataStatus,
        'LastStatusChange': LastStatusChange,
        'TotalQueueTime': TotalQueueTime,
        'PRStatus': PRStatus,
        'Dashboard': Dashboard,
        'BasicPRInformation': BasicPRInformation,
    }

def load_from_json_file(filepath: str, class_registry: Dict[str, type] = DEFAULT_CLASS_REGISTRY):
    """Load data from JSON file with custom deserialization"""
    with open(filepath, 'r') as f:
        json_data = json.load(f)

    decoder = CustomJSONDecoder(class_registry)
    return decoder.deserialize_obj(json_data)


# Parse the contents |data| of an aggregate json file into a dictionary pr number -> AggregatePRInfo.
def parse_aggregate_file(data: dict) -> dict[int, AggregatePRInfo]:
    label_colours = data["label_colours"]

    def toLabel(name: str) -> Label:
        url = f"https://github.com/leanprover-community/mathlib4/labels/{name}"
        if name.startswith("t-"):
            colour = label_colours["t-analysis"]
        elif name.startswith("blocked-by"):
            colour = label_colours["blocked-by-other-PR"]
        else:
            colour = label_colours[name]
        return Label(name, colour, url)

    aggregate_info = dict()
    for pr in data["pr_statusses"]:
        date = parser.isoparse(pr["last_updated"])
        label_names = pr["label_names"]
        commenters = pr["commenters"]
        users_commented = (DataStatus.fromStr(commenters["status"]), commenters["users"])
        # Some PRs only have basic information present.
        if "number_review_comments" in pr:
            number_all_comments = pr["number_comments"] + pr["number_review_comments"]
            # If status information is invalid, omit it.
            st = pr["last_status_change"]
            if st["status"] == "missing":
                last_status_change = None
            else:
                (data_status, raw_time, raw_delta, raw_current_status) = st["status"], st["time"], st["delta"], st["current_status"]
                delta = relativedelta_tryParse(raw_delta)
                current_status = PRStatus.tryFrom_str(raw_current_status)
                if delta is None:
                    print(f"error: invalid data, input {raw_delta} for 'delta' field of 'last_status_change' is invalid", file=sys.stderr)
                elif current_status is None:
                    print(f"error: invalid data, input {raw_current_status} for 'current_status' field of 'last_status_change' is invalid", file=sys.stderr)
                last_status_change = LastStatusChange(DataStatus.fromStr(data_status), parser.isoparse(raw_time), delta, current_status)

            foq = pr["first_on_queue"]
            if foq["status"] == "missing":
                first_on_queue = None
            else:
                date2 = None if foq["date"] is None else parser.isoparse(foq["date"])
                first_on_queue = (DataStatus.fromStr(foq["status"]), date2)

            tqt = pr["total_queue_time"]
            if tqt["status"] == "missing":
                total_queue_time = None
            else:
                (data_status, value_td, value_rd, explanation) = (tqt["status"], tqt["value_td"], tqt["value_rd"], tqt["explanation"])
                td = timedelta_tryParse(value_td)
                rd = relativedelta_tryParse(value_rd)
                if rd is None:
                    print(f"error: invalid data, input {rd} for 'value_rd' field of 'total_queue_time' is invalid", file=sys.stderr)
                elif td is None:
                    print(f"error: invalid data, input {td} for 'value_td' field of 'total_queue_time' is invalid", file=sys.stderr)
                total_queue_time = TotalQueueTime(DataStatus.fromStr(data_status), td, rd, explanation)
        else:
            number_all_comments = None
            last_status_change = None
            first_on_queue = None
            total_queue_time = None
        info = AggregatePRInfo(
            pr["is_draft"], CIStatus.from_string(pr["CI_status"]), pr["base_branch"], pr["branch_name"], pr["head_repo"]["login"],
            pr["state"], date, pr["author"], pr["title"], pr["description"], pr["direct_dependencies"], [toLabel(name) for name in label_names],
            pr["additions"], pr["deletions"], pr["files"], pr["num_files"], pr["review_approvals"], pr["assignees"],
            users_commented, number_all_comments, last_status_change, first_on_queue, total_queue_time,
        )
        aggregate_info[pr["number"]] = info
    return aggregate_info


# Compute the status of each PR in a given list. Return a dictionary keyed by the PR number.
# (`BasicPRInformation` is not hashable, hence cannot be used as a dictionary key.)
# 'aggregate_info' contains aggregate information about each PR's CI state,
# draft status and base branch (and more, which we do not use).
# If no detailed information was available for a given PR number, 'None' is returned.
def compute_pr_statusses(aggregate_info: dict[int, AggregatePRInfo], prs: List[BasicPRInformation]) -> dict[int, PRStatus]:
    def determine_status(aggregate_info: AggregatePRInfo) -> PRStatus:
        # Ignore all "other" labels, which are not relevant for this anyway.
        labels = [label_categorisation_rules[lab.name] for lab in aggregate_info.labels if lab.name in label_categorisation_rules]
        from_fork = aggregate_info.head_repo != "leanprover-community"
        state = PRState(labels, aggregate_info.CI_status, aggregate_info.is_draft, from_fork)
        return determine_PR_status(datetime.now(timezone.utc), state)
    return {info.number: determine_status(aggregate_info[info.number] or PLACEHOLDER_AGGREGATE_INFO) for info in prs}


# Does a PR have a given label?
def _has_label(pr: BasicPRInformation, name: str) -> bool:
    return name in [label.name for label in pr.labels]


# Extract all PRs from a given list which have a certain label.
def prs_with_label(prs: List[BasicPRInformation], label_name: str) -> List[BasicPRInformation]:
    return [prinfo for prinfo in prs if _has_label(prinfo, label_name)]


# Extract all PRs from a given list which have any label in a certain list.
def prs_with_any_label(prs: List[BasicPRInformation], label_names: List[str]) -> List[BasicPRInformation]:
    return [prinfo for prinfo in prs if any([_has_label(prinfo, name) for name in label_names])]


# Extract all PRs from a given list which do not have a certain label.
def prs_without_label(prs: List[BasicPRInformation], label_name: str) -> List[BasicPRInformation]:
    return [prinfo for prinfo in prs if not _has_label(prinfo, label_name)]


# Extract all PRs from a given list which do not have any label among a given list.
def prs_without_any_label(prs: List[BasicPRInformation], label_names: List[str]) -> List[BasicPRInformation]:
    return [prinfo for prinfo in prs if all([not _has_label(prinfo, name) for name in label_names])]


# XXX: this method should go into dashboard.py, but cannot as gather_pr_statistics_inner also uses it.

# Compute the HTML code for a link to a particular dashboard |kind|.
# If the link to a dashboard goes to a separate page |subpage|, open the link in a new tab.
# |force_same_page| disables that parameter, i.e. all links always go to the current page.
def link_to(kind: Dashboard, name="these ones", subpage=None, force_same_page=False) -> str:
    if subpage and not force_same_page:
        return f'<a href="{subpage or ""}#{getIdTitle(kind)[0]}">{name}</a>'
    else:
        return f'<a href="#{getIdTitle(kind)[0]}" target="_self">{name}</a>'


# The following logic is mathlib-dependent again.


def infer_pr_url(number: int) -> str:
    return f"https://github.com/leanprover-community/mathlib4/pull/{number}"


# Compute aggregate information about a collection of PRs, including a piechart of the PRs by their status.abs
# Returns a tuple (number_all, details, pie_chart) of
#   - the number of all PRs in the collection
#   - a break-down of these PRs by PR state (formatted as HTML list)
#   - code for generating a pie-chart, inside a complete <div> element.
# If aggregate information about a PR is missing, we treat it as non-draft, failing CI and against 'master'.
# (Though, in fact, we assume that 'aggregate_info' is complete, by prior normalisation.)
def gather_pr_statistics(
    all_pr_statusses: dict[int, PRStatus],
    aggregate_info: dict[int, AggregatePRInfo],
    prs: dict[Dashboard, List[BasicPRInformation]],
    all_ready_prs: List[BasicPRInformation],
    all_draft_prs: List[BasicPRInformation],
    is_triage_board: bool,
) -> Tuple[int, str, str]:
    queue_prs = prs[Dashboard.Queue]
    justmerge_prs = prs[Dashboard.NeedsMerge]

    # Collect the number of PRs in each possible status.
    # NB. The order of these statusses is meaningful; the statistics are shown in the order of these items.
    statusses = [
        PRStatus.AwaitingReview, PRStatus.Blocked, PRStatus.AwaitingAuthor, PRStatus.MergeConflict,
        PRStatus.HelpWanted,PRStatus.NotReady,
        PRStatus.AwaitingDecision,
        # TODO: in August, re-instate reverted
        # PRStatus.FromFork,
        PRStatus.Contradictory,
        PRStatus.Delegated, PRStatus.AwaitingBors,
    ]
    numbers = [str(pr.number) for pr in all_ready_prs]
    ready_pr_status = { n: all_pr_statusses[n] for n in numbers }
    number_prs: Dict[PRStatus, int] = {
        status: len([number for number in ready_pr_status if ready_pr_status[number] == status]) for status in statusses
    }
    number_prs[PRStatus.NotReady] += len(all_draft_prs)
    # Check that we did not miss any variant above
    for status in PRStatus._member_map_.values():
        assert status == PRStatus.Closed or status in number_prs.keys()

    # For some kinds, we have this data already: the review queue and the "not merged" kinds come to mind.
    # Let us compare with the classification logic.
    queue_prs_numbers = [pr for pr in ready_pr_status if ready_pr_status[pr] == PRStatus.AwaitingReview]
    msg = "the review queue (left) with all PRs classified as awaiting review (right)"
    if my_assert_eq(msg, queue_prs_numbers, [i.number for i in queue_prs]):
        print("review queue: two computations match, hooray", file=sys.stderr)
    # TODO: also cross-check the data for merge conflicts
    # add the "needs-decision" status with the awaiting-zulip dashboard

    number_all = len(all_ready_prs) + len(all_draft_prs)

    def number_percent(n: int, total: int, color: str = "") -> str:
        if color:
            return f'{n} (<span style="color: {color};">{n/total:.1%}</span>)'
        else:
            return f"{n} (<span>{n/total:.1%}</span>)"

    instatus = {
        PRStatus.AwaitingReview: f"are awaiting review ({link_to(Dashboard.Queue, subpage='review_dashboard.html', force_same_page=is_triage_board)}), among these <b>{number_percent(len(prs[Dashboard.QueueNewContributor]), len(queue_prs))}</b> by new contributors ({link_to(Dashboard.QueueNewContributor, subpage='review_dashboard.html')})",
        PRStatus.HelpWanted: f"are labelled help-wanted or please-adopt ({link_to(Dashboard.NeedsHelp, 'roughly these', 'help_out.html', is_triage_board)})",
        PRStatus.AwaitingAuthor: "are awaiting the PR author's action",
        PRStatus.AwaitingDecision: f"are awaiting the outcome of a zulip discussion ({link_to(Dashboard.NeedsDecision)})",
        # TODO: in August, re-instate reverted
        # PRStatus.FromFork: f"are opened from a fork of mathlib ({link_to(Dashboard.FromFork)})",
        PRStatus.Blocked: "are blocked on another PR",
        PRStatus.Delegated: f"are delegated (stale ones are {link_to(Dashboard.StaleDelegated, 'here', 'help_out.html', is_triage_board)})",
        PRStatus.AwaitingBors: f"have been sent to bors (stale ones are {link_to(Dashboard.StaleReadyToMerge, 'here', 'maintainers_quick.html', is_triage_board)})",
        PRStatus.MergeConflict: f"have a merge conflict: among these, <b>{number_percent(len(justmerge_prs), number_prs[PRStatus.MergeConflict])}</b> would be ready for review otherwise: {link_to(Dashboard.NeedsMerge, 'these')}",
        PRStatus.Contradictory: f"have contradictory labels ({link_to(Dashboard.ContradictoryLabels)})",
        PRStatus.NotReady: "are marked as draft or work in progress",
    }
    assert set(instatus.keys()) == set(statusses)
    color = {
        PRStatus.AwaitingReview: "#33b4ec",
        PRStatus.HelpWanted: "#cc317c",
        PRStatus.AwaitingAuthor: "#f6ae9a",
        PRStatus.AwaitingDecision: "#086ad4",
        # PRStatus.FromFork: "#FF8000",
        PRStatus.Blocked: "#8A6A1C",
        PRStatus.Delegated: "#689dea",
        PRStatus.AwaitingBors: "#098306",
        PRStatus.MergeConflict: "#f17075",
        PRStatus.Contradictory: "black",
        PRStatus.NotReady: "#e899cd",
    }
    assert set(color.keys()) == set(statusses)
    details = "\n".join([f"  <li><b>{number_percent(number_prs[s], number_all, color[s])}</b> {instatus[s]}</li>" for s in statusses])
    # Generate a simple pie chart showing the distribution of PR statusses.
    # Doing so requires knowing the cumulative sums, of all statusses so far.
    numbers = [number_prs[s] for s in statusses]
    cumulative = [sum(numbers[: i + 1]) for i in range(len(numbers))]
    piechart = ", ".join([f"{color[s]} 0 {cumulative[i] * 360 // number_all}deg" for (i, s) in enumerate(statusses)])
    piechart_style = f"width: 200px;height: 200px;border-radius: 50%;border: 1px solid black;background-image: conic-gradient( {piechart} );"
    if cumulative[-1] != number_all:
        dict = "{" + ", ".join([f"{s}: {number_prs[s]}" for s in statusses]) + "}"
        msg = f"""error: statistics calculation is inconsistent, all PR kinds do not add up!
            cumulative sum of PRs is {cumulative[-1]}, while there are {number_all} PRs in total
            detailed stats dictionary is {dict}"""
        print(msg, file=sys.stderr)
        # TODO: compare these lists of PRs in detail, to verify if this exposes anything other than outdated data!
        # assert False

    return (number_all, f"<ul>\n{details}\n</ul>", f'<div class="piechart" style="{piechart_style}"></div>')


def has_contradictory_labels(pr: BasicPRInformation) -> bool:
    # Combine common labels.
    canonicalise = {
        "ready-to-merge": "bors", "auto-merge-after-CI": "bors",
        "blocked-by-other-PR": "blocked", "blocked-by-core-PR": "blocked", "blocked-by-batt-PR": "blocked", "blocked-by-qq-PR": "blocked",
    }
    normalised_labels = [(canonicalise[label.name] if label.name in canonicalise else label.name) for label in pr.labels]
    # Test for contradictory label combinations.
    if "awaiting-review-DONT-USE" in normalised_labels:
        return True
    elif "bors" in normalised_labels and ("awaiting-author" in normalised_labels or "awaiting-zulip" in normalised_labels or "WIP" in normalised_labels):
        return True
    elif "WIP" in normalised_labels and "awaiting-review" in normalised_labels:
        return True
    return False


# Determine all PRs in `prs` which are not labelled `WIP` and
# - are feature PRs without a topic label,
# - have a badly formatted title (we currently only check some of the conditions in the guidelines),
# - have contradictory labels.
def compute_dashboards_bad_labels_title(
    prs: List[BasicPRInformation],
) -> Tuple[List[BasicPRInformation], List[BasicPRInformation], List[BasicPRInformation]]:
    # Filter out all PRs which have a WIP label.
    nonwip_prs = prs_without_label(prs, "WIP")
    with_bad_title = [pr for pr in nonwip_prs if not pr.title.startswith(("feat", "chore", "perf", "refactor", "style", "fix", "doc"))]

    # Whether a PR has a "topic" label.
    def has_topic_label(pr: BasicPRInformation) -> bool:
        topic_labels = [label for label in pr.labels if label.name in ["CI", "IMO"] or label.name.startswith("t-")]
        return len(topic_labels) >= 1

    prs_without_topic_label = [pr for pr in nonwip_prs if pr.title.startswith("feat") and not has_topic_label(pr)]
    prs_with_contradictory_labels = [pr for pr in nonwip_prs if has_contradictory_labels(pr)]
    return (with_bad_title, prs_without_topic_label, prs_with_contradictory_labels)


# use_aggregate_queue: if True, determine the review queue (and everything depending on it)
# from the aggregate data and not queue.json
def determine_pr_dashboards(
    all_open_prs: List[BasicPRInformation],
    nondraft_PRs: List[BasicPRInformation],
    base_branch: dict[int, str],
    # TODO(August/September): re-instate this table, with inverted meaning prs_from_fork: List[BasicPRInformation],
    CI_status: dict[int, CIStatus],
    aggregate_info: dict[int, AggregatePRInfo],
    use_aggregate_queue: bool,
) -> dict[Dashboard, List[BasicPRInformation]]:
    approved = [pr for pr in nondraft_PRs if aggregate_info[pr.number].approvals]
    prs_to_list: dict[Dashboard, List[BasicPRInformation]] = dict()
    prs_to_list[Dashboard.All] = all_open_prs
    # The 'tech debt' and 'other base' boards are obtained
    # from filtering the list of all non-draft PRs (without the WIP label).
    all_ready_prs = prs_without_label(nondraft_PRs, "WIP")
    prs_to_list[Dashboard.TechDebt] = prs_with_any_label(all_ready_prs, ["tech debt", "longest-pole"])
    prs_to_list[Dashboard.OtherBase] = [pr for pr in nondraft_PRs if base_branch[pr.number] != "master"]
    # TODO: in August, re-instate reverted
    # prs_to_list[Dashboard.FromFork] = prs_from_fork

    prs_to_list[Dashboard.NeedsHelp] = prs_with_any_label(nondraft_PRs, ["help-wanted", "please_adopt"])
    prs_to_list[Dashboard.NeedsDecision] = prs_with_label(nondraft_PRs, "awaiting-zulip")

    # Compute all PRs on the review queue (and well as several sub-filters).
    # The review queue consists of all PRs against the master branch, with passing CI,
    # that are not in draft state and not labelled WIP, help-wanted or please-adopt,
    # and have none of the other labels below.
    master_prs_with_CI = [pr for pr in nondraft_PRs if base_branch[pr.number] == "master" and (CI_status[pr.number] == CIStatus.Pass)]
    other_labels = [
        # XXX: does the #queue check for all of these labels?
        "blocked-by-other-PR",
        "blocked-by-core-PR",
        "blocked-by-batt-PR",
        "blocked-by-qq-PR",
        "awaiting-CI",
        "awaiting-author",
        "awaiting-zulip",
        "please-adopt",
        "help-wanted",
        "WIP",
        "delegated",
        "auto-merge-after-CI",
        "ready-to-merge",
    ]
    queue_or_merge_conflict = prs_without_any_label(master_prs_with_CI, other_labels)
    prs_to_list[Dashboard.NeedsMerge] = prs_with_label(queue_or_merge_conflict, "merge-conflict")
    queue_prs = prs_without_label(queue_or_merge_conflict, "merge-conflict")

    interesting_CI = [pr for pr in nondraft_PRs if CI_status[pr.number] == CIStatus.FailInessential]
    foo = [pr for pr in interesting_CI if base_branch[pr.number] == "master"]
    prs_to_list[Dashboard.InessentialCIFails] = prs_without_any_label(foo, other_labels + ["merge-conflict"])

    queue_prs2 = None
    with open("queue.json", "r") as queuefile:
        queue_prs2 = _extract_prs(json.load(queuefile))
        queue_pr_numbers2 = [pr.number for pr in queue_prs2]
    msg = "comparing this page's review dashboard (left) with the Github #queue (right)"
    if my_assert_eq(msg, [pr.number for pr in queue_prs], queue_pr_numbers2):
        print("Review dashboard and #queue match, hooray!", file=sys.stderr)

    prs_to_list[Dashboard.Queue] = queue_prs if use_aggregate_queue else queue_prs2
    queue = prs_to_list[Dashboard.Queue]
    prs_to_list[Dashboard.QueueNewContributor] = prs_with_label(queue, "new-contributor")
    prs_to_list[Dashboard.QueueEasy] = prs_with_label(queue, "easy")
    prs_to_list[Dashboard.QueueTechDebt] = prs_with_any_label(queue, ["tech debt", "longest-pole"])

    a_day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    a_week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    two_weeks_ago = datetime.now(timezone.utc) - timedelta(days=14)
    one_day_stale = [pr for pr in nondraft_PRs if aggregate_info[pr.number].last_updated < a_day_ago]
    one_week_stale = [pr for pr in nondraft_PRs if aggregate_info[pr.number].last_updated < a_week_ago]
    prs_to_list[Dashboard.AllReadyToMerge] = prs_with_any_label(nondraft_PRs, ["ready-to-merge", "auto-merge-after-CI"])
    prs_to_list[Dashboard.StaleReadyToMerge] = prs_with_any_label(one_day_stale, ["ready-to-merge", "auto-merge-after-CI"])
    prs_to_list[Dashboard.StaleDelegated] = prs_with_label(one_day_stale, "delegated")
    mm_prs = prs_with_label(one_day_stale, "maintainer-merge")
    prs_to_list[Dashboard.StaleMaintainerMerge] = prs_without_label(mm_prs, "ready-to-merge")
    prs_to_list[Dashboard.AllMaintainerMerge] = prs_without_label(prs_with_label(nondraft_PRs, "maintainer-merge"), "ready-to-merge")
    prs_to_list[Dashboard.StaleNewContributor] = prs_with_label(one_week_stale, "new-contributor")

    stale_queue = []
    very_stale_queue = []
    for pr in queue:
        last_real_update = aggregate_info[pr.number].last_status_change
        if last_real_update is not None and last_real_update.time < two_weeks_ago:
            very_stale_queue.append(pr)
        if last_real_update is not None and last_real_update.time < a_week_ago:
            stale_queue.append(pr)
    prs_to_list[Dashboard.QueueStaleUnassigned] = [pr for pr in stale_queue if not aggregate_info[pr.number].assignees]
    prs_to_list[Dashboard.QueueStaleAssigned] = [pr for pr in very_stale_queue if aggregate_info[pr.number].assignees]

    (bad_title, unlabelled, contradictory) = compute_dashboards_bad_labels_title(nondraft_PRs)
    prs_to_list[Dashboard.BadTitle] = bad_title
    prs_to_list[Dashboard.Unlabelled] = unlabelled
    prs_to_list[Dashboard.ContradictoryLabels] = contradictory
    prs_to_list[Dashboard.Approved] = approved
    return prs_to_list
