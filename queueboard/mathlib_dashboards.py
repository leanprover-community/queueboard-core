#!/usr/bin/env python3

"""
This file contains the definitions of all the PR dashboards for mathlib's queueboard.


This makes it very mathlib-specific by definition.
FUTURE: make this even more declarative, e.g. specified by a configuration file?
"""

from enum import Enum, auto, unique
from typing import Tuple


@unique
class Dashboard(Enum):
    """The different kind of dashboards on the created triage webpage"""

    # Note: the tables on the generated page are listed in the order of these variants.
    Queue = 0
    QueueNewContributor = auto()
    QueueEasy = auto()
    # All PRs on the queue which are unassigned and have not been updated in the past week.
    # We use the real last update, not github's date.
    QueueStaleUnassigned = auto()
    # All assigned PRs on the review queue without any update in the past two weeks.
    # TODO: use a more refined measure of activity, such as "no comment/review comment by anybody but the author"
    QueueStaleAssigned = auto()
    # All PRs labelled "tech-debt" or "longest-pole"
    QueueTechDebt = auto()
    # All PRs labelled ready-to-merge or auto-merge-after-CI, not just the stale ones
    AllReadyToMerge = auto()
    StaleReadyToMerge = auto()
    StaleDelegated = auto()
    StaleMaintainerMerge = auto()
    # All PRs labelled maintainer-merge, not the stale ones.
    AllMaintainerMerge = auto()
    # All ready PRs (not draft, not labelled WIP) labelled with "tech debt" or "longest-pole".
    TechDebt = auto()
    # This PR is blocked on a zulip discussion or similar.
    NeedsDecision = auto()
    # PRs passes, but just has a merge conflict: same labels as for review, except we do require a merge conflict
    NeedsMerge = auto()
    # PR would be ready for review, except for a failure of some infrastructure-related CI job:
    # unless this CI modifies some mathlib infrastructure, this is not this PRs fault.
    InessentialCIFails = auto()
    StaleNewContributor = auto()
    # Labelled please-adopt or help-wanted
    NeedsHelp = auto()
    # Non-draft PRs into some branch other than mathlib's master branch
    OtherBase = auto()
    # TODO: in August, invert and re-instate this dashboard (i.e., showing all non-draft PRs opened from the mathlib repo itself)
    # # Non-draft PRs opened from a fork
    # FromFork = auto()
    # "Ready" PRs whose title does not start with an abbreviation like 'feat' or 'style'
    BadTitle = auto()
    # "Ready" PRs without the CI or a t-something label.
    Unlabelled = auto()
    # This PR carries inconsistent labels, such as "WIP" and "ready-to-merge".
    ContradictoryLabels = auto()
    # PRs with at least one "approved" review by a community member.
    Approved = auto()
    # Every open PR in mathlib.
    All = auto()


def short_description(kind: Dashboard) -> str:
    """Describe what the table 'kind' contains, for use in a "there are no such PRs" message."""
    return {
        Dashboard.Queue: "PRs on the review queue",
        Dashboard.QueueNewContributor: "PRs by new mathlib contributors on the review queue",
        Dashboard.QueueEasy: "PRs on the review queue which are labelled 'easy'",
        Dashboard.QueueTechDebt: "PRs on the review queue which are labelled 'tech debt' or 'longest-pole",
        Dashboard.QueueStaleAssigned: "assigned PRs on the review queue without activity in the past two weeks",
        Dashboard.QueueStaleUnassigned: "unassigned PRs on the review queue with no meaningful activity in the past week",
        Dashboard.StaleMaintainerMerge: "stale PRs labelled maintainer merge",
        Dashboard.AllMaintainerMerge: "PRs labelled maintainer merge",
        Dashboard.StaleDelegated: "stale delegated PRs",
        Dashboard.AllReadyToMerge: "all PRs labelled auto-merge-after-CI or ready-to-merge",
        Dashboard.StaleReadyToMerge: "stale PRs labelled auto-merge-after-CI or ready-to-merge",
        Dashboard.TechDebt: "ready PRs labelled with 'tech debt' or 'longest-pole'",
        Dashboard.NeedsDecision: "PRs blocked on a zulip discussion or similar",
        Dashboard.NeedsMerge: "PRs which just have a merge conflict",
        Dashboard.InessentialCIFails: "PRs with just an infrastructure-related CI failure",
        Dashboard.StaleNewContributor: "stale PRs by new contributors",
        Dashboard.NeedsHelp: "PRs which are looking for a help",
        Dashboard.OtherBase: "ready PRs into a non-master branch",
        # TODO: in August, re-instate reverted
        # Dashboard.FromFork: "ready PRs opened from a fork of mathlib",
        Dashboard.Unlabelled: "ready PRs without a 'CI' or 't-something' label",
        Dashboard.BadTitle: "ready PRs whose title does not start with an abbreviation like 'feat', 'style' or 'perf'",
        Dashboard.ContradictoryLabels: "PRs with contradictory labels",
        Dashboard.Approved: "PRs that have an 'approved' review",
        Dashboard.All: "open PRs",
    }[kind]


