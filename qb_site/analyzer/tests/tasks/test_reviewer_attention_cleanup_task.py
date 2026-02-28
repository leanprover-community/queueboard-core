from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from analyzer.models import (
    ReviewerAttentionAutoUnassignRecord,
    ReviewerAttentionDailyRun,
    ReviewerAttentionNotificationRecord,
)
from analyzer.tasks.reviewer_attention_cleanup import reviewer_attention_cleanup_task
from core.models import Repository, User
from syncer.models import PullRequest


class ReviewerAttentionCleanupTaskTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.user = User.objects.create(github_login="alice")
        self.other_user = User.objects.create(github_login="bob")

    def _mk_pr(self, number: int, *, state: str = "open", assignees: list[str] | None = None) -> PullRequest:
        now = timezone.now()
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state=state,
            is_draft=False,
            gh_created_at=now - timezone.timedelta(days=5),
            gh_updated_at=now - timezone.timedelta(days=1),
            closed_at=now - timezone.timedelta(days=1) if state != "open" else None,
            merged_at=now - timezone.timedelta(days=1) if state == "merged" else None,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title=f"PR {number}",
            body="",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=assignees or [],
        )

    def test_deletes_old_notification_for_closed_pr(self) -> None:
        closed_pr = self._mk_pr(101, state="closed", assignees=[])
        ReviewerAttentionNotificationRecord.objects.create(
            run_date=(timezone.now() - timezone.timedelta(days=10)).date(),
            repository=self.repo,
            reviewer=self.user,
            pr_number=closed_pr.number,
            category=ReviewerAttentionNotificationRecord.CATEGORY_NUDGE,
            cycle_anchor_at=timezone.now() - timezone.timedelta(days=12),
            status=ReviewerAttentionNotificationRecord.STATUS_SENT,
        )

        res = reviewer_attention_cleanup_task.apply(kwargs={"notification_retention_days": 7}).get()

        self.assertEqual(res["notifications"]["deleted"], 1)
        self.assertEqual(ReviewerAttentionNotificationRecord.objects.count(), 0)

    def test_deletes_old_notification_when_reviewer_no_longer_assigned(self) -> None:
        pr = self._mk_pr(102, state="open", assignees=[self.other_user.github_login])
        ReviewerAttentionNotificationRecord.objects.create(
            run_date=(timezone.now() - timezone.timedelta(days=10)).date(),
            repository=self.repo,
            reviewer=self.user,
            pr_number=pr.number,
            category=ReviewerAttentionNotificationRecord.CATEGORY_NUDGE,
            cycle_anchor_at=timezone.now() - timezone.timedelta(days=12),
            status=ReviewerAttentionNotificationRecord.STATUS_SENT,
        )

        res = reviewer_attention_cleanup_task.apply(kwargs={"notification_retention_days": 7}).get()

        self.assertEqual(res["notifications"]["deleted"], 1)
        self.assertEqual(ReviewerAttentionNotificationRecord.objects.count(), 0)

    def test_keeps_old_notification_when_pr_open_and_reviewer_still_assigned(self) -> None:
        pr = self._mk_pr(103, state="open", assignees=[self.user.github_login])
        ReviewerAttentionNotificationRecord.objects.create(
            run_date=(timezone.now() - timezone.timedelta(days=10)).date(),
            repository=self.repo,
            reviewer=self.user,
            pr_number=pr.number,
            category=ReviewerAttentionNotificationRecord.CATEGORY_NUDGE,
            cycle_anchor_at=timezone.now() - timezone.timedelta(days=12),
            status=ReviewerAttentionNotificationRecord.STATUS_SENT,
        )

        res = reviewer_attention_cleanup_task.apply(kwargs={"notification_retention_days": 7}).get()

        self.assertEqual(res["notifications"]["deleted"], 0)
        self.assertEqual(res["notifications"]["kept"], 1)
        self.assertEqual(ReviewerAttentionNotificationRecord.objects.count(), 1)

    def test_deletes_old_auto_unassign_and_run_rows_by_retention(self) -> None:
        old_run = ReviewerAttentionDailyRun.objects.create(
            run_date=(timezone.now() - timezone.timedelta(days=40)).date(),
            started_at=timezone.now() - timezone.timedelta(days=40),
            status="completed",
            reports_enabled=True,
            enforcement_enabled=False,
            delivery_enabled=False,
            repository=self.repo,
        )
        recent_run = ReviewerAttentionDailyRun.objects.create(
            run_date=(timezone.now() - timezone.timedelta(days=2)).date(),
            started_at=timezone.now() - timezone.timedelta(days=2),
            status="completed",
            reports_enabled=True,
            enforcement_enabled=False,
            delivery_enabled=False,
            repository=self.repo,
        )
        ReviewerAttentionAutoUnassignRecord.objects.create(
            run_date=(timezone.now() - timezone.timedelta(days=120)).date(),
            repository=self.repo,
            reviewer=self.user,
            pr_number=201,
            status=ReviewerAttentionAutoUnassignRecord.STATUS_UNASSIGNED,
            run=old_run,
        )
        ReviewerAttentionAutoUnassignRecord.objects.create(
            run_date=(timezone.now() - timezone.timedelta(days=5)).date(),
            repository=self.repo,
            reviewer=self.user,
            pr_number=202,
            status=ReviewerAttentionAutoUnassignRecord.STATUS_UNASSIGNED,
            run=recent_run,
        )

        res = reviewer_attention_cleanup_task.apply(
            kwargs={
                "auto_unassign_retention_days": 90,
                "run_retention_days": 30,
            }
        ).get()

        self.assertEqual(res["auto_unassign_records_deleted"], 1)
        self.assertEqual(res["runs_deleted"], 1)
        self.assertTrue(ReviewerAttentionDailyRun.objects.filter(id=recent_run.id).exists())
        self.assertEqual(ReviewerAttentionAutoUnassignRecord.objects.count(), 1)
