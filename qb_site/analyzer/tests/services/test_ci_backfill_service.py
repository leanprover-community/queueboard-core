from __future__ import annotations

from unittest import mock

from django.test import TestCase

from core.models import Repository
from syncer.models import CommitStatusContext, PullRequest
from analyzer.models import PRRevision
from analyzer.services.ci_backfill import plan_missing_ci_shas, enqueue_ci_by_shas


class TestCIBakfillService(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.pr = PullRequest.objects.create(
            repository=self.repo,
            number=6,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at="2024-01-01T00:00:00Z",
            gh_updated_at="2024-01-02T00:00:00Z",
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
        )
        PRRevision.objects.create(
            pull_request=self.pr, head_sha="a1", from_ts="2024-01-01T00:10:00Z", to_ts="2024-01-01T01:00:00Z", seq=0
        )
        PRRevision.objects.create(pull_request=self.pr, head_sha="b2", from_ts="2024-01-01T01:00:00Z", to_ts=None, seq=1)
        # Provide CI for b2 only
        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC2",
            rest_id=None,
            head_sha="b2",
            name="lint",
            state="SUCCESS",
            target_url=None,
            description=None,
            gh_created_at="2024-01-01T01:05:00Z",
        )

    def test_plan_missing_ci(self) -> None:
        plan = plan_missing_ci_shas(repo=self.repo, pr_numbers=[self.pr.number], limit_per_pr=2)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].pr, self.pr)
        self.assertEqual(plan[0].shas, ["a1"])  # only a1 is missing

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_enqueue(self, MockTask) -> None:
        res = mock.Mock()
        res.id = "T999"
        MockTask.delay.return_value = res
        tid = enqueue_ci_by_shas(pr=self.pr, shas=["a1"], pages_per_sha=1, require_pr_association=False)
        self.assertEqual(tid, "T999")
        MockTask.delay.assert_called_once()