def long_description(kind: Dashboard) -> str:
    """Explain what each dashboard contains: full description, for the purposes of a sub-title
    to the full PR table. This description should not be capitalised."""
    notupdated = "which have not been updated in the past"
    return {
        Dashboard.Queue: "all PRs which are ready for review: CI passes, no merge conflict and not blocked on other PRs",
        Dashboard.QueueNewContributor: "all PRs by new contributors which are ready for review",
        Dashboard.QueueEasy: "all PRs labelled 'easy' which are ready for review",
        Dashboard.QueueTechDebt: "all PRs labelled with 'tech debt' or 'longest-pole' which are ready for review",
        Dashboard.QueueStaleAssigned: "all assigned PRs on the review queue which have not been updated at all in the past two weeks",
        Dashboard.QueueStaleUnassigned: "all PRs on the review queue which are unassigned and have not seen status changes in the past two weeks",
        Dashboard.NeedsMerge: "all PRs which have a merge conflict, but otherwise fit the review queue",
        Dashboard.InessentialCIFails: "all PRs with just a failure of some infrastructure-related CI job (usually not this PR's fault), but are otherwise ready for review",
        Dashboard.StaleDelegated: f"all PRs labelled 'delegated' {notupdated} 24 hours",
        Dashboard.AllReadyToMerge: "all PRs labelled 'auto-merge-after-CI' or 'ready-to-merge'",
        Dashboard.StaleReadyToMerge: f"all PRs labelled 'auto-merge-after-CI' or 'ready-to-merge' {notupdated} 24 hours",
        Dashboard.TechDebt: "all 'ready' PRs (not draft, not labelled WIP) labelled with 'tech debt' or 'longest-pole'",
        Dashboard.NeedsDecision: "all PRs labelled 'awaiting-zulip': these are blocked on a zulip discussion or similar",
        Dashboard.StaleMaintainerMerge: f"all PRs labelled 'maintainer-merge' but not 'ready-to-merge' {notupdated} 24 hours",
        Dashboard.AllMaintainerMerge: "all PRs labelled maintainer merge but not 'ready-to-merge'",
        Dashboard.NeedsHelp: "all PRs which are labelled 'please-adopt' or 'help-wanted'",
        Dashboard.OtherBase: "all non-draft PRs, not labelled WIP, into some branch other than mathlib's master branch",
        # TODO: in August, re-instate reverted
        # Dashboard.FromFork: "all non-draft PRs, not labelled WIP, opened from a fork of mathlib",
        Dashboard.StaleNewContributor: f"all PR labelled 'new-contributor' {notupdated} 7 days",
        Dashboard.Unlabelled: "all PRs without draft status or 'WIP' label without a 'CI' or 't-something' label",
        Dashboard.BadTitle: "all PRs without draft status or 'WIP' label whose title does not start with an abbreviation like 'feat', 'style' or 'perf'",
        Dashboard.ContradictoryLabels: "PRs whose labels are contradictory, such as 'WIP' and 'ready-to-merge'",
        Dashboard.Approved: "PRs that have at least one 'approved' review by a community member",
        Dashboard.All: "all open PRs",
    }[kind]


def getIdTitle(kind: Dashboard) -> Tuple[str, str]:
    """Return a tuple (id, title) of the HTML anchor ID and a section name for the table
    describing this PR kind."""
    return {
        Dashboard.Queue: ("queue", "Review queue"),
        Dashboard.QueueNewContributor: (
            "queue-new-contributors",
            "New contributors' PRs on the review queue",
        ),
        Dashboard.QueueEasy: ("queue-easy", "PRs on the review queue labelled 'easy'"),
        Dashboard.QueueTechDebt: ("queue-tech-debt", "PRs on the review queue labelled 'tech debt' or 'longest-pole'"),
        Dashboard.QueueStaleAssigned: ("queue-stale-assigned", "Stale assigned PRs on the review queue"),
        Dashboard.QueueStaleUnassigned: ("queue-stale-unassigned", "Stale unassigned PRs on the review queue"),
        Dashboard.StaleDelegated: ("stale-delegated", "Stale delegated PRs"),
        Dashboard.StaleNewContributor: (
            "stale-new-contributor",
            "Stale new contributor PRs",
        ),
        Dashboard.StaleMaintainerMerge: (
            "stale-maintainer-merge",
            "Stale maintainer-merge'd PRs",
        ),
        Dashboard.AllMaintainerMerge: ("all-maintainer-merge", "All maintainer merge'd PRs"),
        Dashboard.AllReadyToMerge: ("all-ready-to-merge", "All ready-to-merge'd PRs"),
        Dashboard.StaleReadyToMerge: (
            "stale-ready-to-merge",
            "Stale ready-to-merge'd PRs",
        ),
        Dashboard.TechDebt: ("tech-debt", "PRs addressing technical debt"),
        Dashboard.NeedsDecision: (
            "needs-decision",
            "PRs blocked on a zulip discussion",
        ),
        Dashboard.NeedsMerge: ("needs-merge", "PRs with just a merge conflict"),
        Dashboard.InessentialCIFails: ("inessential-CI-fails", "PRs with just failing CI, but only often-spurious jobs"),
        Dashboard.NeedsHelp: ("needs-owner", "PRs looking for help"),
        Dashboard.OtherBase: ("other-base", "PRs not into the master branch"),
        # TODO: in August, re-instate reverted
        # Dashboard.FromFork: ("from-fork", "PRs from a fork of mathlib"),
        Dashboard.Unlabelled: ("unlabelled", "PRs without an area label"),
        Dashboard.BadTitle: ("bad-title", "PRs with non-conforming titles"),
        Dashboard.ContradictoryLabels: (
            "contradictory-labels",
            "PRs with contradictory labels",
        ),
        Dashboard.Approved: ("approved", "PRs with an 'approved' review"),
        Dashboard.All: ("all", "All open PRs")
    }[kind]


def getTableId(kind: Dashboard) -> str:
    return f"t-{getIdTitle(kind)[0]}"
