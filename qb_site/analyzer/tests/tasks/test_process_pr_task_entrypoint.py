from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRRevisionBuildState
from analyzer.tasks import process_pr_task
from core.models import Repository
from syncer.models import PullRequest


class TestProcessPRTaskEntrypoint(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        now = timezone.now()
        self.pr = PullRequest.objects.create(
            repository=self.repo,
            number=1,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=now - timezone.timedelta(days=1),
            gh_updated_at=now - timezone.timedelta(hours=1),
            base_ref_name="master",
            head_ref_name="branch",
            head_sha="seed",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
        )
        PRRevisionBuildState.objects.create(
            pull_request=self.pr,
            revision_version=3,
            ci_checked_revision_version=3,
            ci_checked_at=now,
        )

    @patch("analyzer.tasks.plan_missing_ci_shas")
    @patch("analyzer.tasks.process_pr")
    @patch("analyzer.tasks.GitHubClient")
    def test_skips_ci_planning_when_revision_already_checked(
        self,
        _mock_client,
        mock_process_pr,
        mock_plan_missing_ci,
    ) -> None:
        mock_process_pr.return_value = {
            "created": 0,
            "deleted": 0,
            "revisions": "noop",
            "queue_windows": {},
            "harvest": {},
            "ci_backfill": {"status": "skipped", "planned": 0, "enqueued": []},
        }

        res = process_pr_task.apply(kwargs={"pr_id": int(self.pr.id)}).get()

        mock_process_pr.assert_called_once()
        mock_plan_missing_ci.assert_not_called()
        ci_step = res["steps"]["ci_backfill"]
        self.assertEqual(ci_step["status"], "skipped")
        self.assertEqual(ci_step["reason"], "already_checked_current_revision")
