from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRQueueWindow, PRQueueWindowBuildState, PRRevision, PRRevisionBuildState, QueueRuleSet
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
        rs_state = PRQueueWindowBuildState.objects.get(pull_request=pr, rule_set=self.rule_set)
        self.assertEqual(rs_state.revision_version_built, state.revision_version)
        self.assertIsNotNone(rs_state.windows_built_at)
        self.assertEqual(rs_state.last_status, "rebuilt")

    def test_skips_when_windows_already_built_for_version(self) -> None:
        pr = self._mk_pr(2)
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=2,
            windows_built_revision_version=2,
            windows_built_at=timezone.now(),
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()
        self.assertEqual(res["windows_rebuilt"], 0)
        # Ensure no new windows were created
        self.assertEqual(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rule_set).count(), 0)

    def test_rebuilds_when_rollup_fields_missing(self) -> None:
        pr = self._mk_pr(3)
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=timezone.now(),
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=0,
            first_on_queue_ts=None,
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["windows_rebuilt"], 1)
        qwin = PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rule_set).order_by("-from_ts").first()
        self.assertIsNotNone(qwin)
        self.assertGreaterEqual(qwin.window_count, 1)
        self.assertIsNotNone(qwin.first_on_queue_ts)

    def test_marks_built_at_even_when_rebuild_noop_after_ruleset_bump(self) -> None:
        pr = self._mk_pr(4)
        # Ensure windows exist and are already up-to-date for the current revision version.
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        old_built_at = timezone.now() - timezone.timedelta(days=2)
        state = PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=old_built_at,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )
        # Bump ruleset updated_at so the sweep considers this PR stale, but the rebuild is a no-op.
        self.rule_set.updated_at = timezone.now()
        self.rule_set.save(update_fields=["updated_at"])

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["windows_rebuilt"], 0)
        state.refresh_from_db()
        self.assertEqual(state.windows_built_revision_version, state.revision_version)
        self.assertIsNotNone(state.windows_built_at)
        self.assertGreater(state.windows_built_at, old_built_at)

    def test_rollup_backfill_on_inactive_ruleset_does_not_trigger_rebuild(self) -> None:
        pr = self._mk_pr(5)
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        built_at = timezone.now()
        state = PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=3,
            windows_built_revision_version=3,
            windows_built_at=built_at,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )
        inactive = QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            is_active=False,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=inactive,
            from_ts=pr.gh_created_at + timezone.timedelta(minutes=1),
            to_ts=None,
            cycle_index=0,
            window_count=0,
            first_on_queue_ts=None,
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["prs_checked"], 0)
        self.assertEqual(res["windows_rebuilt"], 0)
        state.refresh_from_db()
        self.assertEqual(state.windows_built_revision_version, 3)
        self.assertEqual(state.windows_built_at, built_at)

    def test_windows_built_at_equal_ruleset_updated_at_is_not_stale(self) -> None:
        pr = self._mk_pr(6)
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        built_at = timezone.now()
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=4,
            windows_built_revision_version=4,
            windows_built_at=built_at,
        )
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=built_at)

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["prs_checked"], 0)
        self.assertEqual(res["windows_rebuilt"], 0)

    def test_missing_build_state_is_initialized_then_skipped_next_sweep(self) -> None:
        pr = self._mk_pr(7)
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )

        res1 = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res1["prs_checked"], 1)
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertEqual(state.windows_built_revision_version, state.revision_version)
        self.assertIsNotNone(state.windows_built_at)

        res2 = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()
        self.assertEqual(res2["prs_checked"], 0)
        self.assertEqual(res2["windows_rebuilt"], 0)

    def test_rebuilds_only_stale_rulesets_for_pr(self) -> None:
        pr = self._mk_pr(8)
        rs_two = QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            is_active=True,
        )
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)

        built_at = timezone.now() - timezone.timedelta(days=2)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=built_at,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )
        state_one = PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
        )
        state_two = PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
        )
        # Align ruleset timestamps with built_at so only the explicit bump below
        # marks one ruleset as stale.
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=built_at)
        QueueRuleSet.objects.filter(pk=rs_two.pk).update(updated_at=built_at)
        # Only rs_two should be stale.
        QueueRuleSet.objects.filter(pk=rs_two.pk).update(updated_at=timezone.now())

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["prs_checked"], 1)
        state_one.refresh_from_db()
        state_two.refresh_from_db()
        self.assertEqual(state_one.windows_built_at, built_at)
        self.assertGreater(state_two.windows_built_at, built_at)

    def test_missing_ruleset_state_uses_legacy_fallback_when_up_to_date(self) -> None:
        pr = self._mk_pr(9)
        rs_two = QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            is_active=True,
        )
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        built_at = timezone.now()
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=built_at,
        )
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=built_at)
        QueueRuleSet.objects.filter(pk=rs_two.pk).update(updated_at=built_at)
        PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["prs_checked"], 0)
        self.assertFalse(PRQueueWindowBuildState.objects.filter(pull_request=pr, rule_set=rs_two).exists())

    def test_missing_ruleset_state_is_created_when_only_missing_ruleset_is_stale(self) -> None:
        pr = self._mk_pr(10)
        rs_two = QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            is_active=True,
        )
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        built_at = timezone.now() - timezone.timedelta(days=2)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=built_at,
        )
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=built_at)
        QueueRuleSet.objects.filter(pk=rs_two.pk).update(updated_at=built_at + timezone.timedelta(hours=1))
        existing = PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["prs_checked"], 1)
        existing.refresh_from_db()
        self.assertEqual(existing.windows_built_at, built_at)
        created = PRQueueWindowBuildState.objects.get(pull_request=pr, rule_set=rs_two)
        self.assertGreater(created.windows_built_at, built_at)
        self.assertEqual(created.last_status, "rebuilt")

    def test_rollup_backfill_marks_only_target_ruleset_stale(self) -> None:
        pr = self._mk_pr(11)
        rs_two = QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            is_active=True,
        )
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        built_at = timezone.now()
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=built_at,
        )
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=built_at)
        QueueRuleSet.objects.filter(pk=rs_two.pk).update(updated_at=built_at)
        state_one = PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
        )
        state_two = PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=0,
            first_on_queue_ts=None,
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["prs_checked"], 1)
        state_one.refresh_from_db()
        state_two.refresh_from_db()
        self.assertEqual(state_one.windows_built_at, built_at)
        self.assertGreater(state_two.windows_built_at, built_at)

    def test_revision_bump_marks_all_rulesets_stale(self) -> None:
        pr = self._mk_pr(12)
        rs_two = QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            is_active=True,
        )
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        built_at = timezone.now() - timezone.timedelta(days=1)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=2,
            windows_built_revision_version=1,
            windows_built_at=built_at,
        )
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=built_at)
        QueueRuleSet.objects.filter(pk=rs_two.pk).update(updated_at=built_at)
        state_one = PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
        )
        state_two = PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["prs_checked"], 1)
        state_one.refresh_from_db()
        state_two.refresh_from_db()
        self.assertEqual(state_one.revision_version_built, 2)
        self.assertEqual(state_two.revision_version_built, 2)
        self.assertGreater(state_one.windows_built_at, built_at)
        self.assertGreater(state_two.windows_built_at, built_at)
