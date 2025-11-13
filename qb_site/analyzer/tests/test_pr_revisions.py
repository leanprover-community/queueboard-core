from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType, CheckRun, StatusContext
from analyzer.services.revisions import rebuild_pr_revisions, next_revision_backfill_shas
from analyzer.models import PRRevision


class TestPRRevisions(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)

    def _mk_pr(self, number: int) -> PullRequest:
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
        )

    def test_build_from_force_push_events(self) -> None:
        pr = self._mk_pr(1)
        t0 = pr.gh_created_at + timezone.timedelta(hours=1)
        t1 = pr.gh_created_at + timezone.timedelta(hours=2)
        # Two force-push events
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

        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.deleted, 0)
        # Expect three windows: [created, t0) on aaa111; [t0, t1) on bbb222; [t1, None) on ccc333
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(revs), 3)
        self.assertEqual(revs[0].head_sha, "aaa111")
        self.assertEqual(revs[0].from_ts, pr.gh_created_at)
        self.assertEqual(revs[0].to_ts, t0)
        self.assertEqual(revs[1].head_sha, "bbb222")
        self.assertEqual(revs[1].from_ts, t0)
        self.assertEqual(revs[1].to_ts, t1)
        self.assertEqual(revs[2].head_sha, "ccc333")
        self.assertEqual(revs[2].from_ts, t1)
        self.assertIsNone(revs[2].to_ts)

    def test_seed_from_ci_when_no_force_push(self) -> None:
        pr = self._mk_pr(2)
        # No HEAD_FORCE_PUSHED events; seed from most recent CI snapshot
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR1",
            head_sha="zzz999",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=pr.gh_created_at + timezone.timedelta(hours=1),
            gh_completed_at=pr.gh_created_at + timezone.timedelta(hours=2),
        )
        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.deleted, 0)
        revs = list(PRRevision.objects.filter(pull_request=pr))
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0].head_sha, "zzz999")
        self.assertIsNone(revs[0].to_ts)

    def test_next_backfill_targets_picks_missing_ci_shas(self) -> None:
        pr = self._mk_pr(3)
        # Build windows explicitly
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        PRRevision.objects.create(
            pull_request=pr, head_sha="b2", from_ts=pr.gh_created_at + timezone.timedelta(hours=1), to_ts=None, seq=1
        )
        # Only b2 has CI
        StatusContext.objects.create(
            pull_request=pr,
            github_node_id="SC1",
            rest_id=None,
            head_sha="b2",
            name="lint",
            state="SUCCESS",
            target_url=None,
            description=None,
            gh_created_at=pr.gh_created_at + timezone.timedelta(hours=1, minutes=5),
        )
        shas = next_revision_backfill_shas(pr, limit=2)
        self.assertEqual(shas, ["a1"])  # only 'a1' is missing CI
