"""
# Computing a better estimate how long a PR was been awaiting review.

This code is rather project-agnostic, *provided* the definitions in classify_pr_state.py
have been updated to match the current project. More precisely, the methods
|first_on_queue_inner| and |total_queue_time_inner| assume there is a variant PRStatus.AwaitingReview
(which is a rather mild assumption). If a project prefers naming this differently,
they should adjust the variant name.

**Problem.** We would like a way to track the progress of PRs, and especially learn which PRs
have been waiting long for review. Currently, we have no good way of obtaining this information:
we use the crude heuristic of
"this PR was last updated X ago, and is awaiting review now" as a metric for "waiting for time X".

That metric is imperfect because
- not everything "updating" a PR is meaningful for our purposes
If somebody edits the PR description to describe the change better
or tweaks the code to make it better understood --- without other activity
and not in response to feedback --- that is a good thing,
but does not change the PR's review status.
- a PR's time on the review queue is often interrupted by having merge conflicts.
This is usually only a temporary state, but means long streaks of no changes are much more rare.
In particular, this disadvantages conflict-prone PRs.

**A better metric** would be to track the PRs state over time, and compute
e.g. the total amount of time this PR was awaiting review.
This is what we attempt to do.

## Input data
This algorithm process a sequence of events "on X, this PR changed in this and that way"
and returns a list of all times when the PRs state changed:
for the purposes of our analysis, this could be "a PR became blocked on another PR",
"a PR became unblocked", "a PR was marked as waiting on author", "a PR incurred a merge conflict".

From this information, we can compute the total time a PR was waiting for review, for instance.

## Implementation notes

This file contains the *analysis* of the given input data; the input data parsing
is done elsewhere.
"""

from datetime import datetime, timedelta, timezone
from typing import List, NamedTuple, Tuple

from dateutil import parser
from dateutil.relativedelta import relativedelta

from queueboard.ci_status import CIStatus
from queueboard.classify_pr_state import PRState, PRStatus, canonicalise_label, determine_PR_status, label_categorisation_rules
from queueboard.util import format_delta


class LabelAdded(NamedTuple):
    """A new label got added"""

    name: str


class LabelRemoved(NamedTuple):
    """An existing label got removed"""

    name: str


class LabelAddedRemoved(NamedTuple):
    """A set of labels was added, and some set of labels was removed.
    Note that a given label can be added and removed at the same time."""

    added: List[str]
    removed: List[str]


class CIStatusChanged(NamedTuple):
    """This PR's CI status changed"""

    new_status: CIStatus


class MarkedDraft(NamedTuple):
    """This PR was marked as draft."""

    pass


class MarkedReady(NamedTuple):
    """This PR was marked as ready for review."""

    pass


# Something changed on a PR which we care about:
# - a new label got added or removed
# - the PR was (un)marked draft
# - the PR's CI status changed (e.g., it passes or fails now)
PRChange = LabelAdded | LabelRemoved | LabelAddedRemoved | MarkedDraft | MarkedReady | CIStatusChanged


# Something changed on this PR, at a given time.
class Event(NamedTuple):
    time: datetime
    change: PRChange

    @staticmethod
    def add_label(time: datetime, name: str):
        return Event(time, LabelAdded(name))

    @staticmethod
    def remove_label(time: datetime, name: str):
        return Event(time, LabelRemoved(name))

    @staticmethod
    def add_remove_labels(time: datetime, added: List[str], removed: List[str]):
        return Event(time, LabelAddedRemoved(added, removed))

    @staticmethod
    def draft(time: datetime):
        return Event(time, MarkedDraft())

    @staticmethod
    def undraft(time: datetime):
        return Event(time, MarkedReady())

    @staticmethod
    def update_ci_status(time: datetime, new: CIStatus):
        return Event(time, CIStatusChanged(new))


