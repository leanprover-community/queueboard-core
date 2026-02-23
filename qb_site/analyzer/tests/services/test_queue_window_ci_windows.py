from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from core.models import Repository
from analyzer.models import QueueRuleSet, PRQueueWindow, PRRevision
from syncer.models import PullRequest, CheckRun, PRTimelineEvent, PRTimelineEventType, StatusContext
from analyzer.services.queue_windows import rebuild_queue_windows_for_ruleset
from analyzer.services.revisions import rebuild_pr_revisions


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class TestQueueWindowCIWindows(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=["lint"],
        )

    def _mk_pr(self, number: int) -> PullRequest:
        created = _dt(2024, 9, 1)
        updated = _dt(2024, 9, 2)
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=created,
            gh_updated_at=updated,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
        )

    def _add_revision(self, pr: PullRequest, head_sha: str, from_ts: datetime, to_ts: datetime | None, seq: int) -> None:
        PRRevision.objects.create(
            pull_request=pr,
            head_sha=head_sha,
            from_ts=from_ts,
            to_ts=to_ts,
            seq=seq,
        )

    def test_ci_windows_open_and_close_on_success_and_failure(self) -> None:
        pr = self._mk_pr(1)
        # Single head SHA across the whole interval.
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)

        # Failing CI first, then passing, then failing again.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_FAIL",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 2),
            gh_completed_at=_dt(2024, 9, 2),
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_OK",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 4),
            gh_completed_at=_dt(2024, 9, 4),
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_FAIL_LATE",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 8),
            gh_completed_at=_dt(2024, 9, 8),
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 10))
        self.assertEqual(res.deleted, 0)

        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        # CI should be ok only from Sep 4 (first SUCCESS) until Sep 8 (later FAILURE).
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 4))
        self.assertEqual(windows[0].to_ts, _dt(2024, 9, 8))

    def test_ci_prefix_matches_check_run_name(self) -> None:
        pr = self._mk_pr(10)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)

        # Required context is "lint", but the actual check run is suffixed.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_OK_FORK",
            head_sha="sha1",
            name="lint (fork)",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 3),
            gh_completed_at=_dt(2024, 9, 3),
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 6))
        self.assertEqual(res.deleted, 0)

        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 3))
        self.assertIsNone(windows[0].to_ts)

    def test_ci_prefix_matches_status_context_name(self) -> None:
        pr = self._mk_pr(11)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)

        StatusContext.objects.create(
            pull_request=pr,
            github_node_id="SC_OK_FORK",
            head_sha="sha1",
            name="lint (fork)",
            state="SUCCESS",
            description="ok",
            target_url=None,
            gh_created_at=_dt(2024, 9, 4),
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 6))
        self.assertEqual(res.deleted, 0)

        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 4))
        self.assertIsNone(windows[0].to_ts)

    def test_ci_windows_across_force_push_and_revisions(self) -> None:
        pr = self._mk_pr(2)
        # Head sha1 from Sep 1-6, then sha2 from Sep 6 onwards.
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), _dt(2024, 9, 6), 0)
        self._add_revision(pr, "sha2", _dt(2024, 9, 6), None, 1)

        # CI success for sha1 at Sep 3.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_sha1_OK",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 3),
            gh_completed_at=_dt(2024, 9, 3),
        )
        # No CI for sha2 until Sep 9.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_sha2_OK",
            head_sha="sha2",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 9),
            gh_completed_at=_dt(2024, 9, 9),
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 12))
        self.assertEqual(res.deleted, 0)

        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        # Expect:
        # - First window from Sep 3 (sha1 CI success) until Sep 6 (revision boundary).
        # - Second window from Sep 9 (sha2 CI success) and still open at as_of.
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 3))
        self.assertEqual(windows[0].to_ts, _dt(2024, 9, 6))
        self.assertEqual(windows[1].from_ts, _dt(2024, 9, 9))
        self.assertIsNone(windows[1].to_ts)

    def test_missing_or_pending_ci_yields_no_windows(self) -> None:
        pr = self._mk_pr(3)
        self._add_revision(pr, "shaP", _dt(2024, 9, 1), None, 0)

        # Only a pending run (status != COMPLETED) exists; no successful snapshot.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_PENDING",
            head_sha="shaP",
            name="lint",
            status="IN_PROGRESS",
            conclusion=None,
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 4),
            gh_completed_at=None,
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 10))
        self.assertEqual(res.deleted, 0)
        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules))
        # With only missing/pending CI for the required context, we should not
        # persist any windows for this PR.
        self.assertEqual(len(windows), 0)

    def test_end_to_end_revisions_and_ci_windows_with_head_change(self) -> None:
        pr = self._mk_pr(4)

        # CI success for initial head sha1 at Sep 2.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_sha1_OK",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 2),
            gh_completed_at=_dt(2024, 9, 2),
        )

        # Later CI failure for new head sha2 at Sep 6 (no success for sha2).
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_sha2_FAIL",
            head_sha="sha2",
            name="lint",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 6),
            gh_completed_at=_dt(2024, 9, 6),
        )

        # Build PRRevision windows from CI (no force-push events present).
        res_rev = rebuild_pr_revisions(pr)
        self.assertGreaterEqual(PRRevision.objects.filter(pull_request=pr).count(), 2)
        self.assertGreaterEqual(res_rev.created, 1)

        # Now rebuild queue windows using those revisions and CI snapshots.
        as_of = _dt(2024, 9, 10)
        res_qw = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=as_of)
        self.assertEqual(res_qw.deleted, 0)

        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        # Expect a single window on the queue:
        # - From Sep 2 (first SUCCESS for sha1) until Sep 6, when the head
        #   changes to sha2 and CI is failing for that head.
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 2))
        self.assertEqual(windows[0].to_ts, _dt(2024, 9, 6))

    def test_end_to_end_force_push_and_ci_inferred_heads(self) -> None:
        pr = self._mk_pr(5)
        t_fp = _dt(2024, 9, 5)

        # Force-push splits the timeline into two segments with baselines h0 and h2.
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=t_fp,
            before_sha="h0",
            after_sha="h2",
        )

        # Segment 1: success for h0, then success for h1 (CI-inferred head change).
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_h0_OK",
            head_sha="h0",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 2),
            gh_completed_at=_dt(2024, 9, 2),
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_h1_OK",
            head_sha="h1",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 4),
            gh_completed_at=_dt(2024, 9, 4),
        )

        # Segment 2: success for h2, then a failing head h3 inferred from CI.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_h2_OK",
            head_sha="h2",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 6),
            gh_completed_at=_dt(2024, 9, 6),
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_h3_FAIL",
            head_sha="h3",
            name="lint",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 7),
            gh_completed_at=_dt(2024, 9, 7),
        )

        # Build revisions from force-push + CI signals.
        rebuild_pr_revisions(pr)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        # Expected revision heads: h0 -> h1 (CI) -> h2 (force-push) -> h3 (CI)
        self.assertEqual([r.head_sha for r in revs], ["h0", "h1", "h2", "h3"])

        # Rebuild queue windows; as_of after all events.
        as_of = _dt(2024, 9, 10)
        res_qw = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=as_of)
        self.assertEqual(res_qw.deleted, 0)

        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        # Queue windows should:
        # - Open when h0 first succeeds (Sep 2) and stay open through h1 in the same segment until the force-push at Sep 5.
        # - Remain closed between Sep 5 and Sep 6 (h2 has no success yet).
        # - Open again once h2 succeeds (Sep 6) and close when h3 (failing) becomes the head at Sep 7.
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 2))
        self.assertEqual(windows[0].to_ts, t_fp)
        self.assertEqual(windows[1].from_ts, _dt(2024, 9, 6))
        self.assertEqual(windows[1].to_ts, _dt(2024, 9, 7))

    def test_checkrun_wins_tie_over_status_context_at_same_timestamp(self) -> None:
        pr = self._mk_pr(6)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)

        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_TIE_FAIL",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 4),
            gh_completed_at=_dt(2024, 9, 4),
        )
        StatusContext.objects.create(
            pull_request=pr,
            github_node_id="SC_TIE_OK",
            head_sha="sha1",
            name="lint",
            state="SUCCESS",
            description="ok",
            target_url=None,
            gh_created_at=_dt(2024, 9, 4),
        )

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 6))
        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        self.assertEqual(windows, [])

    def test_required_contexts_can_be_satisfied_across_checkrun_and_status_context(self) -> None:
        rules_two = QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=["lint", "build"],
        )
        pr = self._mk_pr(7)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)

        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_LINT_OK",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 2),
            gh_completed_at=_dt(2024, 9, 2),
        )
        StatusContext.objects.create(
            pull_request=pr,
            github_node_id="SC_BUILD_OK",
            head_sha="sha1",
            name="build",
            state="SUCCESS",
            description="ok",
            target_url=None,
            gh_created_at=_dt(2024, 9, 4),
        )

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rules_two, as_of=_dt(2024, 9, 6))
        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=rules_two).order_by("from_ts"))
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 4))
        self.assertIsNone(windows[0].to_ts)

    def test_revision_to_ts_without_boundary_event_keeps_window_open_until_as_of(self) -> None:
        pr = self._mk_pr(8)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), _dt(2024, 9, 5), 0)

        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_SHA1_OK",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 2),
            gh_completed_at=_dt(2024, 9, 2),
        )

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 7))
        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 2))
        # Legacy behavior: revision to_ts is not itself a boundary unless another
        # event occurs there, so this window closes at the next evaluated boundary
        # (as_of in this test).
        self.assertEqual(windows[0].to_ts, _dt(2024, 9, 7))
