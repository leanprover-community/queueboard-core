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
            timeline_backfill_done=True,
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
        # No HEAD_FORCE_PUSHED events; seed from CI snapshots (single head)
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

    def test_multiple_heads_from_ci_when_no_force_push(self) -> None:
        pr = self._mk_pr(5)
        t1 = pr.gh_created_at + timezone.timedelta(hours=2)
        t2 = pr.gh_created_at + timezone.timedelta(hours=5)
        # CI snapshots for two different head SHAs; expect two windows ordered by earliest timestamp.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR1",
            head_sha="aaa111",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=pr.gh_created_at + timezone.timedelta(hours=1),
            gh_completed_at=t1,
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR2",
            head_sha="bbb222",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=pr.gh_created_at + timezone.timedelta(hours=4),
            gh_completed_at=t2,
        )

        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.deleted, 0)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(revs), 2)
        # First head is aaa111 from PR creation until we first see bbb222; second
        # head is bbb222 from that point onward.
        self.assertEqual(revs[0].head_sha, "aaa111")
        self.assertEqual(revs[0].from_ts, pr.gh_created_at)
        self.assertEqual(revs[0].to_ts, revs[1].from_ts)
        # Second head is bbb222 from its first CI onward
        self.assertEqual(revs[1].head_sha, "bbb222")
        # The exact boundary comes from the earliest CI timestamp for bbb222; we
        # only assert that it starts after creation and that the window is open-ended.
        self.assertGreater(revs[1].from_ts, pr.gh_created_at)
        self.assertIsNone(revs[1].to_ts)

    def test_noop_when_not_backfilled(self) -> None:
        pr = self._mk_pr(4)
        # Mark as not backfilled
        pr.timeline_backfill_done = False
        pr.save(update_fields=["timeline_backfill_done"])
        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.created, 0)
        self.assertEqual(res.deleted, 0)

    def test_force_push_and_ci_heads_combined(self) -> None:
        pr = self._mk_pr(6)
        t0 = pr.gh_created_at + timezone.timedelta(hours=1)
        t1 = pr.gh_created_at + timezone.timedelta(hours=2)
        t_ci1 = pr.gh_created_at + timezone.timedelta(minutes=10)
        t_ci2 = pr.gh_created_at + timezone.timedelta(hours=3)
        # Force-push events define hard boundaries; within each segment we also
        # incorporate CI-derived head changes.
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
        # CI snapshots for other heads inside segments.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CRX1",
            head_sha="xxx000",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=t_ci1,
            gh_completed_at=t_ci1 + timezone.timedelta(minutes=10),
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CRX2",
            head_sha="yyy000",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=t_ci2,
            gh_completed_at=t_ci2 + timezone.timedelta(minutes=30),
        )

        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.deleted, 0)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        # Expect windows that respect force-push boundaries and also capture
        # CI-derived head changes within segments:
        #   [created, t_ci1) on aaa111
        #   [t_ci1, t0) on xxx000
        #   [t0, t1) on bbb222
        #   [t1, t_ci2) on ccc333
        #   [t_ci2, None) on yyy000
        self.assertEqual(len(revs), 5)
        self.assertEqual(revs[0].head_sha, "aaa111")
        self.assertEqual(revs[0].from_ts, pr.gh_created_at)
        self.assertEqual(revs[0].to_ts, t_ci1)
        self.assertEqual(revs[1].head_sha, "xxx000")
        self.assertEqual(revs[1].from_ts, t_ci1)
        self.assertEqual(revs[1].to_ts, t0)
        self.assertEqual(revs[2].head_sha, "bbb222")
        self.assertEqual(revs[2].from_ts, t0)
        self.assertEqual(revs[2].to_ts, t1)
        self.assertEqual(revs[3].head_sha, "ccc333")
        self.assertEqual(revs[3].from_ts, t1)
        self.assertEqual(revs[3].to_ts, t_ci2)
        self.assertEqual(revs[4].head_sha, "yyy000")
        self.assertEqual(revs[4].from_ts, t_ci2)
        self.assertIsNone(revs[4].to_ts)

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

    def test_ci_heads_before_and_after_force_pushes(self) -> None:
        pr = self._mk_pr(7)
        created = pr.gh_created_at
        t0 = created + timezone.timedelta(hours=1)
        t1 = created + timezone.timedelta(hours=4)

        # Two force-push events partition the timeline into three segments.
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t0,
            before_sha="h0",
            after_sha="h2",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t1,
            before_sha="h2",
            after_sha="h4",
        )

        # CI heads:
        # Segment 1: [created, t0) -> baseline h0 with an additional head h1.
        t_ci1 = created + timezone.timedelta(minutes=30)
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_seg1",
            head_sha="h1",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=t_ci1,
            gh_completed_at=t_ci1 + timezone.timedelta(minutes=10),
        )

        # Segment 2: [t0, t1) -> baseline h2 with an additional head h3.
        t_ci2 = t0 + timezone.timedelta(hours=1)
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_seg2",
            head_sha="h3",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=t_ci2,
            gh_completed_at=t_ci2 + timezone.timedelta(minutes=10),
        )

        # Segment 3: [t1, None) -> baseline h4 with two additional heads h5, h6.
        t_ci3 = t1 + timezone.timedelta(minutes=30)
        t_ci4 = t1 + timezone.timedelta(hours=1)
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_seg3a",
            head_sha="h5",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=t_ci3,
            gh_completed_at=t_ci3 + timezone.timedelta(minutes=10),
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_seg3b",
            head_sha="h6",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=t_ci4,
            gh_completed_at=t_ci4 + timezone.timedelta(minutes=10),
        )

        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.deleted, 0)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))

        # Expected windows:
        #   [created, t_ci1) on h0
        #   [t_ci1, t0) on h1
        #   [t0, t_ci2) on h2
        #   [t_ci2, t1) on h3
        #   [t1, t_ci3) on h4
        #   [t_ci3, t_ci4) on h5
        #   [t_ci4, None) on h6
        self.assertEqual(len(revs), 7)

        self.assertEqual(revs[0].head_sha, "h0")
        self.assertEqual(revs[0].from_ts, created)
        self.assertEqual(revs[0].to_ts, t_ci1)

        self.assertEqual(revs[1].head_sha, "h1")
        self.assertEqual(revs[1].from_ts, t_ci1)
        self.assertEqual(revs[1].to_ts, t0)

        self.assertEqual(revs[2].head_sha, "h2")
        self.assertEqual(revs[2].from_ts, t0)
        self.assertEqual(revs[2].to_ts, t_ci2)

        self.assertEqual(revs[3].head_sha, "h3")
        self.assertEqual(revs[3].from_ts, t_ci2)
        self.assertEqual(revs[3].to_ts, t1)

        self.assertEqual(revs[4].head_sha, "h4")
        self.assertEqual(revs[4].from_ts, t1)
        self.assertEqual(revs[4].to_ts, t_ci3)

        self.assertEqual(revs[5].head_sha, "h5")
        self.assertEqual(revs[5].from_ts, t_ci3)
        self.assertEqual(revs[5].to_ts, t_ci4)

        self.assertEqual(revs[6].head_sha, "h6")
        self.assertEqual(revs[6].from_ts, t_ci4)
        self.assertIsNone(revs[6].to_ts)

    def test_force_push_with_ci_for_baseline_heads_only(self) -> None:
        pr = self._mk_pr(8)
        t0 = pr.gh_created_at + timezone.timedelta(hours=1)
        t1 = pr.gh_created_at + timezone.timedelta(hours=2)

        # Force-push events define three segments with baselines aaa111, bbb222, ccc333.
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

        # CI only for the baseline heads; should not introduce extra windows.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_base1",
            head_sha="aaa111",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=pr.gh_created_at + timezone.timedelta(minutes=10),
            gh_completed_at=pr.gh_created_at + timezone.timedelta(minutes=20),
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_base2",
            head_sha="bbb222",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=t0 + timezone.timedelta(minutes=10),
            gh_completed_at=t0 + timezone.timedelta(minutes=20),
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_base3",
            head_sha="ccc333",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=t1 + timezone.timedelta(minutes=10),
            gh_completed_at=t1 + timezone.timedelta(minutes=20),
        )

        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.deleted, 0)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))

        # Still expect three windows driven purely by force-push baselines:
        #   [created, t0) on aaa111
        #   [t0, t1) on bbb222
        #   [t1, None) on ccc333
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