# Update the current PR state in light of some `Event`.
def update_state(current: PRState, ev: Event) -> PRState:
    match ev.change:
        case MarkedDraft():
            return PRState(current.labels, current.ci, True, current.from_fork)
        case MarkedReady():
            return PRState(current.labels, current.ci, False, current.from_fork)
        case CIStatusChanged(new_state):
            return PRState(current.labels, new_state, current.draft, current.from_fork)
        case LabelAdded(name):
            # Depending on the label added, update the PR status. We ignore irrelevant labels.
            if name in label_categorisation_rules:
                label_kind = label_categorisation_rules[name]
                return PRState(current.labels + [label_kind], current.ci, current.draft, current.from_fork)
            else:
                # Adding an irrelevant label does not change the PR status.
                if not name.startswith("t-") and name != "CI":
                    pass  # print(f"found irrelevant label: {name}")
                return current
        case LabelRemoved(name):
            if name in label_categorisation_rules:
                # NB: make sure to *copy* current.labels using [:], otherwise that state is also modified!
                new_labels = current.labels[:]
                new_labels.remove(label_categorisation_rules[name])
                return PRState(new_labels, current.ci, current.draft, current.from_fork)
            else:
                # Removing an irrelevant label does not change the PR status.
                return current
        case LabelAddedRemoved(added, removed):
            # Remove any label which is both added and removed, and filter out irrelevant labels.
            both = set(added) & set(removed)
            added = [lab for lab in added if lab in label_categorisation_rules and lab not in both]
            removed = [lab for lab in removed if lab in label_categorisation_rules and lab not in both]
            # Any remaining labels to be removed should exist.
            new_labels = current.labels[:]
            for r in removed:
                if r not in current.labels:
                    print(f"warning: label {r} is supposedly removed twice")
                    continue
                new_labels.remove(label_categorisation_rules[r])
            return PRState(
                new_labels + [label_categorisation_rules[lab] for lab in added], current.ci, current.draft, current.from_fork
            )
        case _:
            print(f"unhandled event: {ev.change}")
            assert False


# Determine the evolution of this PR's state over time, starting from a given state at some time.
# Return a list of pairs (timestamp, s), where this PR moved into state *s* at time *timestamp*.
# The first item corresponds to the PR's creation.
def determine_state_changes(
    creation_time: datetime, initial_state: PRState, events: List[Event]
) -> List[Tuple[datetime, PRState]]:
    result = []
    result.append((creation_time, initial_state))
    curr_state = initial_state
    for event in events:
        new_state = update_state(curr_state, event)
        result.append((event.time, new_state))
        curr_state = new_state
    return result


# Determine the evolution of this PR's status over time.
# Return a list of pairs (timestamp, st), where this PR moved into status *st* at time *timestamp*.
# The first item corresponds to the PR's creation.
def determine_status_changes(
    initial_time: datetime, initial_state: PRState, events: List[Event]
) -> List[Tuple[datetime, PRStatus]]:
    evolution = determine_state_changes(initial_time, initial_state, events)
    res = []
    for time, state in evolution:
        res.append((time, determine_PR_status(time, state)))
    return res


########### Overall computation #########


def total_time_in_status(
    creation_time: datetime, now: datetime, initial_state: PRState, events: List[Event], status: PRStatus
) -> Tuple[Tuple[timedelta, relativedelta], str]:
    """Determine the total amount of time this PR was in a given status,
    from its creation to the current time.

    Returns a tuple (time, description), where
    - description lists the times in that state in human-readable form, and
    - time is a tuple (td, rd), containing the total time in this state,
      once as a timedelta (i.e. only knowing days, not e.g. months) and
      once as a relativedelta. The former is more useful for comparing time spans,
      the latter provides nicer output for users."""
    explanation = ""
    total_rd = relativedelta(days=0)
    total_td = timedelta(days=0)
    evolution_status = determine_status_changes(creation_time, initial_state, events)
    # The PR creation should be the first event in `evolution_status`.
    assert len(evolution_status) == len(events) + 1
    for i in range(len(evolution_status) - 1):
        (old_time, old_status) = evolution_status[i]
        (new_time, _new_status) = evolution_status[i + 1]
        if old_status == status:
            explanation += f"from {old_time} to {new_time} ({format_delta(relativedelta(new_time, old_time))})\n"
            total_rd += new_time - old_time
            total_td += new_time - old_time
    (last, last_status) = evolution_status[-1]
    if last_status == status:
        total_rd += now - last
        total_td += now - last
        explanation += f"since {last} ({format_delta(relativedelta(now, last))})\n"
    return ((total_td, total_rd), explanation.rstrip().replace("+00:00", ""))


