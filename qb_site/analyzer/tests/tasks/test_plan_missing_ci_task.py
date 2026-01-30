from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRRevision, PRRevisionBuildState
from analyzer.tasks.plan_missing_ci import plan_missing_ci_backfill_task
from core.models import Repository
from syncer.models import CIShaFetchState, CheckRun, PullRequest, PRTimelineEvent, PRTimelineEventType


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

        res = plan_missing_ci_backfill_task.apply(kwargs={"max_prs_per_repo": 5, "shas_per_pr": 2, "pages_per_sha": 1}).get()

        mock_enqueue.assert_called_once()
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertIsNone(state.ci_checked_revision_version)
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

        res = plan_missing_ci_backfill_task.apply(kwargs={"max_prs_per_repo": 5, "shas_per_pr": 2, "pages_per_sha": 1}).get()

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

        res = plan_missing_ci_backfill_task.apply(kwargs={"max_prs_per_repo": 5, "shas_per_pr": 2, "pages_per_sha": 1}).get()

        mock_enqueue.assert_not_called()
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertEqual(state.ci_checked_revision_version, state.revision_version)
        self.assertIsNotNone(state.ci_checked_at)
        self.assertEqual(res["prs_checked"], 1)
        self.assertEqual(res["ci_tasks"], 0)

    def test_skips_when_backoff_blocks_enqueue(self) -> None:
        pr = self._mk_pr_with_revision(4, "sha-blocked")
        CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha-blocked",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="error",
            attempts=1,
        )

        res = plan_missing_ci_backfill_task.apply(kwargs={"max_prs_per_repo": 5, "shas_per_pr": 2, "pages_per_sha": 1}).get()

        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertIsNone(state.ci_checked_revision_version)
        self.assertIsNotNone(state.ci_checked_at)
        self.assertEqual(res["prs_checked"], 1)
        self.assertEqual(res["ci_tasks"], 0)
        self.assertEqual(res["prs_skipped_backoff"], 1)

    @patch("analyzer.tasks.plan_missing_ci.enqueue_ci_by_shas")
    def test_skips_terminal_fetch_state(self, mock_enqueue) -> None:
        pr = self._mk_pr_with_revision(6, "sha-terminal")
        CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha-terminal",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="not_found",
            attempts=1,
        )

        res = plan_missing_ci_backfill_task.apply(kwargs={"max_prs_per_repo": 5, "shas_per_pr": 2, "pages_per_sha": 1}).get()

        mock_enqueue.assert_not_called()
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertEqual(state.ci_checked_revision_version, state.revision_version)
        self.assertIsNotNone(state.ci_checked_at)
        self.assertEqual(res["prs_checked"], 1)
        self.assertEqual(res["ci_tasks"], 0)
        self.assertEqual(res["prs_skipped_backoff"], 0)

    @patch("analyzer.tasks.plan_missing_ci.enqueue_ci_by_shas")
    def test_force_push_heads_skip_terminal_and_progress(self, mock_enqueue) -> None:
        pr = self._mk_pr_with_revision(7, "rev1")
        t0 = pr.gh_created_at + timezone.timedelta(hours=1)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t0,
            before_sha="fp_before",
            after_sha="fp_after",
        )
        CIShaFetchState.objects.create(
            repository=self.repo,
            sha="fp_before",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="filtered",
            attempts=1,
        )

        plan_missing_ci_backfill_task.apply(kwargs={"max_prs_per_repo": 5, "shas_per_pr": 5, "pages_per_sha": 1}).get()

        mock_enqueue.assert_called_once()
        _, call_kwargs = mock_enqueue.call_args
        shas = call_kwargs.get("shas") or []
        self.assertIn("fp_after", shas)
        self.assertIn("rev1", shas)
        self.assertNotIn("fp_before", shas)

    @patch("analyzer.tasks.plan_missing_ci.enqueue_ci_by_shas")
    def test_two_sweeps_converge_after_terminal(self, mock_enqueue) -> None:
        pr = self._mk_pr_with_revision(8, "sha1")
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="sha2",
            from_ts=pr.gh_created_at + timezone.timedelta(hours=1),
            to_ts=None,
            seq=1,
        )

        # First sweep should enqueue both heads (no CI, no terminal fetch state).
        plan_missing_ci_backfill_task.apply(kwargs={"max_prs_per_repo": 5, "shas_per_pr": 5, "pages_per_sha": 1}).get()
        self.assertTrue(mock_enqueue.called)
        _, call_kwargs = mock_enqueue.call_args
        shas = set(call_kwargs.get("shas") or [])
        self.assertIn("sha1", shas)
        self.assertIn("sha2", shas)

        # Mark both as terminal outcomes.
        CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha1",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="empty",
            attempts=1,
        )
        CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha2",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="not_found",
            attempts=1,
        )

        mock_enqueue.reset_mock()
        res = plan_missing_ci_backfill_task.apply(kwargs={"max_prs_per_repo": 5, "shas_per_pr": 5, "pages_per_sha": 1}).get()

        mock_enqueue.assert_not_called()
        self.assertEqual(res["ci_tasks"], 0)
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertEqual(state.ci_checked_revision_version, state.revision_version)

    @patch("analyzer.tasks.plan_missing_ci.enqueue_ci_by_shas")
    def test_marks_checked_only_when_no_actionable_shas(self, mock_enqueue) -> None:
        pr = self._mk_pr_with_revision(9, "sha-one")
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="sha-two",
            from_ts=pr.gh_created_at + timezone.timedelta(hours=1),
            to_ts=None,
            seq=1,
        )

        # First sweep should enqueue and should NOT mark checked.
        plan_missing_ci_backfill_task.apply(kwargs={"max_prs_per_repo": 5, "shas_per_pr": 5, "pages_per_sha": 1}).get()
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertIsNone(state.ci_checked_revision_version)

        # Mark both SHAs as terminal; second sweep should mark checked.
        CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha-one",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="empty",
            attempts=1,
        )
        CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha-two",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="not_found",
            attempts=1,
        )

        mock_enqueue.reset_mock()
        plan_missing_ci_backfill_task.apply(kwargs={"max_prs_per_repo": 5, "shas_per_pr": 5, "pages_per_sha": 1}).get()
        state.refresh_from_db()
        self.assertEqual(state.ci_checked_revision_version, state.revision_version)
