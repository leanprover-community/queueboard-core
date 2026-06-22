from __future__ import annotations

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Repository
from syncer.models import CommitCheckRun, CommitStatusContext, PullRequest, PRTimelineEvent, PRTimelineEventType
from analyzer.services.revisions import (
    PR_REVISION_BUILDER_VERSION,
    mark_pr_revision_dirty_if_earlier,
    next_revision_backfill_shas,
    rebuild_pr_revisions,
)
from analyzer.models import PRRevision, PRRevisionBuildState


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

    def test_build_from_head_sha_when_ci_missing(self) -> None:
        pr = self._mk_pr(2)
        pr.head_sha = "seed123"
        pr.save(update_fields=["head_sha"])

        res = rebuild_pr_revisions(pr)

        self.assertEqual(res.deleted, 0)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0].head_sha, "seed123")
        self.assertEqual(revs[0].from_ts, pr.gh_created_at)
        self.assertIsNone(revs[0].to_ts)

    def _seed_old_head_state(self, pr: PullRequest, old_head: str) -> None:
        """Simulate the state left by a prior build: one window + CI for an old head."""
        created = pr.gh_created_at
        PRRevision.objects.create(pull_request=pr, head_sha=old_head, from_ts=created, to_ts=None, seq=0)
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id=f"CR_{old_head}",
            head_sha=old_head,
            name="ci",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            external_id=None,
            gh_started_at=created + timezone.timedelta(minutes=5),
            gh_completed_at=created + timezone.timedelta(minutes=10),
        )
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            built_through_ts=created + timezone.timedelta(minutes=10),
            dirty_from_ts=None,
            builder_version=PR_REVISION_BUILDER_VERSION,
        )

    def test_trailing_window_for_current_head_without_ci_or_force_push(self) -> None:
        # A fork PR whose newest commit had CI skipped: head advanced via a plain push,
        # so there is no force-push event and no CI for the current head. The builder
        # must still open a trailing window for pr.head_sha instead of staying pinned to
        # the old, failed commit. The boundary is dated from gh_updated_at (push-time
        # proxy) since GitHub does not expose the actual push time.
        pr = self._mk_pr(40)
        created = pr.gh_created_at
        old_head, new_head = "oldhead0", "newhead9"
        self._seed_old_head_state(pr, old_head)
        t_push = created + timezone.timedelta(hours=2)
        pr.head_sha = new_head
        pr.gh_updated_at = t_push  # bumped by the push
        pr.save(update_fields=["head_sha", "gh_updated_at"])

        res = rebuild_pr_revisions(pr)

        self.assertNotEqual(res.strategy, "noop")
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[0].head_sha, old_head)
        self.assertEqual(revs[0].from_ts, created)
        self.assertEqual(revs[0].to_ts, t_push)
        self.assertEqual(revs[1].head_sha, new_head)
        self.assertEqual(revs[1].from_ts, t_push)
        self.assertIsNone(revs[1].to_ts)

    def test_trailing_window_stable_across_rebuilds(self) -> None:
        # Once the trailing window exists, a later rebuild must not move its from_ts or
        # churn the revision_version: the boundary is reused from the existing window,
        # not recomputed from a since-drifted gh_updated_at.
        pr = self._mk_pr(42)
        created = pr.gh_created_at
        old_head, new_head = "oldhead2", "newhead2"
        self._seed_old_head_state(pr, old_head)
        pr.head_sha = new_head
        pr.gh_updated_at = created + timezone.timedelta(hours=2)
        pr.save(update_fields=["head_sha", "gh_updated_at"])

        rebuild_pr_revisions(pr)
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        version_after_first = state.revision_version
        trailing_from = PRRevision.objects.filter(pull_request=pr, head_sha=new_head).get().from_ts
        self.assertEqual(trailing_from, created + timezone.timedelta(hours=2))

        # Later unrelated activity drifts gh_updated_at forward and a CI re-run on the old
        # head advances the latest signal, forcing another rebuild pass.
        pr.gh_updated_at = created + timezone.timedelta(hours=6)
        pr.save(update_fields=["gh_updated_at"])
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CR_rerun",
            head_sha=old_head,
            name="ci",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            external_id=None,
            gh_started_at=created + timezone.timedelta(hours=6),
            gh_completed_at=created + timezone.timedelta(hours=6, minutes=5),
        )

        rebuild_pr_revisions(pr)

        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[1].head_sha, new_head)
        # Reused from the existing window — NOT moved to the drifted gh_updated_at.
        self.assertEqual(revs[1].from_ts, trailing_from)
        state.refresh_from_db()
        self.assertEqual(state.revision_version, version_after_first)

    def test_no_trailing_window_when_head_matches_force_push(self) -> None:
        # Common case: pr.head_sha equals the last force-push after_sha -> no extra window.
        pr = self._mk_pr(43)
        t0 = pr.gh_created_at + timezone.timedelta(hours=1)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t0,
            before_sha="h0",
            after_sha="h1",
        )
        pr.head_sha = "h1"
        pr.save(update_fields=["head_sha"])

        rebuild_pr_revisions(pr)

        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[-1].head_sha, "h1")
        self.assertIsNone(revs[-1].to_ts)

    def test_trailing_window_after_force_push_then_plain_push(self) -> None:
        # Force-push to h1, then a plain commit push to h2 (no event, no CI): the builder
        # appends a trailing window for h2 after the force-push-derived windows.
        pr = self._mk_pr(44)
        t0 = pr.gh_created_at + timezone.timedelta(hours=1)
        t_push = pr.gh_created_at + timezone.timedelta(hours=3)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t0,
            before_sha="h0",
            after_sha="h1",
        )
        pr.head_sha = "h2"
        pr.gh_updated_at = t_push
        pr.save(update_fields=["head_sha", "gh_updated_at"])

        rebuild_pr_revisions(pr)

        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(revs), 3)
        self.assertEqual(revs[0].head_sha, "h0")
        self.assertEqual(revs[0].to_ts, t0)
        self.assertEqual(revs[1].head_sha, "h1")
        self.assertEqual(revs[1].to_ts, t_push)
        self.assertEqual(revs[2].head_sha, "h2")
        self.assertEqual(revs[2].from_ts, t_push)
        self.assertIsNone(revs[2].to_ts)

    def test_head_mismatch_forces_rebuild_without_new_signal(self) -> None:
        # A plain push with NO new CI and NO new timeline event does not advance any
        # time-based signal, so the noop short-circuit would skip it. head_mismatch must
        # force the rebuild so the current head is tracked.
        pr = self._mk_pr(47)
        created = pr.gh_created_at
        PRRevision.objects.create(pull_request=pr, head_sha="h_old", from_ts=created, to_ts=None, seq=0)
        built_through = created + timezone.timedelta(hours=1)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            built_through_ts=built_through,
            dirty_from_ts=None,
            builder_version=PR_REVISION_BUILDER_VERSION,
        )
        pr.head_sha = "h_new"
        pr.gh_updated_at = created + timezone.timedelta(minutes=30)
        pr.save(update_fields=["head_sha", "gh_updated_at"])

        # latest_signal_ts (no CI/timeline) falls back to built_through, so only
        # head_mismatch can prevent a noop here.
        res = rebuild_pr_revisions(pr, latest_signal_ts=built_through)

        self.assertNotEqual(res.strategy, "noop")
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual([r.head_sha for r in revs], ["h_new"])
        self.assertIsNone(revs[-1].to_ts)

    def test_trailing_window_handles_revert_to_prior_sha(self) -> None:
        # Head A -> B (force-push), then reverted to A via a plain push (no event). A fresh
        # A window is opened from the re-push (gh_updated_at), distinct from the original A
        # window — the original window's start is NOT reused for the reverted head.
        pr = self._mk_pr(45)
        created = pr.gh_created_at
        t0 = created + timezone.timedelta(hours=1)
        t_repush = created + timezone.timedelta(hours=3)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t0,
            before_sha="A",
            after_sha="B",
        )
        pr.head_sha = "A"
        pr.gh_updated_at = t_repush
        pr.save(update_fields=["head_sha", "gh_updated_at"])

        rebuild_pr_revisions(pr)

        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual([r.head_sha for r in revs], ["A", "B", "A"])
        self.assertEqual(revs[0].from_ts, created)
        self.assertEqual(revs[0].to_ts, t0)
        self.assertEqual(revs[1].from_ts, t0)
        self.assertEqual(revs[1].to_ts, t_repush)
        self.assertEqual(revs[2].from_ts, t_repush)
        self.assertIsNone(revs[2].to_ts)

    def test_trailing_window_redates_when_ci_arrives_later(self) -> None:
        # A fork PR's CI-less current head gets a synthetic window from the push; later CI
        # actually runs for it. The window is re-dated to the CI time WITHOUT leaving a
        # coverage gap (the prior window's to_ts moves too), and then converges.
        pr = self._mk_pr(46)
        created = pr.gh_created_at
        old_head, new_head = "oldhead6", "newhead6"
        self._seed_old_head_state(pr, old_head)
        t_push = created + timezone.timedelta(hours=2)
        pr.head_sha = new_head
        pr.gh_updated_at = t_push
        pr.save(update_fields=["head_sha", "gh_updated_at"])

        rebuild_pr_revisions(pr)
        new_rev = PRRevision.objects.get(pull_request=pr, head_sha=new_head)
        self.assertEqual(new_rev.from_ts, t_push)  # dated from the push, no CI yet

        # CI finally runs for the current head (e.g. a maintainer approved the fork run).
        t_ci = created + timezone.timedelta(hours=5)
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CR_new6",
            head_sha=new_head,
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=t_ci,
            gh_completed_at=t_ci + timezone.timedelta(minutes=5),
        )

        rebuild_pr_revisions(pr)

        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual([r.head_sha for r in revs], [old_head, new_head])
        # No gap: the old-head window now closes exactly where the new-head window opens.
        self.assertEqual(revs[0].to_ts, t_ci)
        self.assertEqual(revs[1].from_ts, t_ci)
        self.assertIsNone(revs[1].to_ts)

        # Converged: a further rebuild changes nothing.
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        version_after = state.revision_version
        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.strategy, "noop")
        state.refresh_from_db()
        self.assertEqual(state.revision_version, version_after)

    def test_build_state_updated_on_full_rebuild(self) -> None:
        pr = self._mk_pr(9)
        created = pr.gh_created_at
        t_fp = created + timezone.timedelta(hours=3)
        t_ci_end = created + timezone.timedelta(hours=4)

        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t_fp,
            before_sha="old",
            after_sha="new",
        )
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CR_state",
            head_sha="new",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=t_ci_end - timezone.timedelta(minutes=5),
            gh_completed_at=t_ci_end,
        )

        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.strategy, "full")
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertEqual(state.builder_version, PR_REVISION_BUILDER_VERSION)
        # built_through_ts should reflect the latest signal (CI completion here).
        self.assertEqual(state.built_through_ts, t_ci_end)
        self.assertIsNone(state.dirty_from_ts)
        self.assertIsNotNone(state.last_built_at)
        self.assertEqual(state.revision_version, 1)
        self.assertIsNone(state.ci_checked_revision_version)
        self.assertIsNone(state.ci_checked_at)

        tail = state.tail_revision
        self.assertIsNotNone(tail)
        self.assertEqual(state.tail_from_ts, tail.from_ts)
        self.assertEqual(tail.head_sha, "new")

    def test_rebuild_noop_when_state_clean_and_no_new_signals(self) -> None:
        pr = self._mk_pr(12)
        PRRevision.objects.create(pull_request=pr, head_sha="seed", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        # Seed a clean state that claims to be built through t1.
        t1 = pr.gh_created_at + timezone.timedelta(hours=1)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            built_through_ts=t1,
            dirty_from_ts=None,
            builder_version=PR_REVISION_BUILDER_VERSION,
        )
        res = rebuild_pr_revisions(pr, latest_signal_ts=t1)
        self.assertEqual(res.strategy, "noop")
        self.assertEqual(res.created, 0)
        self.assertEqual(res.deleted, 0)

    def test_rebuild_noop_preserves_ci_checked(self) -> None:
        pr = self._mk_pr(121)
        pr.head_sha = "seed"
        pr.save(update_fields=["head_sha"])
        _ = rebuild_pr_revisions(pr)
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        state.ci_checked_revision_version = state.revision_version
        state.ci_checked_at = timezone.now()
        state.save(
            update_fields=[
                "ci_checked_revision_version",
                "ci_checked_at",
            ]
        )

        t_later = (state.built_through_ts or pr.gh_created_at) + timezone.timedelta(hours=1)
        res = rebuild_pr_revisions(pr, latest_signal_ts=t_later)
        self.assertEqual(res.strategy, "noop")
        state.refresh_from_db()
        self.assertEqual(state.revision_version, 1)
        self.assertEqual(state.ci_checked_revision_version, 1)
        self.assertIsNotNone(state.ci_checked_at)

    def test_rebuild_full_when_builder_version_mismatch(self) -> None:
        pr = self._mk_pr(13)
        t1 = pr.gh_created_at + timezone.timedelta(hours=1)
        # Seed an older builder_version.
        state = PRRevisionBuildState.objects.create(
            pull_request=pr,
            built_through_ts=t1,
            dirty_from_ts=None,
            builder_version=PR_REVISION_BUILDER_VERSION - 1,
        )
        res = rebuild_pr_revisions(pr, latest_signal_ts=t1)
        # No timelines/CI so no revisions, but strategy should reflect a full run and builder_version bumps.
        self.assertEqual(res.strategy, "full")
        state.refresh_from_db()
        self.assertEqual(state.builder_version, PR_REVISION_BUILDER_VERSION)

    def test_rebuild_noop_without_new_signals_autodetect(self) -> None:
        pr = self._mk_pr(14)
        PRRevision.objects.create(pull_request=pr, head_sha="seed", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        t1 = pr.gh_created_at + timezone.timedelta(hours=1)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            built_through_ts=t1,
            dirty_from_ts=None,
            builder_version=PR_REVISION_BUILDER_VERSION,
        )
        # No timeline/CI rows exist; latest_signal_ts auto-detect falls back to built_through_ts.
        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.strategy, "noop")
        self.assertEqual(res.created, 0)
        self.assertEqual(res.deleted, 0)

    def test_rebuild_runs_when_no_revisions_exist(self) -> None:
        pr = self._mk_pr(14)
        pr.head_sha = "seed123"
        pr.save(update_fields=["head_sha"])
        t1 = pr.gh_created_at + timezone.timedelta(hours=1)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            built_through_ts=t1,
            dirty_from_ts=None,
            builder_version=PR_REVISION_BUILDER_VERSION,
        )

        res = rebuild_pr_revisions(pr, latest_signal_ts=t1)

        self.assertNotEqual(res.strategy, "noop")
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0].head_sha, "seed123")

    def test_rebuild_no_signal_no_seed_does_not_churn_revision_version(self) -> None:
        pr = self._mk_pr(141)
        t1 = pr.gh_created_at + timezone.timedelta(hours=1)
        state = PRRevisionBuildState.objects.create(
            pull_request=pr,
            built_through_ts=t1,
            dirty_from_ts=None,
            builder_version=PR_REVISION_BUILDER_VERSION,
            revision_version=7,
        )

        first = rebuild_pr_revisions(pr, latest_signal_ts=t1)
        self.assertEqual(first.strategy, "noop")
        state.refresh_from_db()
        self.assertEqual(state.revision_version, 7)
        self.assertEqual(PRRevision.objects.filter(pull_request=pr).count(), 0)

        second = rebuild_pr_revisions(pr, latest_signal_ts=t1)
        self.assertEqual(second.strategy, "noop")
        state.refresh_from_db()
        self.assertEqual(state.revision_version, 7)
        self.assertEqual(PRRevision.objects.filter(pull_request=pr).count(), 0)

    def test_rebuild_append_strategy_when_clean_and_new_signal(self) -> None:
        pr = self._mk_pr(15)
        # Initial rebuild seeds state with one window
        t_fp1 = pr.gh_created_at + timezone.timedelta(hours=1)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t_fp1,
            before_sha="h0",
            after_sha="h1",
        )
        first_res = rebuild_pr_revisions(pr)
        self.assertEqual(first_res.strategy, "full")

        # Add a new force-push after the built_through_ts mark with a new head.
        t_fp2 = t_fp1 + timezone.timedelta(hours=2)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t_fp2,
            before_sha="h1",
            after_sha="h2",
        )

        # Clean state, existing revisions, new signal in the future -> append strategy.
        res = rebuild_pr_revisions(pr, latest_signal_ts=t_fp2)
        self.assertEqual(res.strategy, "append")
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(revs), 3)
        self.assertEqual(revs[0].head_sha, "h0")
        self.assertEqual(revs[1].head_sha, "h1")
        self.assertEqual(revs[2].head_sha, "h2")

    def test_revision_version_increments_and_resets_ci_marker(self) -> None:
        pr = self._mk_pr(115)
        pr.head_sha = "seed115"
        pr.save(update_fields=["head_sha"])
        # Initial rebuild seeds version 1
        first = rebuild_pr_revisions(pr)
        self.assertEqual(first.strategy, "full")
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertEqual(state.revision_version, 1)

        # Pretend a CI coverage check was recorded for this version
        state.ci_checked_revision_version = state.revision_version
        state.ci_checked_at = timezone.now()
        state.save(update_fields=["ci_checked_revision_version", "ci_checked_at"])

        # Add a new signal to force an append; expect version bump and CI marker cleared.
        t_fp = pr.gh_created_at + timezone.timedelta(hours=2)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t_fp,
            before_sha="h_prev",
            after_sha="h_new",
        )
        res = rebuild_pr_revisions(pr, latest_signal_ts=t_fp)
        self.assertEqual(res.strategy, "append")

        state.refresh_from_db()
        self.assertEqual(state.revision_version, 2)
        self.assertIsNone(state.ci_checked_revision_version)
        self.assertIsNone(state.ci_checked_at)

    def test_append_preserves_prefix_and_rewrites_tail(self) -> None:
        pr = self._mk_pr(16)
        t0 = pr.gh_created_at + timezone.timedelta(hours=1)
        t1 = pr.gh_created_at + timezone.timedelta(hours=2)
        # First build: two windows
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t0,
            before_sha="h0",
            after_sha="h1",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t1,
            before_sha="h1",
            after_sha="h2",
        )
        first = rebuild_pr_revisions(pr, latest_signal_ts=t1)
        self.assertEqual(first.strategy, "full")
        # Add one more force-push after built_through_ts; expect append and prefix retained.
        t2 = t1 + timezone.timedelta(hours=1)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t2,
            before_sha="h2",
            after_sha="h3",
        )
        res = rebuild_pr_revisions(pr, latest_signal_ts=t2)
        self.assertEqual(res.strategy, "append")
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(len(revs), 4)
        self.assertEqual(revs[0].head_sha, "h0")
        self.assertEqual(revs[1].head_sha, "h1")
        self.assertEqual(revs[2].head_sha, "h2")
        self.assertEqual(revs[3].head_sha, "h3")

    def test_mark_dirty_if_signal_is_earlier(self) -> None:
        pr = self._mk_pr(10)
        before_ts = pr.gh_created_at - timezone.timedelta(hours=2)
        after_ts = pr.gh_created_at + timezone.timedelta(hours=2)

        # Seed a build state as if we had rebuilt through `after_ts`.
        state = PRRevisionBuildState.objects.create(
            pull_request=pr,
            built_through_ts=after_ts,
            dirty_from_ts=None,
            builder_version=PR_REVISION_BUILDER_VERSION,
        )

        # Signal older than built_through_ts should mark dirty.
        updated = mark_pr_revision_dirty_if_earlier(pr, before_ts)
        state.refresh_from_db()
        self.assertTrue(updated)
        self.assertEqual(state.dirty_from_ts, before_ts)

        # A later signal should not change dirty_from_ts.
        updated = mark_pr_revision_dirty_if_earlier(pr, after_ts + timezone.timedelta(minutes=5))
        state.refresh_from_db()
        self.assertFalse(updated)
        self.assertEqual(state.dirty_from_ts, before_ts)

    def test_mark_dirty_noop_without_built_through(self) -> None:
        pr = self._mk_pr(11)
        # No built_through_ts yet.
        state = PRRevisionBuildState.objects.create(
            pull_request=pr,
            built_through_ts=None,
            dirty_from_ts=None,
            builder_version=PR_REVISION_BUILDER_VERSION,
        )
        updated = mark_pr_revision_dirty_if_earlier(pr, pr.gh_created_at - timezone.timedelta(days=1))
        state.refresh_from_db()
        self.assertFalse(updated)
        self.assertIsNone(state.dirty_from_ts)

    def test_seed_from_ci_when_no_force_push(self) -> None:
        pr = self._mk_pr(2)
        # No HEAD_FORCE_PUSHED events; seed from CI snapshots (single head)
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        # Without force-push/revision/head-sha association, commit-scoped CI rows
        # alone are not used to infer arbitrary PR heads.
        self.assertEqual(len(revs), 0)

    def test_multiple_heads_from_ci_when_no_force_push(self) -> None:
        pr = self._mk_pr(5)
        t1 = pr.gh_created_at + timezone.timedelta(hours=2)
        t2 = pr.gh_created_at + timezone.timedelta(hours=5)
        # CI snapshots for two different head SHAs; expect two windows ordered by earliest timestamp.
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        self.assertEqual(len(revs), 0)

    def test_noop_when_not_backfilled(self) -> None:
        pr = self._mk_pr(4)
        # Mark as not backfilled
        pr.timeline_backfill_done = False
        pr.save(update_fields=["timeline_backfill_done"])
        res = rebuild_pr_revisions(pr)
        self.assertEqual(res.created, 0)
        self.assertEqual(res.deleted, 0)
        self.assertEqual(res.strategy, "skipped")
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        # State should not be advanced when skipping work.
        self.assertIsNone(state.built_through_ts)
        self.assertIsNone(state.dirty_from_ts)
        self.assertIsNone(state.last_built_at)
        self.assertIsNone(state.tail_revision)

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
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        # Expect windows that respect force-push boundaries (baseline heads only).
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

    def test_next_backfill_targets_picks_missing_ci_shas(self) -> None:
        pr = self._mk_pr(3)
        # Build windows explicitly
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        PRRevision.objects.create(
            pull_request=pr, head_sha="b2", from_ts=pr.gh_created_at + timezone.timedelta(hours=1), to_ts=None, seq=1
        )
        # Only b2 has CI
        CommitStatusContext.objects.create(
            repository=self.repo,
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

    def test_next_backfill_targets_include_timeline_heads(self) -> None:
        pr = self._mk_pr(17)
        t0 = pr.gh_created_at + timezone.timedelta(hours=1)
        t1 = pr.gh_created_at + timezone.timedelta(hours=2)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t0,
            before_sha="t_before",
            after_sha="t_after",
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t1,
            before_sha="t_after",
            after_sha="t_after2",
        )
        # No revisions or CI yet; expect oldest timeline heads first.
        shas = next_revision_backfill_shas(pr, limit=3)
        self.assertEqual(shas, ["t_before", "t_after", "t_after2"])

    def test_next_backfill_targets_include_pending_and_queued_ci(self) -> None:
        pr = self._mk_pr(30)
        created = pr.gh_created_at
        PRRevision.objects.create(pull_request=pr, head_sha="pending_sc", from_ts=created, to_ts=None, seq=0)
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="queued_cr",
            from_ts=created + timezone.timedelta(hours=1),
            to_ts=None,
            seq=1,
        )
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="done",
            from_ts=created + timezone.timedelta(hours=2),
            to_ts=None,
            seq=2,
        )

        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC_pending",
            rest_id=None,
            head_sha="pending_sc",
            name="lint",
            state="PENDING",
            target_url=None,
            description=None,
            gh_created_at=created + timezone.timedelta(minutes=5),
        )
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CR_pending",
            head_sha="queued_cr",
            name="ci",
            status="QUEUED",
            conclusion=None,
            details_url=None,
            external_id=None,
            gh_started_at=None,
            gh_completed_at=None,
        )
        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC_done",
            rest_id=None,
            head_sha="done",
            name="lint",
            state="SUCCESS",
            target_url=None,
            description=None,
            gh_created_at=created + timezone.timedelta(hours=2, minutes=10),
        )

        shas = next_revision_backfill_shas(pr, limit=5)
        self.assertEqual(shas, ["pending_sc", "queued_cr"])

    def test_pending_status_not_selected_when_completed_exists(self) -> None:
        pr = self._mk_pr(31)
        PRRevision.objects.create(pull_request=pr, head_sha="mixed", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC_pending_mixed",
            rest_id=None,
            head_sha="mixed",
            name="lint",
            state="PENDING",
            target_url=None,
            description=None,
            gh_created_at=pr.gh_created_at + timezone.timedelta(minutes=5),
        )
        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC_done_mixed",
            rest_id=None,
            head_sha="mixed",
            name="lint",
            state="SUCCESS",
            target_url=None,
            description=None,
            gh_created_at=pr.gh_created_at + timezone.timedelta(minutes=10),
        )

        shas = next_revision_backfill_shas(pr, limit=2)
        self.assertEqual(shas, [])

    @override_settings(ANALYZER_PENDING_STATUS_STALE_NON_OPEN_HOURS=8)
    def test_pending_status_not_selected_for_stale_non_open_pr(self) -> None:
        pr = self._mk_pr(32)
        pr.state = "merged"
        pr.merged_at = timezone.now() - timezone.timedelta(days=365)
        pr.gh_updated_at = pr.merged_at
        pr.save(update_fields=["state", "merged_at", "gh_updated_at", "updated_at"])
        PRRevision.objects.create(pull_request=pr, head_sha="stale_pending", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC_pending_stale",
            rest_id=None,
            head_sha="stale_pending",
            name="bors",
            state="PENDING",
            target_url=None,
            description=None,
            gh_created_at=pr.gh_created_at + timezone.timedelta(minutes=5),
        )

        shas = next_revision_backfill_shas(pr, limit=2)
        self.assertEqual(shas, [])

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
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        CommitCheckRun.objects.create(
            repository=self.repo,
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

        # Expected windows from force-push baselines only:
        #   [created, t0) on h0
        #   [t0, t1) on h2
        #   [t1, None) on h4
        self.assertEqual(len(revs), 3)

        self.assertEqual(revs[0].head_sha, "h0")
        self.assertEqual(revs[0].from_ts, created)
        self.assertEqual(revs[0].to_ts, t0)

        self.assertEqual(revs[1].head_sha, "h2")
        self.assertEqual(revs[1].from_ts, t0)
        self.assertEqual(revs[1].to_ts, t1)

        self.assertEqual(revs[2].head_sha, "h4")
        self.assertEqual(revs[2].from_ts, t1)
        self.assertIsNone(revs[2].to_ts)

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
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        CommitCheckRun.objects.create(
            repository=self.repo,
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