class Metadata(NamedTuple):
    """All necessary input data for analysing a PR's state evolution: its creation time, initial state
    and all relevant changes to its state over time."""

    created_at: datetime
    events: List[Event]
    created_as_draft: bool
    from_fork: bool


# Determine the total amount of time this PR was in a given status.
def total_time_in_status_inner(
    now: datetime, metadata: Metadata, status: PRStatus
) -> Tuple[Tuple[timedelta, relativedelta], str]:
    # We assume the PR was created in passing state without labels.
    initial_state = PRState([], CIStatus.Pass, metadata.created_as_draft, metadata.from_fork)
    return total_time_in_status(metadata.created_at, now, initial_state, metadata.events, status)


# Determine the total amount of time this PR was awaiting review.
#
# NB. This method is slightly mathlib-specific: it assumes there is a PRStatus variant "AwaitingReview"
# (which seems broadly reasonable: other projects might want to name this variant differently,
# but will presumably want to have this.)
def total_queue_time_inner(now: datetime, metadata: Metadata) -> Tuple[Tuple[timedelta, relativedelta], str]:
    return total_time_in_status_inner(now, metadata, PRStatus.AwaitingReview)


# Determine the first point in time a PR was in a given status; return None if this never happened so far.
def first_in_status_inner(metadata, status: PRStatus) -> datetime | None:
    # We assume the PR was created in passing state without labels.
    initial_state = PRState([], CIStatus.Pass, metadata.created_as_draft, metadata.from_fork)
    evolution_status = determine_status_changes(metadata.created_at, initial_state, metadata.events)
    # The first state in |evolution_status| is the initial state.
    # If a label was added "immediately", we do not count this state.
    if len(evolution_status) > 1:
        if evolution_status[0][0] == evolution_status[1][0]:
            _ = evolution_status.pop(0)
    for time, estatus in evolution_status:
        if estatus == status:
            return time
    return None


# Determine the first point in time a PR was on the review queue; return None if this never happened so far.
#
# NB. This method is slightly mathlib-specific: it assumes there is a PRStatus variant "AwaitingReview"
# (which seems broadly reasonable: other projects might want to name this variant differently,
# but will presumably want to have this.)
def first_on_queue_inner(metadata) -> datetime | None:
    return first_in_status_inner(metadata, PRStatus.AwaitingReview)


# Return the total time since this PR's last status change,
# as a tuple (absolute time, time since now).
def last_status_update_inner(now: datetime, metadata: Metadata) -> Tuple[datetime, relativedelta, PRStatus]:
    """Compute the total time since this PR's state changed last."""
    # We assume the PR was created in passing state without labels.
    initial_state = PRState([], CIStatus.Pass, metadata.created_as_draft, metadata.from_fork)
    # FUTURE: should this ignore short-lived merge conflicts? for now, it does not
    evolution_status = determine_status_changes(metadata.created_at, initial_state, metadata.events)
    # The PR creation should be the first event in `evolution_status`.
    assert len(evolution_status) == len(metadata.events) + 1
    last: datetime = evolution_status[-1][0]
    return (last, relativedelta(now, last), evolution_status[-1][1])


