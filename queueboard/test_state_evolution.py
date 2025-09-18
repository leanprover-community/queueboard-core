#!/usr/bin/env python3

"""
Unit test for the code in `state_evolution.py`: all of these are very mathlib-specific, hence extracted into a separate file.
"""

from ci_status import CIStatus
from classify_pr_state import LabelKind
from state_evolution import PRState, PRStatus, Event, Metadata, total_queue_time_inner, determine_state_changes, first_on_queue_inner, last_status_update_inner

from dateutil.relativedelta import relativedelta
from dateutil import tz
from datetime import datetime
from typing import List, Tuple

######### Some basic unit tests ##########

# Helper methods to reduce boilerplate


def april(n: int) -> datetime:
    return datetime(2024, 4, n, tzinfo=tz.tzutc())
def june(n: int) -> datetime:
    return datetime(2024, 6, n, tzinfo=tz.tzutc())
def july(n: int) -> datetime:
    return datetime(2024, 7, n, tzinfo=tz.tzutc())
def aug(n: int) -> datetime:
    return datetime(2024, 8, n, tzinfo=tz.tzutc())
def sep(n: int) -> datetime:
    return datetime(2024, 9, n, tzinfo=tz.tzutc())


# These tests are just some basic smoketests and not exhaustive.
def test_determine_state_changes() -> None:
    def check(events: List[Event], expected: PRState) -> None:
        initial = PRState([], CIStatus.Pass, False, False)
        compute = determine_state_changes(datetime(2024, 7, 15, tzinfo=tz.tzutc()), initial, events)
        actual = compute[-1][1]
        assert expected == actual, f"expected PR state {expected} from events {events}, got {actual}"
    check([], PRState.with_labels_and_ci([], CIStatus.Pass))
    dummy = datetime(2024, 7, 2, tzinfo=tz.tzutc())
    # Drafting or undrafting; changing CI status.
    check([Event.draft(dummy)], PRState.with_labels_ci_draft([], CIStatus.Pass, True))
    check([Event.draft(dummy), Event.undraft(dummy)], PRState.with_labels_ci_draft([], CIStatus.Pass, False))
    # Additional "undraft" or "draft" events are ignored.
    check([Event.undraft(dummy)], PRState.with_labels_ci_draft([], CIStatus.Pass, False))
    check([Event.undraft(dummy), Event.undraft(dummy), Event.draft(dummy)], PRState.with_labels_ci_draft([], CIStatus.Pass, True))
    check([Event.undraft(dummy), Event.draft(dummy), Event.draft(dummy)], PRState.with_labels_ci_draft([], CIStatus.Pass, True))
    # Updating the CI status.
    check([Event.update_ci_status(dummy, CIStatus.Running)], PRState.with_labels_and_ci([], CIStatus.Running))
    check([Event.update_ci_status(dummy, CIStatus.Fail)], PRState.with_labels_and_ci([], CIStatus.Fail))
    check([Event.update_ci_status(dummy, CIStatus.Pass)], PRState.with_labels_and_ci([], CIStatus.Pass))
    check([Event.update_ci_status(dummy, CIStatus.Pass), Event.update_ci_status(dummy, CIStatus.Fail)], PRState.with_labels_and_ci([], CIStatus.Fail))
    check([Event.update_ci_status(dummy, CIStatus.Pass), Event.draft(dummy), Event.update_ci_status(dummy, CIStatus.Running), Event.undraft(dummy)], PRState.with_labels_and_ci([], CIStatus.Running))

    # Adding and removing labels.
    check([Event.add_label(dummy, "WIP")], PRState.with_labels([LabelKind.WIP]))
    check([Event.add_label(dummy, "awaiting-author")], PRState.with_labels([LabelKind.Author]))
    # Non-relevant labels are not recorded here.
    check([Event.add_label(dummy, "t-data")], PRState.with_labels([]))
    check([Event.add_label(dummy, "t-data"), Event.add_label(dummy, "WIP")], PRState.with_labels([LabelKind.WIP]))
    check([Event.add_label(dummy, "t-data"), Event.add_label(dummy, "WIP"), Event.remove_label(dummy, "t-data")], PRState.with_labels([LabelKind.WIP]))
    # Adding two labels.
    check([Event.add_label(dummy, "awaiting-author")], PRState.with_labels([LabelKind.Author]))
    check([Event.add_label(dummy, "awaiting-author"), Event.add_label(dummy, "WIP")], PRState.with_labels([LabelKind.Author, LabelKind.WIP]))
    check([Event.add_label(dummy, "awaiting-author"), Event.remove_label(dummy, "awaiting-author")], PRState.with_labels([]))
    check([Event.add_label(dummy, "awaiting-author"), Event.remove_label(dummy, "awaiting-author"), Event.add_label(dummy, "awaiting-zulip")], PRState.with_labels([LabelKind.Decision]))
    # TODO: better tests for add-remove
    # - equivalent to individual additions; with irrelevant labels; same for removal
    # - adding and removing same label is a no-op
    # - test that intermediate states are - no errors and - no contradictory states
    #   => need to test intermediate ones -> need the full sequence of states to test?
    check([Event.add_remove_labels(dummy, ["WIP"], ["WIP"])], PRState.with_labels([]))


