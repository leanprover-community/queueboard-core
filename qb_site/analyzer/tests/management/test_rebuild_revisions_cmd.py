from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType
from analyzer.models import PRRevision, PRRevisionBuildState
from analyzer.services.revisions import PR_REVISION_BUILDER_VERSION


class TestRebuildRevisionsCommand(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)

    def _mk_backfilled_pr(self, number: int) -> PullRequest:
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

    def test_builds_windows_from_events(self) -> None:
        pr = self._mk_backfilled_pr(10)
        t0 = pr.gh_created_at + timezone.timedelta(hours=1)
        t1 = pr.gh_created_at + timezone.timedelta(hours=2)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t0,
            before_sha="aaa111",
            after_sha="bbb222",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t1,
            before_sha="bbb222",
            after_sha="ccc333",
        )

        call_command("rebuild_revisions", repo=f"{self.repo.owner}/{self.repo.name}", pr=[pr.number])
        windows = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0].head_sha, "aaa111")
        self.assertEqual(windows[0].to_ts, t0)
        self.assertEqual(windows[1].head_sha, "bbb222")
        self.assertEqual(windows[1].from_ts, t0)
        self.assertEqual(windows[1].to_ts, t1)
        self.assertEqual(windows[2].head_sha, "ccc333")
        self.assertIsNone(windows[2].to_ts)

    def test_skips_not_backfilled(self) -> None:
        now = timezone.now()
        pr = PullRequest.objects.create(
            repository=self.repo,
            number=11,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=now - timezone.timedelta(days=1),
            gh_updated_at=now - timezone.timedelta(hours=1),
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=False,
        )
        call_command("rebuild_revisions", repo=f"{self.repo.owner}/{self.repo.name}", pr=[pr.number])
        self.assertEqual(PRRevision.objects.filter(pull_request=pr).count(), 0)

    def test_command_updates_build_state(self) -> None:
        pr = self._mk_backfilled_pr(12)
        call_command("rebuild_revisions", repo=f"{self.repo.owner}/{self.repo.name}", pr=[pr.number])
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertEqual(state.builder_version, PR_REVISION_BUILDER_VERSION)
        self.assertIsNone(state.dirty_from_ts)
        self.assertIsNotNone(state.built_through_ts)
