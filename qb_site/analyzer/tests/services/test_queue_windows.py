from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from core.models import Repository
from analyzer.models import QueueRuleSet
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType
from analyzer.services.queue_windows import (
    is_on_queue_at,
    queue_windows_for_pr,
    total_queue_time_for_pr,
    who_was_on_queue_at,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class TestQueueWindows(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        # Default rules: open, not draft, and no forbidden labels.
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=[],
            forbidden_label_names=["blocked-by-other-pr"],
        )

    def _mk_pr(self, number: int, *, state: str = "open") -> PullRequest:
        created = _dt(2024, 9, 1)
        updated = _dt(2024, 9, 2)
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state=state,
            is_draft=False,
            gh_created_at=created,
            gh_updated_at=updated,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
        )

    def test_queue_window_from_label_unblock(self) -> None:
        pr = self._mk_pr(1)
        # Blocked from Sep 1 until Sep 6; then ready for review.
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 1),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 6),
            label_name="blocked-by-other-PR",
        )
        as_of = _dt(2024, 9, 10)

        windows = queue_windows_for_pr(pr, as_of=as_of)
        self.assertEqual(len(windows), 1)
        start, end = windows[0]
        self.assertEqual(start, _dt(2024, 9, 6))
        self.assertIsNone(end)

        summary = total_queue_time_for_pr(pr, as_of=as_of)
        # Expect 4 full days on the queue: Sep 6–10.
        self.assertEqual(summary.total_seconds, 4 * 24 * 60 * 60)

    def test_closed_pr_queue_time_capped_at_close(self) -> None:
        pr = self._mk_pr(2, state="closed")
        pr.closed_at = _dt(2024, 9, 8)
        pr.save(update_fields=["closed_at"])

        as_of = _dt(2024, 9, 10)
        windows = queue_windows_for_pr(pr, as_of=as_of)
        self.assertEqual(len(windows), 1)
        start, end = windows[0]
        # No labels: PR is awaiting review from creation until closure.
        self.assertEqual(start, _dt(2024, 9, 1))
        self.assertEqual(end, _dt(2024, 9, 8))

        summary = total_queue_time_for_pr(pr, as_of=as_of)
        self.assertEqual(summary.total_seconds, 7 * 24 * 60 * 60)

    def test_who_was_on_queue_at(self) -> None:
        pr1 = self._mk_pr(3)
        pr2 = self._mk_pr(4)
        # pr1: blocked then unblocked on Sep 6.
        PRTimelineEvent.objects.create(
            pull_request=pr1,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 1),
            label_name="blocked-by-other-PR",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr1,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 6),
            label_name="blocked-by-other-PR",
        )
        # pr2: always ready (no labels).

        at = _dt(2024, 9, 5)
        self.assertFalse(is_on_queue_at(pr1, at=at))
        self.assertTrue(is_on_queue_at(pr2, at=at))

        at_later = _dt(2024, 9, 7)
        on_queue = who_was_on_queue_at(repo=self.repo, at=at_later)
        self.assertIn(pr1, on_queue)
        self.assertIn(pr2, on_queue)

    def test_created_as_draft_blocks_queue_until_ready(self) -> None:
        pr = self._mk_pr(5)
        # Created as draft (no ConvertToDraft event), ready on Sep 5.
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.READY_FOR_REVIEW,
            occurred_at=_dt(2024, 9, 5),
        )

        self.assertFalse(is_on_queue_at(pr, at=_dt(2024, 9, 4)))
        self.assertTrue(is_on_queue_at(pr, at=_dt(2024, 9, 6)))

        as_of = _dt(2024, 9, 10)
        windows = queue_windows_for_pr(pr, as_of=as_of)
        self.assertEqual(len(windows), 1)
        start, end = windows[0]
        self.assertEqual(start, _dt(2024, 9, 5))
        self.assertIsNone(end)
