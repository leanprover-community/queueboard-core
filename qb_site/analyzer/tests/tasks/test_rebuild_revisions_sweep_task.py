from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRRevision
from analyzer.tasks.rebuild_revisions_sweep import rebuild_revisions_sweep_task
from core.models import Repository
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType


class TestRebuildRevisionsSweepTask(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)

    def _mk_pr(self, number: int, *, backfill_done: bool = True) -> PullRequest:
        now = timezone.now()
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=now - timezone.timedelta(days=1),
            gh_updated_at=now - timezone.timedelta(hours=1),
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="fork",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=backfill_done,
            commits_backfill_done=backfill_done,
        )

    def test_rebuilds_revisions_for_eligible_prs(self) -> None:
        pr = self._mk_pr(1)
        t_fp = pr.gh_created_at + timezone.timedelta(hours=1)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t_fp,
            before_sha="a1",
            after_sha="b2",
        )

        res = rebuild_revisions_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()
        revs = PRRevision.objects.filter(pull_request=pr)
        self.assertEqual(revs.count(), 2)
        self.assertEqual(res["revisions_updated"], 1)
        self.assertEqual(res["prs_checked"], 1)

    def test_skips_when_backfill_incomplete(self) -> None:
        pr = self._mk_pr(2, backfill_done=False)
        res = rebuild_revisions_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()
        self.assertEqual(PRRevision.objects.filter(pull_request=pr).count(), 0)
        self.assertEqual(res["revisions_updated"], 0)
        self.assertEqual(res["prs_checked"], 0)
