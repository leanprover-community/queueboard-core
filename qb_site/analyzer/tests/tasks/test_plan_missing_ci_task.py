from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRRevision, PRRevisionBuildState
from analyzer.tasks.plan_missing_ci import plan_missing_ci_backfill_task
from core.models import Repository
from syncer.models import CheckRun, PullRequest


class TestPlanMissingCITask(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)

    def _mk_pr_with_revision(self, number: int, head_sha: str) -> PullRequest:
        now = timezone.now()
        pr = PullRequest.objects.create(
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
            timeline_backfill_done=True,
            commits_backfill_done=True,
        )
        PRRevision.objects.create(pull_request=pr, head_sha=head_sha, from_ts=pr.gh_created_at, to_ts=None, seq=0)
        state = PRRevisionBuildState.objects.create(pull_request=pr, revision_version=1)
        return pr

    @patch("analyzer.tasks.plan_missing_ci.enqueue_ci_by_shas")
    def test_enqueues_missing_ci_and_marks_checked(self, mock_enqueue) -> None:
        pr = self._mk_pr_with_revision(1, "sha-miss")

        res = plan_missing_ci_backfill_task.apply(
            kwargs={"max_prs_per_repo": 5, "shas_per_pr": 2, "pages_per_sha": 1}
        ).get()

        mock_enqueue.assert_called_once()
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertEqual(state.ci_checked_revision_version, state.revision_version)
        self.assertIsNotNone(state.ci_checked_at)
        self.assertEqual(res["prs_checked"], 1)
        self.assertEqual(res["ci_tasks"], 1)

    @patch("analyzer.tasks.plan_missing_ci.enqueue_ci_by_shas")
    def test_skips_when_already_checked_for_version(self, mock_enqueue) -> None:
        pr = self._mk_pr_with_revision(2, "sha-old")
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        state.ci_checked_revision_version = state.revision_version
        state.ci_checked_at = timezone.now()
        state.save(update_fields=["ci_checked_revision_version", "ci_checked_at"])

        res = plan_missing_ci_backfill_task.apply(
            kwargs={"max_prs_per_repo": 5, "shas_per_pr": 2, "pages_per_sha": 1}
        ).get()

        mock_enqueue.assert_not_called()
        state.refresh_from_db()
        self.assertEqual(state.ci_checked_revision_version, state.revision_version)
        self.assertEqual(res["prs_checked"], 0)
        self.assertEqual(res["ci_tasks"], 0)

    @patch("analyzer.tasks.plan_missing_ci.enqueue_ci_by_shas")
    def test_marks_checked_even_without_missing_ci(self, mock_enqueue) -> None:
        pr = self._mk_pr_with_revision(3, "sha-ci")
        # Seed CI so no missing shas are planned.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR1",
            head_sha="sha-ci",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=pr.gh_created_at + timezone.timedelta(hours=1),
            gh_completed_at=pr.gh_created_at + timezone.timedelta(hours=2),
        )

        res = plan_missing_ci_backfill_task.apply(
            kwargs={"max_prs_per_repo": 5, "shas_per_pr": 2, "pages_per_sha": 1}
        ).get()

        mock_enqueue.assert_not_called()
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertEqual(state.ci_checked_revision_version, state.revision_version)
        self.assertIsNotNone(state.ci_checked_at)
        self.assertEqual(res["prs_checked"], 1)
        self.assertEqual(res["ci_tasks"], 0)
