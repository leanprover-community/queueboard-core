from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRQueueWindowBuildState, PRRevisionBuildState, QueueRuleSet
from core.models import Repository
from syncer.models import PullRequest


class TestBackfillQueueWindowBuildStatesCommand(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            is_active=True,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
        )

    def _mk_pr(self, number: int) -> PullRequest:
        now = timezone.now()
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=now - timezone.timedelta(days=1),
            gh_updated_at=now,
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
            commits_backfill_done=True,
        )

    def test_dry_run_does_not_write(self) -> None:
        pr = self._mk_pr(1)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=timezone.now(),
        )

        call_command("backfill_queue_window_build_states", repo=f"{self.repo.owner}/{self.repo.name}")
        self.assertEqual(PRQueueWindowBuildState.objects.count(), 0)

    def test_write_persists_rows(self) -> None:
        pr = self._mk_pr(2)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=2,
            windows_built_revision_version=2,
            windows_built_at=timezone.now(),
        )

        call_command("backfill_queue_window_build_states", repo=f"{self.repo.owner}/{self.repo.name}", write=True)
        self.assertEqual(PRQueueWindowBuildState.objects.count(), 1)