def test_total_queue_time() -> None:
    def check_basic(created: datetime, now: datetime, events: List[Event], expected: relativedelta) -> None:
        ((_, wait), _) = total_queue_time_inner(now, Metadata(created, events, False, False))
        assert wait == expected, f"basic test failed: expected total time of {expected} in review, obtained {wait} instead"
    def check_with_initial(now: datetime, state: Metadata, expected: relativedelta) -> None:
        ((_, wait), _) = total_queue_time_inner(now, state)
        assert wait == expected, f"test failed: expected total time of {expected} in review, obtained {wait} instead"

    check_basic(sep(1), sep(10), [Event.add_label(sep(1), 'blocked-by-other-PR')], relativedelta(days=0))
    check_basic(sep(1), sep(10), [Event.add_label(sep(1), 'blocked-by-other-PR'), Event.add_label(sep(6), 'merge-conflict')], relativedelta(days=0))

    check_basic(sep(1), sep(10), [Event.add_label(sep(1), 'blocked-by-other-PR'), Event.remove_label(sep(6), "blocked-by-other-PR")], relativedelta(days=4))
    check_basic(sep(1), sep(10), [Event.add_label(sep(1), 'blocked-by-other-PR'), Event.remove_label(sep(6), "blocked-by-other-PR"), Event.add_label(sep(8), "WIP")], relativedelta(days=2))

    check_basic(sep(1), sep(20), [Event.add_label(sep(1), 'blocked-by-other-PR'), Event.remove_label(sep(8), 'blocked-by-other-PR'), Event.add_label(sep(10), 'WIP')], relativedelta(days=2))
    check_basic(sep(1), sep(10), [Event.add_label(sep(1), 'blocked-by-other-PR'), Event.remove_label(sep(8), 'blocked-by-other-PR')], relativedelta(days=2))

    # Doing nothing in April: not ready for review. In September, it is!
    check_basic(april(1), april(3), [], relativedelta(days=0))
    check_basic(sep(1), sep(3), [], relativedelta(days=2))
    # Applying an irrelevant label.
    check_basic(sep(1), sep(5), [Event.add_label(sep(1), "CI")], relativedelta(days=4))
    # Removing it again.
    check_basic(
        sep(1), sep(12),
        [Event.add_label(sep(1), "CI"), Event.remove_label(sep(3), "CI")],
        relativedelta(days=11),
    )

    # After September 8th, this PR is in WIP status -> only seven days in review.
    check_basic(sep(1), sep(10), [Event.add_label(sep(1), 'CI'), Event.remove_label(sep(3), 'CI'), Event.add_label(sep(8), 'WIP')], relativedelta(days=7))

    # A PR getting blocked.
    check_basic(sep(1), sep(10), [Event.add_label(sep(1), 'blocked-by-other-PR'), Event.add_label(sep(8), 'easy')], relativedelta(days=0))
    # A PR getting unblocked again.
    check_basic(sep(1), sep(10), [Event.add_label(sep(1), 'blocked-by-other-PR'), Event.remove_label(sep(8), 'blocked-by-other-PR')], relativedelta(days=2))

    # Apply two irrelevant labels, then removing one.
    check_basic(sep(1), sep(20), [Event.add_label(sep(1), 'blocked-by-other-PR'), Event.add_label(sep(3), 'new-contributor'), Event.add_label(sep(12), 'WIP'), Event.add_label(sep(14), 'AIMS'), Event.remove_label(sep(16), 'new-contributor'), Event.remove_label(sep(18), 'blocked-by-other-PR')], relativedelta(days=0))
    # Still WIP in the end; TODO also test below!
    check_basic(sep(1), sep(20), [Event.add_label(sep(10), 'blocked-by-other-PR'), Event.add_label(sep(13), 'new-contributor'), Event.add_label(sep(14), 'WIP'), Event.add_label(sep(16), 'AIMS'), Event.remove_label(sep(16), 'new-contributor'), Event.remove_label(sep(18), 'blocked-by-other-PR')], relativedelta(days=9))
    # Waiting for review in the end.
    check_basic(sep(1), sep(20), [Event.add_label(sep(10), 'blocked-by-other-PR'), Event.add_label(sep(13), 'new-contributor'), Event.add_label(sep(14), 'WIP'), Event.add_label(sep(16), 'AIMS'), Event.remove_label(sep(16), 'new-contributor'), Event.remove_label(sep(18), 'blocked-by-other-PR'), Event.remove_label(sep(19), 'WIP')], relativedelta(days=10))

    # Some more complex tests, adapted from real PRs.
    # Adapted from PR 16666: created in draft state.
    events = [Event.add_label(sep(10), 't-meta'), Event.undraft(sep(10)), Event.add_label(sep(29), 'ready-to-merge')]
    check_basic(sep(1), sep(30), events, relativedelta(days=28))
    check_with_initial(sep(30), Metadata(sep(1), events, True, False), relativedelta(days=19))

    # Minimised from PR 14269
    events = [
        Event.add_label(june(29), 'awaiting-review-DONT-USE'),
        Event.add_label(june(29), 'new-contributor'),
        Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'),
        Event.remove_label(july(1), 'awaiting-author'),
    ]
    check_with_initial(sep(30), Metadata(june(28), events, False, False), relativedelta(days=2))

    # Subtle detail: the awaiting-review flag is not removed on July 9th (for these data),
    # so a new assessment of the state only happens at the later label changes.
    events = [
        Event.add_label(june(29), 'awaiting-review-DONT-USE'),
        Event.add_label(june(29), 'new-contributor'),
        Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'),
        Event.remove_label(july(1), 'awaiting-author'),
        Event.add_label(july(1), 'awaiting-review-DONT-USE'), Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'), Event.remove_label(july(2), 'awaiting-author'),
        Event.add_label(july(2), 'WIP'), Event.remove_label(july(13), 'WIP'),
        # is now without essential labels -> reviewable
        Event.add_label(july(13), 'help-wanted'), Event.add_label(aug(8), 'awaiting-author'), # waiting for help
        Event.add_label(aug(15), 't-number-theory'), Event.remove_label(sep(3), 'awaiting-author'), # help-wanted
        # two days awaiting review: September 20 to 22
        Event.remove_label(sep(20), 'help-wanted'), Event.add_label(sep(22), 'awaiting-author'),
    ]
    check_with_initial(sep(25), Metadata(june(29), events, False, False), relativedelta(days=4))

    events = [
        Event.add_label(june(29), 'awaiting-review-DONT-USE'),
        Event.add_label(june(29), 'new-contributor'),
        Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'),
        Event.remove_label(july(1), 'awaiting-author'),
        Event.add_label(july(1), 'awaiting-review-DONT-USE'), Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'), Event.remove_label(july(2), 'awaiting-author'),
        Event.add_label(july(2), 'WIP'), Event.remove_label(july(13), 'WIP'),
        # is now without essential labels -> reviewable
        Event.add_label(july(13), 'help-wanted'), Event.add_label(aug(8), 'awaiting-author'), # waiting for help
        Event.add_label(aug(15), 't-number-theory'), Event.remove_label(sep(3), 'awaiting-author'), # help-wanted
        # two days awaiting review: September 20 to 22
        Event.remove_label(sep(20), 'help-wanted'), Event.add_label(sep(22), 'awaiting-author'), Event.remove_label(sep(23), 'awaiting-author')
        # on the queue now
    ]
    check_with_initial(sep(27), Metadata(june(29), events, False, False), relativedelta(days=8))

    events = [Event.draft(sep(3)), Event.add_label(sep(10), 't-meta'), Event.undraft(sep(10)), Event.draft(sep(15)), Event.add_label(sep(29), 'ready-to-merge')]
    # total review time windows: sep 1-3, sep 10-15: 7 days
    check_with_initial(sep(30), Metadata(sep(1), events, False, False), relativedelta(days=7))


