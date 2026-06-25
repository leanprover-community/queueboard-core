from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from django.test import TestCase

from analyzer.models import PRDependency, PRQueueWindow, QueueRuleSet, PRRevision
from analyzer.models.queue_snapshot import QueueSnapshot
from analyzer.services.queueboard_snapshot import QueueboardSnapshotBuilder
from core.models import Repository, User
from syncer.models import CommitCheckRun, LabelDef, PRLabel, PullRequest
from syncer.models.ci_enums import CheckRunConclusion, CheckRunStatus
from syncer.models.pull_request import PullRequestState


class QueueboardSnapshotBuilderTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.user = User.objects.create(github_login="alice")
        self.now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)

    def _make_pr(
        self,
        number: int,
        *,
        is_draft: bool = False,
        base: str = "master",
        labels: tuple[str, ...] = (),
        author: User | None = None,
        body: str = "description",
        head_repo_owner_login: str = "fork-user",
    ) -> PullRequest:
        pr = PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=author,
            state=PullRequestState.OPEN,
            is_draft=is_draft,
            gh_created_at=self.now,
            gh_updated_at=self.now,
            closed_at=None,
            merged_at=None,
            base_ref_name=base,
            head_ref_name=f"feature/{number}",
            head_repo_owner_login=head_repo_owner_login,
            head_repo_name=self.repo.name,
            title=f"PR {number}",
            body=body,
            additions=1,
            deletions=1,
            changed_files_count=1,
            head_sha=f"sha{number}",
            files=["src/file.py"],
            assignees=[],
            approvals=[],
            commenters=[],
            number_total_comments=0,
            last_synced_at=self.now,
            files_incomplete=False,
            assignees_incomplete=False,
            reviews_incomplete=False,
            comments_incomplete=False,
            timeline_backfill_done=True,
        )
        for label_name in labels:
            label_def, _ = LabelDef.objects.get_or_create(repository=self.repo, name=label_name, defaults={"color": "123456"})
            PRLabel.objects.create(pull_request=pr, label_def=label_def)
        return pr

    def _add_ci(self, pr: PullRequest, *, conclusion=CheckRunConclusion.SUCCESS, status=CheckRunStatus.COMPLETED, name="lint"):
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id=f"cr-{pr.number}-{name}",
            head_sha=pr.head_sha or "",
            name=name,
            status=status,
            conclusion=conclusion,
            gh_started_at=self.now,
            gh_completed_at=self.now if status == CheckRunStatus.COMPLETED else None,
        )

    def test_builds_snapshot_with_queue_filters(self):
        pr1 = self._make_pr(1, author=self.user, labels=("t-analysis",))
        self._make_pr(2, labels=("awaiting-zulip",))
        self._make_pr(3, is_draft=True)
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
            forbidden_label_names=["awaiting-zulip"],
        )

        # CI success for pr1
        self._add_ci(pr1)

        # Dependency edge for pr1
        PRDependency.objects.create(
            pull_request=pr1,
            depends_on_repository=self.repo,
            depends_on_number=42,
        )

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)

        self.assertEqual(snapshot["meta"]["repository"], "leanprover-community/mathlib4")
        self.assertEqual(snapshot["meta"]["schema_version"], "v1-draft")
        self.assertEqual(snapshot["meta"]["rule_set_id"], rule_set.id)
        self.assertEqual(snapshot["meta"]["rule_set_version"], rule_set.version)
        self.assertTrue(snapshot["meta"]["require_ci_success"])
        self.assertEqual(snapshot["meta"]["ci_gating_mode"], QueueRuleSet.CIGatingMode.ALL_REQUIRED_SUCCESS)
        self.assertEqual(snapshot["meta"]["required_ci_contexts"], ["lint"])

        prs = snapshot["prs"]
        self.assertIn(1, prs)
        self.assertIn(2, prs)
        self.assertEqual(prs[1]["ci_status"], "pass")
        self.assertEqual(prs[2]["ci_status"], "missing")
        self.assertEqual(prs[1]["pr_status"], "AwaitingReview")
        self.assertEqual(prs[2]["pr_status"], "NotReady")
        self.assertEqual(prs[1]["direct_dependencies"], [42])
        self.assertEqual(prs[1]["labels"][0]["name"], "t-analysis")
        self.assertEqual(prs[1]["labels"][0]["url"], "https://github.com/leanprover-community/mathlib4/labels/t-analysis")
        self.assertEqual(prs[1]["head_repo"], "fork-user")
        self.assertEqual(prs[1]["data_status"]["comments"], "valid")

        self.assertEqual(set(snapshot["lists"]["nondraft_prs"]), {1, 2})
        self.assertEqual(set(snapshot["lists"]["draft_prs"]), {3})
        self.assertEqual(snapshot["lists"]["dashboards"]["Queue"], [1])
        self.assertEqual(snapshot["lists"]["dashboards"]["NeedsDecision"], [2])

    def test_queue_membership_respects_rule_set_ci_requirement(self):
        pr = self._make_pr(60)
        rule_set_ci = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        rule_set_no_ci = QueueRuleSet.objects.create(repository=self.repo, version=2, require_ci_success=False)

        builder = QueueboardSnapshotBuilder(chunk_size=1)
        snapshot_ci = builder.build(self.repo, rule_set=rule_set_ci)
        snapshot_no_ci = builder.build(self.repo, rule_set=rule_set_no_ci)

        self.assertNotIn(pr.number, snapshot_ci["lists"]["dashboards"]["Queue"])
        self.assertIn(pr.number, snapshot_no_ci["lists"]["dashboards"]["Queue"])

    def test_required_contexts_require_all_matching_jobs(self):
        pr = self._make_pr(61, labels=("t-analysis",))
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )

        self._add_ci(pr, name="lint / linux", conclusion=CheckRunConclusion.SUCCESS)
        self._add_ci(pr, name="lint / mac", conclusion=CheckRunConclusion.FAILURE)

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)

        self.assertEqual(snapshot["prs"][pr.number]["ci_status"], "fail")

    def test_ci_requirement_disabled_when_no_required_contexts(self):
        pr = self._make_pr(65)
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=[],
        )

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        self.assertIn(pr.number, snapshot["lists"]["dashboards"]["Queue"])
        self.assertEqual(snapshot["prs"][pr.number]["ci_status"], "pass")

    def test_required_context_contains_match_passes(self):
        pr = self._make_pr(66, author=self.user, labels=("t-analysis",))
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        self._add_ci(pr, conclusion=CheckRunConclusion.SUCCESS, name="ci / lint / linux")

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        self.assertEqual(snapshot["prs"][pr.number]["ci_status"], "pass")
        self.assertIn(pr.number, snapshot["lists"]["dashboards"]["Queue"])

    def test_queue_membership_respects_required_labels(self):
        pr_allowed = self._make_pr(61, labels=("t-analysis",))
        pr_blocked = self._make_pr(62)
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            required_label_names=["t-analysis"],
            forbidden_label_names=["wip"],
        )

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)

        self.assertIn(pr_allowed.number, snapshot["lists"]["dashboards"]["Queue"])
        self.assertNotIn(pr_blocked.number, snapshot["lists"]["dashboards"]["Queue"])

    def test_build_and_store_creates_snapshot_row(self):
        pr1 = self._make_pr(10, labels=("t-analysis",))
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="cr10",
            head_sha=pr1.head_sha or "",
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )

        builder = QueueboardSnapshotBuilder(chunk_size=5)
        obj = builder.build_and_store(self.repo, cache_key="default")

        self.assertEqual(obj.repository, self.repo)
        self.assertEqual(obj.cache_key, "default")
        self.assertEqual(obj.pr_count, 1)
        self.assertEqual(obj.queue_count, 1)
        self.assertIsNone(obj.expires_at)
        self.assertEqual(QueueSnapshot.objects.count(), 1)

        # Etag should be consistent with the payload content
        from hashlib import sha256
        import json as _json

        expected_etag = sha256(_json.dumps(obj.payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(obj.etag, expected_etag)

    def test_build_and_store_updates_existing_snapshot(self):
        pr1 = self._make_pr(20, labels=("t-analysis",))
        builder = QueueboardSnapshotBuilder(chunk_size=2)
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        first = builder.build_and_store(self.repo, cache_key="k", rule_set=rule_set)
        self.assertEqual(QueueSnapshot.objects.count(), 1)
        self.assertEqual(first.queue_count, 0)  # no CI yet

        # Add CI and a second PR off-queue
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="cr20",
            head_sha=pr1.head_sha or "",
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )
        self._make_pr(21, labels=("awaiting-zulip",))

        updated = builder.build_and_store(self.repo, cache_key="k", rule_set=rule_set)
        self.assertEqual(QueueSnapshot.objects.count(), 1)
        self.assertEqual(updated.pr_count, 2)
        self.assertEqual(updated.queue_count, 1)
        self.assertGreater(updated.generated_at, first.generated_at)

    def test_dashboards_cover_expected_keys(self):
        pr_queue = self._make_pr(30, labels=("easy",))
        pr_merge_conflict = self._make_pr(31, labels=("merge-conflict",))
        pr_ready_to_merge = self._make_pr(32, labels=("ready-to-merge",))
        pr_awaiting_zulip = self._make_pr(33, labels=("awaiting-zulip",))
        pr_help = self._make_pr(34, labels=("help-wanted",))
        self._make_pr(35, is_draft=True)
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
            forbidden_label_names=["merge-conflict"],
        )

        # CI for queue entry
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="cr30",
            head_sha=pr_queue.head_sha or "",
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )
        # CI for merge-conflict candidate so it qualifies for NeedsMerge
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="cr31",
            head_sha=pr_merge_conflict.head_sha or "",
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )

        snapshot = QueueboardSnapshotBuilder(chunk_size=5).build(self.repo)
        dashboards = snapshot["lists"]["dashboards"]

        expected_keys = {
            "Queue",
            "QueueNewContributor",
            "QueueEasy",
            "QueueTechDebt",
            "QueueStaleUnassigned",
            "QueueStaleAssigned",
            "NeedsDecision",
            "NeedsMerge",
            "InessentialCIFails",
            "TechDebt",
            "NeedsHelp",
            "OtherBase",
            "NotFromFork",
            "AllReadyToMerge",
            "StaleReadyToMerge",
            "StaleDelegated",
            "StaleMaintainerMerge",
            "AllMaintainerMerge",
            "StaleNewContributor",
            "Approved",
            "BadTitle",
            "Unlabelled",
            "ContradictoryLabels",
            "All",
        }
        self.assertTrue(expected_keys.issubset(set(dashboards.keys())))
        self.assertIn(pr_queue.number, dashboards["Queue"])
        self.assertIn(pr_merge_conflict.number, dashboards["NeedsMerge"])
        self.assertIn(pr_ready_to_merge.number, dashboards["AllReadyToMerge"])
        self.assertIn(pr_awaiting_zulip.number, dashboards["NeedsDecision"])
        self.assertIn(pr_help.number, dashboards["NeedsHelp"])

    def test_not_from_fork_dashboard(self):
        pr_nonfork = self._make_pr(70, head_repo_owner_login=self.repo.owner)
        pr_fork = self._make_pr(71, head_repo_owner_login="external-contributor")

        snapshot = QueueboardSnapshotBuilder(chunk_size=5).build(self.repo)
        dashboards = snapshot["lists"]["dashboards"]

        self.assertIn(pr_nonfork.number, dashboards["NotFromFork"])
        self.assertNotIn(pr_fork.number, dashboards["NotFromFork"])

    def test_ci_status_uses_head_rollup_for_untracked_failure(self):
        pr = self._make_pr(50, author=self.user, labels=("t-analysis",))
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        # Tracked contexts all pass, but head rollup is failing => fail-inessential.
        self._add_ci(pr, conclusion=CheckRunConclusion.SUCCESS)
        pr.head_ci_state = "FAILURE"
        pr.save(update_fields=["head_ci_state"])

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo)
        prs = snapshot["prs"]
        self.assertEqual(prs[50]["ci_status"], "fail-inessential")

    def test_ci_status_prefers_tracked_failure_over_head_rollup(self):
        pr = self._make_pr(51, author=self.user, labels=("t-analysis",))
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        self._add_ci(pr, conclusion=CheckRunConclusion.FAILURE)
        pr.head_ci_state = "SUCCESS"
        pr.save(update_fields=["head_ci_state"])

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo)
        prs = snapshot["prs"]
        self.assertEqual(prs[51]["ci_status"], "fail")

    def test_ci_status_running_when_tracked_in_progress(self):
        pr = self._make_pr(52, author=self.user, labels=("t-analysis",))
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        self._add_ci(pr, status=CheckRunStatus.IN_PROGRESS)

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo)
        prs = snapshot["prs"]
        self.assertEqual(prs[52]["ci_status"], "running")

    def test_ci_status_reads_commit_rows(self):
        pr = self._make_pr(520, author=self.user, labels=("t-analysis",))
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=10,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="sha520",
            from_ts=self.now,
            to_ts=None,
            seq=0,
        )
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CCR-520",
            head_sha="sha520",
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        self.assertEqual(snapshot["prs"][520]["ci_status"], "pass")
        self.assertIn(520, snapshot["lists"]["dashboards"]["Queue"])

    def test_ci_status_ignores_non_head_commit_rows(self):
        pr = self._make_pr(521, author=self.user, labels=("t-analysis",))
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=11,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        PRRevision.objects.create(
            pull_request=pr,
            head_sha="sha521",
            from_ts=self.now,
            to_ts=None,
            seq=0,
        )
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CR-521",
            head_sha="oldsha521",
            name="lint",
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            gh_started_at=self.now,
            gh_completed_at=self.now,
        )

        snap = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        self.assertEqual(snap["prs"][521]["ci_status"], "missing")
        self.assertNotIn(521, snap["lists"]["dashboards"]["Queue"])

    def test_ci_status_ignores_head_rollup_pending_when_required_pass(self):
        pr = self._make_pr(53, author=self.user, labels=("t-analysis",))
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        # Required contexts pass; head rollup pending should not override.
        self._add_ci(pr, conclusion=CheckRunConclusion.SUCCESS)
        pr.head_ci_state = "PENDING"
        pr.save(update_fields=["head_ci_state"])

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo)
        prs = snapshot["prs"]
        self.assertEqual(prs[53]["ci_status"], "pass")

    def test_ci_status_missing_required_context(self):
        self._make_pr(54, author=self.user, labels=("t-analysis",))
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo)
        prs = snapshot["prs"]
        self.assertEqual(prs[54]["ci_status"], "missing")

    def test_ci_status_missing_required_context_not_on_queue(self):
        pr = self._make_pr(55, author=self.user, labels=("t-analysis",))
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        self.assertNotIn(pr.number, snapshot["lists"]["dashboards"]["Queue"])

    def test_maintainer_merge_dashboards_parity(self):
        # Legacy AllMaintainerMerge = all NON-DRAFT maintainer-merge PRs minus ready-to-merge,
        # NOT age-gated and NOT excluding auto-merge-after-CI.
        a = self._make_pr(70, labels=("maintainer-merge",))
        b = self._make_pr(71, labels=("maintainer-merge", "auto-merge-after-CI"))
        c = self._make_pr(72, labels=("maintainer-merge", "ready-to-merge"))
        d = self._make_pr(73, is_draft=True, labels=("maintainer-merge",))
        rule_set = QueueRuleSet.objects.create(repository=self.repo, version=1, require_ci_success=False)

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        all_mm = snapshot["lists"]["dashboards"]["AllMaintainerMerge"]
        self.assertIn(a.number, all_mm)
        self.assertIn(b.number, all_mm)  # auto-merge-after-CI is NOT excluded (legacy parity)
        self.assertNotIn(c.number, all_mm)  # ready-to-merge is excluded
        self.assertNotIn(d.number, all_mm)  # draft is excluded (legacy operates on non-draft PRs)

    def test_stale_new_contributor_excludes_drafts(self):
        # gh_updated_at is set to a fixed past date in _make_pr, so these are "stale" vs the live
        # 7-day threshold. Legacy StaleNewContributor operates on non-draft PRs only.
        nondraft = self._make_pr(74, labels=("new-contributor",))
        draft = self._make_pr(75, is_draft=True, labels=("new-contributor",))
        rule_set = QueueRuleSet.objects.create(repository=self.repo, version=1, require_ci_success=False)

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        stale_nc = snapshot["lists"]["dashboards"]["StaleNewContributor"]
        self.assertIn(nondraft.number, stale_nc)
        self.assertNotIn(draft.number, stale_nc)  # draft excluded (legacy parity)

    def test_no_required_failures_allows_missing_context_on_queue(self):
        pr = self._make_pr(57, author=self.user, labels=("t-analysis",))
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            ci_gating_mode=QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES,
            required_ci_contexts=["lint"],
        )

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        self.assertEqual(snapshot["prs"][pr.number]["ci_status"], "missing")
        self.assertIn(pr.number, snapshot["lists"]["dashboards"]["Queue"])
        # Under 'no_required_failures', a missing required context must not be classified as
        # work-in-progress: the triage status stays consistent with queue eligibility.
        self.assertEqual(snapshot["prs"][pr.number]["pr_status"], "AwaitingReview")

    def test_no_required_failures_allows_running_context_on_queue(self):
        pr = self._make_pr(58, author=self.user, labels=("t-analysis",))
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            ci_gating_mode=QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES,
            required_ci_contexts=["lint"],
        )
        self._add_ci(pr, status=CheckRunStatus.IN_PROGRESS)

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        self.assertEqual(snapshot["prs"][pr.number]["ci_status"], "running")
        self.assertIn(pr.number, snapshot["lists"]["dashboards"]["Queue"])
        # A still-running required context is likewise tolerated under 'no_required_failures'.
        self.assertEqual(snapshot["prs"][pr.number]["pr_status"], "AwaitingReview")

    def test_no_required_failures_still_blocks_observed_failure(self):
        pr = self._make_pr(59, author=self.user, labels=("t-analysis",))
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            ci_gating_mode=QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES,
            required_ci_contexts=["lint"],
        )
        self._add_ci(pr, conclusion=CheckRunConclusion.FAILURE)

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        self.assertEqual(snapshot["prs"][pr.number]["ci_status"], "fail")
        self.assertNotIn(pr.number, snapshot["lists"]["dashboards"]["Queue"])
        # An actual required-context failure still marks the PR not ready, even under this mode.
        self.assertEqual(snapshot["prs"][pr.number]["pr_status"], "NotReady")

    def test_queue_includes_fail_inessential_when_required_contexts_pass(self):
        pr = self._make_pr(56, author=self.user, labels=("t-analysis",))
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_ci_success=True,
            required_ci_contexts=["lint"],
        )
        self._add_ci(pr, conclusion=CheckRunConclusion.SUCCESS)
        pr.head_ci_state = "FAILURE"
        pr.save(update_fields=["head_ci_state"])

        snapshot = QueueboardSnapshotBuilder(chunk_size=1).build(self.repo, rule_set=rule_set)
        self.assertIn(pr.number, snapshot["lists"]["dashboards"]["Queue"])

    def test_queue_timeline_fields_from_windows(self):
        rule_set = QueueRuleSet.objects.create(repository=self.repo, version=1)
        pr = self._make_pr(101)
        window1_start = self.now - timedelta(days=2)
        window1_end = self.now - timedelta(days=1)
        window2_start = self.now - timedelta(hours=12)
        window2_end = None
        duration1 = int((window1_end - window1_start).total_seconds())
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rule_set,
            from_ts=window1_start,
            to_ts=window1_end,
            cycle_index=0,
            duration_seconds_closed=duration1,
            cumulative_seconds_closed=duration1,
            window_count=2,
            first_on_queue_ts=window1_start,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rule_set,
            from_ts=window2_start,
            to_ts=window2_end,
            cycle_index=1,
            duration_seconds_closed=0,
            cumulative_seconds_closed=duration1,
            window_count=2,
            first_on_queue_ts=window1_start,
        )

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz:
                    return self.now
                return self.now.replace(tzinfo=None)

        with patch("analyzer.services.queueboard_snapshot.datetime", FixedDateTime):
            snapshot = QueueboardSnapshotBuilder(chunk_size=5).build(self.repo, rule_set=rule_set)

        entry = snapshot["prs"][pr.number]
        self.assertEqual(entry["first_on_queue"]["status"], "valid")
        self.assertEqual(entry["first_on_queue"]["date"], window1_start.isoformat())
        self.assertEqual(entry["total_queue_time"]["status"], "valid")
        self.assertEqual(entry["total_queue_time"]["value_td"], 129600)
        self.assertEqual(entry["last_queue_status_change"]["time"], window2_start.isoformat())
        self.assertEqual(entry["last_queue_status_change"]["current_status"], "OnQueue")
        self.assertEqual(entry["last_queue_status_change"]["delta"]["hours"], 12)

    def test_snapshot_queue_fields_with_open_ended_window(self) -> None:
        rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=[],
            forbidden_label_names=[],
        )
        pr = self._make_pr(102)
        window_start = self.now - timedelta(hours=6)
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rule_set,
            from_ts=window_start,
            to_ts=None,
            cycle_index=0,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=1,
            first_on_queue_ts=window_start,
        )

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz:
                    return self.now
                return self.now.replace(tzinfo=None)

        with patch("analyzer.services.queueboard_snapshot.datetime", FixedDateTime):
            snapshot = QueueboardSnapshotBuilder(chunk_size=5).build(self.repo, rule_set=rule_set)

        entry = snapshot["prs"][pr.number]
        self.assertEqual(entry["first_on_queue"]["status"], "valid")
        self.assertEqual(entry["first_on_queue"]["date"], window_start.isoformat())
        self.assertEqual(entry["total_queue_time"]["status"], "valid")
        self.assertEqual(entry["total_queue_time"]["value_td"], 6 * 60 * 60)
        self.assertEqual(entry["last_queue_status_change"]["time"], window_start.isoformat())
        self.assertEqual(entry["last_queue_status_change"]["current_status"], "OnQueue")
        self.assertEqual(entry["last_queue_status_change"]["delta"]["hours"], 6)

    def test_queue_timeline_fields_tail_explanation(self) -> None:
        rule_set = QueueRuleSet.objects.create(repository=self.repo, version=3)
        pr = self._make_pr(103)
        window_count = 6
        starts = [self.now - timedelta(hours=6 - idx) for idx in range(window_count)]
        ends = [start + timedelta(hours=1) for start in starts]
        cumulative = 0
        for idx, (start, end) in enumerate(zip(starts, ends)):
            duration = int((end - start).total_seconds())
            cumulative += duration
            PRQueueWindow.objects.create(
                pull_request=pr,
                rule_set=rule_set,
                from_ts=start,
                to_ts=end,
                cycle_index=idx,
                duration_seconds_closed=duration,
                cumulative_seconds_closed=cumulative,
                window_count=window_count,
                first_on_queue_ts=starts[0],
            )

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz:
                    return self.now
                return self.now.replace(tzinfo=None)

        with patch("analyzer.services.queueboard_snapshot.datetime", FixedDateTime):
            snapshot = QueueboardSnapshotBuilder(chunk_size=5).build(self.repo, rule_set=rule_set)

        entry = snapshot["prs"][pr.number]
        explanation = entry["total_queue_time"]["explanation"]
        self.assertIn("from 2025-01-01 07:00 to 2025-01-01 08:00 (1 hour)", explanation)
        self.assertIn("from 2025-01-01 11:00 to 2025-01-01 12:00 (1 hour)", explanation)
        self.assertTrue(explanation.endswith("(last 5 of 6)"))

    def test_queue_timeline_fields_open_window_explanation(self) -> None:
        rule_set = QueueRuleSet.objects.create(repository=self.repo, version=4)
        pr = self._make_pr(104)
        window_start = self.now - timedelta(hours=6)
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rule_set,
            from_ts=window_start,
            to_ts=None,
            cycle_index=0,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=1,
            first_on_queue_ts=window_start,
        )

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz:
                    return self.now
                return self.now.replace(tzinfo=None)

        with patch("analyzer.services.queueboard_snapshot.datetime", FixedDateTime):
            snapshot = QueueboardSnapshotBuilder(chunk_size=5).build(self.repo, rule_set=rule_set)

        entry = snapshot["prs"][pr.number]
        explanation = entry["total_queue_time"]["explanation"]
        self.assertIn("since 2025-01-01 06:00 (6 hours)", explanation)