# Parse the detailed information about a given PR and return a pair
# (creation_data, relevant_events) of the PR's creation date (in UTC time)
# and all relevant events which change a PR's state.
def parse_data(data: dict) -> Tuple[datetime, List[Event]]:
    creation_time = parser.isoparse(data["data"]["repository"]["pullRequest"]["createdAt"])
    events = []
    events_data = data["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"]
    known_irrelevant = [
        "ClosedEvent",
        "ReopenedEvent",
        "BaseRefChangedEvent",
        "HeadRefForcePushedEvent",
        "HeadRefDeletedEvent",
        "PullRequestCommit",
        "IssueComment",
        "PullRequestReview",
        "RenamedTitleEvent",
        "AssignedEvent",
        "UnassignedEvent",
        "ReferencedEvent",
        "CrossReferencedEvent",
        "MentionedEvent",
        "ReviewRequestedEvent",
        "ReviewRequestRemovedEvent",
        "ReviewDismissedEvent",
        "ConnectedEvent",
        "DisconnectedEvent",  # no idea what these are used for
        "SubscribedEvent",
        "UnsubscribedEvent",
        "CommentDeletedEvent",
        "MergedEvent",
        "BaseRefForcePushedEvent",
        "MarkedAsDuplicateEvent",
        "PullRequestRevisionMarker",
        "BaseRefDeletedEvent",
        "HeadRefRestoredEvent",
        "AddedToMergeQueueEvent",
        "RemovedFromMergeQueueEvent",
        "LockedEvent",
        "UnlockedEvent",
        "AutoMergeEnabledEvent",
        "AutoMergeDisabledEvent",
        "MilestonedEvent",
    ]
    for event in events_data:
        match event.get("__typename"):
            case "LabeledEvent":
                time = parser.isoparse(event["createdAt"])
                name = canonicalise_label(event["label"]["name"])
                events.append(Event.add_label(time, name))
            case "UnlabeledEvent":
                time = parser.isoparse(event["createdAt"])
                name = canonicalise_label(event["label"]["name"])
                events.append(Event.remove_label(time, name))
            case "ReadyForReviewEvent":
                time = parser.isoparse(event["createdAt"])
                events.append(Event.undraft(time))
            case "ConvertToDraftEvent":
                time = parser.isoparse(event["createdAt"])
                events.append(Event.draft(time))
            case other_kind if other_kind not in known_irrelevant:
                print(f"unhandled event kind: {other_kind}")
    return (creation_time, events)


def _process_data(data: dict) -> Metadata:
    (createdAt, events) = parse_data(data)
    inner_data = data["data"]["repository"]["pullRequest"]

    # A PR started as draft iff the number of events toggling its state "differs" from the final
    # draft status, e.g. five toggles and not-draft means the PR started as draft.
    # Logically, this is the XOR of the values "draft was toggled overall" and "final state is draft".
    # This is truthy iff the draft state was toggled an odd number of times.
    draft_toggled_overall = len([e for e in events if e.change in [MarkedDraft, MarkedReady]]) % 2
    final_draft_state = inner_data["isDraft"]
    created_as_draft = draft_toggled_overall ^ final_draft_state

    from_fork = inner_data["headRepositoryOwner"]["login"] != "leanprover-community"
    return Metadata(createdAt, events, created_as_draft, from_fork)


# Determine a rough estimate how long PR 'number' is in its current state,
# and how long it was in its current state overall.
# 'data' is a JSON object containing all known information about a PR.
#
# TODO: this algorithm pretends CI always passes, i.e. ignores failing or running CI
#   (the *classification* doesn't, but I don't parse CI info yet... that only works
#    for full data, so this would need a "full data" boolean to not yield errors)
def last_status_update(data: dict) -> Tuple[datetime, relativedelta, PRStatus]:
    metadata = _process_data(data)
    return last_status_update_inner(datetime.now(timezone.utc), metadata)


def total_queue_time(data: dict) -> Tuple[Tuple[timedelta, relativedelta], str]:
    metadata = _process_data(data)
    return total_queue_time_inner(datetime.now(timezone.utc), metadata)


def first_time_on_queue(data: dict) -> datetime | None:
    metadata = _process_data(data)
    return first_on_queue_inner(metadata)