# Some basic tests for last_status_update and first time on the queue.
def test_last_status_update():
    def check_with_initial(now: datetime, state: Metadata, expected: Tuple[datetime, relativedelta, PRStatus]) -> None:
        (absolute, relative, last_status) = last_status_update_inner(now, state)
        assert absolute == expected[0], f"basic test failed: expected last update on {expected[0]} in review, obtained {absolute} instead"
        assert relative == expected[1], f"basic test failed: expected last update on {expected[1]} in review, obtained {relative} instead"
        assert last_status == expected[2], f"expected last PR status of {expected[2]}, obtained {last_status} instead"
    def check_basic(created: datetime, now: datetime, events: List[Event], expected: Tuple[datetime, relativedelta, PRStatus]) -> None:
        check_with_initial(now, Metadata(created, events, False, False), expected)
    def check_first(metadata: Metadata, expected: datetime) -> None:
        actual = first_on_queue_inner(metadata)
        assert actual == expected, f"expect first time on the queue of {expected}, obtained {actual} instead"
    def check_first_basic(created: datetime, events: List[Event], expected: datetime) -> None:
        check_first(Metadata(created, events, False, False), expected)

    events = [
        Event.add_label(sep(10), 'blocked-by-other-PR'), Event.add_label(sep(13), 'new-contributor'), Event.add_label(sep(14), 'WIP'), Event.add_label(sep(16), 'AIMS'), Event.remove_label(sep(16), 'new-contributor'), Event.remove_label(sep(18), 'AIMS')
    ]
    check_basic(sep(1), sep(20), events, (sep(18), relativedelta(days=2), PRStatus.Blocked))
    # The PR is created and stays without labels for 9 days.
    check_first_basic(sep(1), events, sep(1))
    # Now, it is created and labelled at the same time: we do not regard the initial "0 seconds" as ready for review.
    check_first_basic(sep(10), events, None)

    events = [
        Event.add_label(sep(10), 'blocked-by-other-PR'), Event.add_label(sep(13), 'new-contributor'), Event.add_label(sep(14), 'WIP'), Event.add_label(sep(16), 'AIMS'), Event.remove_label(sep(16), 'new-contributor'), Event.remove_label(sep(19), 'blocked-by-other-PR')
    ]
    check_basic(sep(1), sep(22), events, (sep(19), relativedelta(days=3), PRStatus.NotReady))
    check_first_basic(sep(1), events, sep(1))
    check_first_basic(sep(10), events, None)
    events = [
        Event.add_label(sep(10), 'blocked-by-other-PR'), Event.add_label(sep(13), 'new-contributor'), Event.add_label(sep(14), 'WIP'), Event.add_label(sep(16), 'AIMS'), Event.remove_label(sep(16), 'new-contributor'), Event.remove_label(sep(19), 'blocked-by-other-PR'), Event.remove_label(sep(20), 'WIP')
    ]
    check_basic(sep(1), sep(24), events, (sep(20), relativedelta(days=4), PRStatus.AwaitingReview))
    check_first_basic(sep(10), events, sep(20))

    # Adapted from PR 16666: created in draft state.
    events = [Event.add_label(sep(10), 't-meta'), Event.undraft(sep(11)), Event.add_label(sep(25), 'ready-to-merge')]
    check_basic(sep(1), sep(30), events, (sep(25), relativedelta(days=5), PRStatus.AwaitingBors))
    check_with_initial(sep(28), Metadata(sep(1), events, True, False), (sep(25), relativedelta(days=3), PRStatus.AwaitingBors))
    check_first_basic(sep(9), events, sep(9))

    events = [Event.draft(sep(3)), Event.add_label(sep(10), 't-meta'), Event.undraft(sep(10)), Event.draft(sep(15)), Event.add_label(sep(29), 'WIP-to-merge')]
    check_with_initial(sep(30), Metadata(sep(1), events, False, False), (sep(29), relativedelta(days=1), PRStatus.NotReady))
    check_first_basic(sep(3), events, sep(10))
    # Minimised from PR 14269
    events = [
        Event.add_label(june(29), 'awaiting-review-DONT-USE'),
        Event.add_label(june(29), 'new-contributor'),
        Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'),
        Event.remove_label(july(1), 'awaiting-author'),
    ]
    check_with_initial(sep(30), Metadata(june(28), events, False, False), (july(1), relativedelta(months=2, days=29), PRStatus.AwaitingAuthor))
    check_first_basic(june(29), events, june(29))
    events = [
        Event.add_label(june(29), 'awaiting-review-DONT-USE'),
        Event.add_label(june(29), 'new-contributor'),
        Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_remove_labels(july(1), ['awaiting-author'], ["awaiting-author"]),
    ]
    check_with_initial(sep(30), Metadata(june(28), events, False, False), (july(1), relativedelta(months=2, days=29), PRStatus.AwaitingAuthor))
    check_first_basic(june(28), events, june(29))

    # This one illustrates a subtle change of labels: removing the label on July 13 means this is awaiting review.
    events = [
        Event.add_label(june(29), 'awaiting-review-DONT-USE'),
        Event.add_label(june(29), 'new-contributor'),
        Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'),
        Event.remove_label(july(1), 'awaiting-author'),
        Event.add_label(july(1), 'awaiting-review-DONT-USE'), Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'), Event.remove_label(july(2), 'awaiting-author'),
        Event.add_label(july(2), 'WIP'), Event.remove_label(july(13), 'WIP'),
    ]
    check_with_initial(aug(30), Metadata(june(28), events, False, False), (july(13), relativedelta(months=1, days=17), PRStatus.AwaitingReview))
    check_first_basic(june(28), events, june(29))

    # This one illustrates a subtle change of labels: removing the label on July 13 means this is awaiting review.
    # Doing so a month later would have triggered my algorithm later.
    # (In practice, all review labels were removed on July 9th, so this is purely hypothetical.)
    events = [
        Event.add_label(june(29), 'awaiting-review-DONT-USE'),
        Event.add_label(july(2), 'WIP'), Event.remove_label(aug(13), 'WIP'),
    ]
    check_with_initial(aug(30), Metadata(june(27), events, False, False), (aug(13), relativedelta(days=17), PRStatus.AwaitingReview))
    check_first_basic(june(27), events, june(29))

    events = [
        Event.add_label(june(29), 'awaiting-review-DONT-USE'),
        Event.add_label(june(29), 'new-contributor'),
        Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'),
        Event.remove_label(july(1), 'awaiting-author'),
        Event.add_label(july(1), 'awaiting-review-DONT-USE'), Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'), Event.remove_label(july(2), 'awaiting-author'),
        Event.add_label(july(2), 'WIP'), Event.remove_label(july(13), 'WIP'),
        Event.add_label(july(13), 'help-wanted'), Event.add_label(aug(8), 'awaiting-author'),
    ]
    check_with_initial(aug(30), Metadata(june(28), events, False, False), (aug(8), relativedelta(days=22), PRStatus.HelpWanted))
    check_first_basic(june(28), events, june(29))

    events = [
        Event.add_label(june(29), 'awaiting-review-DONT-USE'),
        Event.add_label(june(29), 'new-contributor'),
        Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-review-DONT-USE'), Event.remove_label(july(1), 'awaiting-review-DONT-USE'),
        Event.add_label(july(1), 'awaiting-author'), Event.remove_label(july(2), 'awaiting-author'),
        Event.add_label(july(2), 'WIP'), Event.remove_label(july(13), 'WIP'),
        Event.add_label(july(13), 'help-wanted'), Event.add_label(aug(8), 'awaiting-author'),
        Event.add_label(aug(15), 't-number-theory'), Event.remove_label(sep(3), 'awaiting-author'),
        Event.remove_label(sep(20), 'help-wanted'), Event.add_label(sep(22), 'awaiting-author'), Event.remove_label(sep(23), 'awaiting-author')
    ]
    check_with_initial(sep(30), Metadata(june(28), events, False, False), (sep(23), relativedelta(days=7), PRStatus.AwaitingReview))
    check_first_basic(june(28), events, june(29))


if __name__ == '__main__':
    test_determine_state_changes()
    test_total_queue_time()
    test_last_status_update()
