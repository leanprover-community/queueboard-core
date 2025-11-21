from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRRevision, PRRevisionBuildState, QueueRuleSet, PRQueueWindow
from analyzer.tasks.rebuild_queue_windows_sweep import rebuild_queue_windows_sweep_task
from core.models import Repository
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType


class TestRebuildQueueWindowsSweepTask(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
        )

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

    def test_builds_windows_when_revision_version_new(self) -> None:
        pr = self._mk_pr(1)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=pr.gh_created_at + timezone.timedelta(hours=1),
            before_sha="a1",
            after_sha="b2",
        )
        # Seed revisions and state
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        state = PRRevisionBuildState.objects.create(pull_request=pr, revision_version=1)

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["windows_rebuilt"], 1)
        self.assertEqual(
            PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rule_set).count(),
            1,
        )
        state.refresh_from_db()
        self.assertEqual(state.windows_built_revision_version, state.revision_version)
        self.assertIsNotNone(state.windows_built_at)

    def test_skips_when_windows_already_built_for_version(self) -> None:
        pr = self._mk_pr(2)
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        state = PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=2,
            windows_built_revision_version=2,
            windows_built_at=timezone.now(),
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()
        self.assertEqual(res["windows_rebuilt"], 0)
        # Ensure no new windows were created
        self.assertEqual(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rule_set).count(), 0)
