from __future__ import annotations

from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from core.models import Repository
from syncer.models import PullRequest, StatusContext
from analyzer.models import PRRevision


class TestPlanCIBackfillCommand(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        # PR with two revisions: a1 (missing), b2 (has CI)
        self.pr = PullRequest.objects.create(
            repository=self.repo,
            number=5,
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
        StatusContext.objects.create(
            pull_request=self.pr,
            github_node_id="SC1",
            rest_id=None,
            head_sha="b2",
            name="lint",
            state="SUCCESS",
            target_url=None,
            description=None,
            gh_created_at="2024-01-01T01:05:00Z",
        )

    def test_dry_run_lists_missing_shas(self) -> None:
        out = []

        def _write(s: str) -> None:
            out.append(s)

        with mock.patch("sys.stdout.write", side_effect=_write):
            call_command("plan_ci_backfill", repo=f"{self.repo.owner}/{self.repo.name}", pr=[self.pr.number], limit=2)
        joined = "".join(out)
        self.assertIn("PR #5", joined)
        self.assertIn("a1", joined)
        self.assertNotIn("b2", joined)

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_enqueue_calls_syncer_task(self, MockTask) -> None:
        res = mock.Mock()
        res.id = "T123"
        MockTask.delay.return_value = res
        call_command(
            "plan_ci_backfill",
            repo=f"{self.repo.owner}/{self.repo.name}",
            pr=[self.pr.number],
            limit=1,
            enqueue=True,
            pages_per_sha=1,
        )
        MockTask.delay.assert_called_once()
